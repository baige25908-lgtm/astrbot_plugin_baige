# -*- coding: utf-8 -*-
"""白鸽·余额（dsh_balance）

- QQ 里发 `/余额`（或 `/查余额`）直接回当前 DeepSeek 余额
- 每小时自检，低于阈值私聊提醒（12 小时最多一次）
- 结果写在本插件数据目录（打开「自用：报表写到 bridge_dir」后才写 /stage）

API key 的来源（按顺序）：
  1) 面板配置 `deepseek_api_key`（推荐，公开安装就用这个）
  2) 环境变量 DEEPSEEK_API_KEY
  3) 仅当打开「允许读取宿主 LLM 配置」时，才去读 AstrBot 的 cmd_config.json
没配 key 就只会回「没配 key」，不会去翻宿主配置。
"""
import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

from . import settings
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

CONFIG = settings.CMD_CONFIG
BALANCE_API = "https://api.deepseek.com/user/balance"

OWNER_QQ = settings.owner_qq()
LOW_THRESHOLD = 5.0      # 仅用于统计，不再据此私聊
ALERT_ENABLED = False    # 低余额私聊提醒：关闭（用户要求）
CHECK_INTERVAL = 3600
ALERT_COOLDOWN = 12 * 3600


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "balance"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self._state = self._load()
        self._task = None

    def _load(self):
        try:
            if self.state_path.is_file():
                return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            self.state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except Exception:
            pass

    # ---------- 取 key ----------
    def _api_key(self):
        key = str(settings._c("deepseek_api_key") or "").strip()
        if key:
            return key
        key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if key:
            return key
        if not settings.flag("read_host_llm_key"):
            return ""
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
            for src in cfg.get("provider_sources", []):
                if src.get("provider") == "deepseek":
                    keys = src.get("key") or []
                    if isinstance(keys, list) and keys:
                        return keys[0]
                    if isinstance(keys, str) and keys:
                        return keys
        except Exception as e:
            logger.warning(f"[dsh_balance] 读宿主配置失败: {e}")
        return ""

    async def _fetch(self):
        key = self._api_key()
        if not key:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    BALANCE_API,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=15,
                ) as r:
                    if r.status != 200:
                        logger.warning(f"[dsh_balance] 接口返回 {r.status}")
                        return None
                    return await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"[dsh_balance] 查询失败: {e}")
            return None

    @staticmethod
    def _fmt(data):
        infos = (data or {}).get("balance_infos") or []
        if not infos:
            return "没查到余额信息"
        parts = []
        for it in infos:
            cur = it.get("currency", "")
            sym = "¥" if cur == "CNY" else ""
            parts.append(
                f"{sym}{it.get('total_balance', '?')} {cur}（充值 {it.get('topped_up_balance', '?')}"
                f" / 赠送 {it.get('granted_balance', '?')}）"
            )
        avail = "可用" if (data or {}).get("is_available") else "**不可用（可能欠费/超额）**"
        return "DeepSeek 余额：" + "；".join(parts) + f"，状态：{avail}"

    # ---------- 命令 ----------
    async def balance_cmd(self, event: AstrMessageEvent):
        """查询 DeepSeek 账户余额。用法：/余额（别名：/查余额）"""
        data = await self._fetch()
        if not data:
            yield event.plain_result("查不到余额，可能是接口异常或 key 失效")
            return
        text = self._fmt(data)
        self._record(data)
        yield event.plain_result(text)

    def _record(self, data):
        try:
            infos = (data or {}).get("balance_infos") or [{}]
            total = float(infos[0].get("total_balance") or 0)
            out = settings.report("balance.json")
            out.write_text(
                json.dumps(
                    {
                        "ts": int(time.time()),
                        "is_available": data.get("is_available"),
                        "total": total,
                        "raw": data,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return total
        except Exception:
            return None

    async def _notify(self, text):
        try:
            for inst in getattr(self.context.platform_manager, "platform_insts", []) or []:
                bot = getattr(inst, "bot", None)
                if bot and hasattr(bot, "call_action"):
                    await bot.call_action("send_private_msg", user_id=OWNER_QQ, message=text)
                    return True
        except Exception as e:
            logger.warning(f"[dsh_balance] 私聊失败: {e}")
        return False

    async def _loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                data = await self._fetch()
                if data:
                    total = self._record(data)
                    now = int(time.time())
                    if (
                        ALERT_ENABLED
                        and total is not None
                        and total < LOW_THRESHOLD
                        and now - int(self._state.get("last_alert", 0)) > ALERT_COOLDOWN
                    ):
                        self._state["last_alert"] = now
                        self._save()
                        await self._notify(
                            f"余额只剩 {total:.2f}，快没了，记得充一下。"
                        )
                        logger.info(f"[dsh_balance] 余额：{total:.2f}（低于阈值，仅记录不打扰）")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[dsh_balance] 检查异常: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def initialize(self):
        if not self._api_key():
            logger.warning(
                "[dsh_balance] 没有配置 deepseek_api_key，余额查询不可用"
                "（面板里填 key，或打开「允许读取宿主 LLM 配置」）"
            )
        self._task = asyncio.create_task(self._loop())
        logger.info("[dsh_balance] 已启动：QQ 里发 /余额 可查（低余额提醒关闭）")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[dsh_balance] 已停止")
