# -*- coding: utf-8 -*-
"""白鸽·分寸（dsh_attach）

根据「她主动说话 -> 你多久回」来调节 emotion_core 的主动频率：
- 连续 3 次你 >2 小时才回  -> 当天上限 3 条、间隔 3 小时
- 连续 2 次你 <10 分钟就回 -> 恢复 12 条、间隔 45 分钟
- 每天每个方向最多调一次，避免抖动
所有调整写日志，可人工回滚。
"""
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from . import settings
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

TZ = timezone(timedelta(hours=8))
TOKEN_FILE = settings.report(".panel_token")
LOG = settings.report("attach.log")
SLOW_GAP = 2 * 3600
FAST_GAP = 600

SLOW_PROFILE = {"daily_limit": 3, "cooldown_hours": 3.0}
FAST_PROFILE = {"daily_limit": 12, "cooldown_hours": 0.75}


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "attach"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        st = self._load()
        self.state = st if isinstance(st, dict) else {}
        self.latencies = deque(st.get("latencies", []), maxlen=10)
        self.last_bot_at = float(st.get("last_bot_at", 0) or 0)
        self._busy = False

    def _load(self):
        try:
            if self.state_path.is_file():
                return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            self.state.update(
                {
                    "latencies": list(self.latencies),
                    "last_bot_at": self.last_bot_at,
                }
            )
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[dsh_attach] 保存状态失败: {e}")

    def _log(self, msg):
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(
                    f"[{datetime.now(TZ).strftime('%F %T')}] {msg}\n"
                )
        except Exception:
            pass
        logger.info(f"[dsh_attach] {msg}")

    # ---------- 记录 ----------
    async def mark_bot_spoke(self, event: AstrMessageEvent):
        try:
            if event.unified_msg_origin != settings.owner_umo():
                return
            res = event.get_result()
            if res is None or not getattr(res, "chain", None):
                return
            self.last_bot_at = time.time()
            self._save()
        except Exception:
            pass
    async def on_user_msg(self, event: AstrMessageEvent):
        try:
            if event.unified_msg_origin != settings.owner_umo():
                return
            if self.last_bot_at <= 0:
                return
            gap = time.time() - self.last_bot_at
            if gap > 12 * 3600:      # 太久不算样本
                self.last_bot_at = 0
                return
            self.latencies.append(int(gap))
            self.last_bot_at = 0
            self._save()
            await self._maybe_adjust()
        except Exception as e:
            logger.warning(f"[dsh_attach] 处理消息失败: {e}")

    # ---------- 调整 ----------
    async def _maybe_adjust(self):
        if self._busy:
            return
        today = datetime.now(TZ).strftime("%F")
        lat = list(self.latencies)

        slow = len(lat) >= 3 and all(x > SLOW_GAP for x in lat[-3:])
        fast = len(lat) >= 2 and all(x < FAST_GAP for x in lat[-2:])

        if slow and self.state.get("slow_day") != today:
            ok = await self._apply(SLOW_PROFILE)
            if ok:
                self.state["slow_day"] = today
                self.state["mode"] = "slow"
                self._save()
                await self._notify("她这几天先少发点：你回得比较慢，我把主动频率调低了。")
        elif fast and self.state.get("fast_day") != today:
            ok = await self._apply(FAST_PROFILE)
            if ok:
                self.state["fast_day"] = today
                self.state["mode"] = "fast"
                self._save()
                if self.state.get("mode") == "fast" and self.state.get("slow_day"):
                    await self._notify("你回得快，她把主动频率调回来了。")

    async def _apply(self, profile):
        """改 emotion_core 的主动配置。"""
        if self._busy:
            return False
        self._busy = True
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            self._busy = False
            self._log("没有面板 token，跳过调整")
            return False
        base = "http://127.0.0.1:6185"
        hdr = {"Authorization": f"Bearer {tok}"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{base}/api/v1/plugins/config?plugin_id=astrbot_plugin_emotion_core",
                    headers=hdr, timeout=15,
                ) as r:
                    data = (await r.json(content_type=None)).get("data", {})
                cfg = data.get("config") or {}
                pro = cfg.get("proactive") or {}
                before = (pro.get("daily_limit"), pro.get("cooldown_hours"))
                pro["daily_limit"] = profile["daily_limit"]
                pro["cooldown_hours"] = profile["cooldown_hours"]
                cfg["proactive"] = pro
                body = {"plugin_id": "astrbot_plugin_emotion_core", "config": cfg}
                async with s.put(
                    f"{base}/api/v1/plugins/config",
                    headers={**hdr, "Content-Type": "application/json"},
                    data=json.dumps(body), timeout=20,
                ) as r2:
                    if r2.status != 200:
                        self._log(f"写配置失败 HTTP {r2.status}")
                        return False
            self._log(
                f"主动频率调整 {before} -> ({profile['daily_limit']}, {profile['cooldown_hours']})"
            )
            return True
        except Exception as e:
            self._log(f"调整失败: {e}")
            return False
        finally:
            self._busy = False

    async def _notify(self, text):
        try:
            for inst in getattr(self.context.platform_manager, "platform_insts", []) or []:
                bot = getattr(inst, "bot", None)
                if bot and hasattr(bot, "call_action"):
                    await bot.call_action(
                        "send_private_msg", user_id=settings.owner_qq(), message=text
                    )
                    return
        except Exception:
            pass

    async def initialize(self):
        logger.info(
            f"[dsh_attach] 已启动：样本 {len(self.latencies)} 条，当前模式 {self.state.get('mode', '默认')}"
        )

    async def terminate(self):
        self._save()
        logger.info("[dsh_attach] 已停止")
