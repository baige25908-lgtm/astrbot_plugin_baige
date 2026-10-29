# -*- coding: utf-8 -*-
"""白鸽·体检（dsh_health）

每 10 分钟检查一次，出问题就私聊主人 + 写 /stage/health-report.md。
检查项：
  1) 三个「看图」核心补丁是否还在（AstrBot 升级会覆盖）
  2) /stage/_updates 是否可读（自更新链路）
  3) 素材库体积 / 文件数
  4) 可用内存
  5) vibe 的 sqlite 是否可写
同一类问题 6 小时内只报一次，避免刷屏。
"""
import asyncio
import json
import os
import time
from pathlib import Path

from . import settings
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

OWNER_QQ = settings.owner_qq()
INTERVAL = 600
COOLDOWN = 6 * 3600
REPORT = settings.report("health-report.md")

PATCHES = [
    (str(settings.ROOT / "astrbot/builtin_stars/astrbot/main.py"), "_dsh_fetch_image", "看图补丁1（当前消息取图）"),
    (str(settings.ROOT / "astrbot/core/pipeline/preprocess_stage/stage.py"), "_dsh_fallback_image", "看图补丁2（引用图回退）"),
    (str(settings.ROOT / "astrbot/core/platform/astr_message_event.py"), "[dsh] 调用方没传图时", "看图补丁3（请求自动补图）"),
]
VERSION_FILE = settings.ROOT / "astrbot/__init__.py"
LIB = settings.PLUGIN_DATA / "media/library"
PDATA = settings.PLUGIN_DATA.parent
PLUGINS = settings._DATA / "plugins"
ALL_DIR = settings.PLUGIN_DATA
AUTO_ROLLBACK = False  # 合并后由单一插件承担，自动回滚风险大于收益，默认只告警
BACKUP = PDATA / "dsh_backups"
# (组件名, 心跳最长允许的年龄秒)
WATCH = [
    ("updater", 900),
    ("vibe", 3 * 3600),
    ("media", 3 * 3600),
]
VIBE_DB = settings.PLUGIN_DATA / "vibe/vibe.db"


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "health"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "alert_state.json"
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

    # ---------- 检查 ----------
    def _checks(self):
        problems = []

        for path, marker, label in PATCHES:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="ignore")
                if marker not in text:
                    problems.append(f"{label} 不见了（AstrBot 可能升级过，需重打）")
            except Exception as e:
                problems.append(f"{label} 读不到：{e}")

        if not settings.bridge_dir().joinpath("_updates").is_dir():
            problems.append("自更新目录不可读（部署目录可能被搬动）")

        try:
            total = 0
            n = 0
            for cat in LIB.iterdir() if LIB.is_dir() else []:
                if cat.is_dir():
                    for f in cat.iterdir():
                        if f.is_file():
                            n += 1
                            total += f.stat().st_size
            if total > 5 * 1024 ** 3:
                problems.append(f"素材库偏大：{n} 个文件 / {total // 1024 ** 2}MB")
        except Exception as e:
            problems.append(f"素材库检查失败：{e}")

        try:
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
            avail_mb = mem.get("MemAvailable", 0) // 1024
            if avail_mb < 800:
                problems.append(f"可用内存偏低：{avail_mb}MB")
        except Exception:
            pass

        now = time.time()
        for name, max_age in WATCH:
            hb = ALL_DIR / name / "heartbeat"
            try:
                if not hb.is_file():
                    problems.append(f"{name} 没有心跳（可能没加载成功）")
                elif now - hb.stat().st_mtime > max_age:
                    problems.append(
                        f"{name} 心跳过期 {int((now - hb.stat().st_mtime) / 60)} 分钟"
                    )
            except Exception as e:
                problems.append(f"{name} 心跳检查失败：{e}")

        # AstrBot 版本变化哨兵：升级会覆盖看图补丁
        try:
            ver = ""
            for line in VERSION_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("__version__"):
                    ver = line.split("=", 1)[1].strip().strip('"\'')
                    break
            if ver:
                last = self._state.get("astrbot_version", "")
                if last and last != ver:
                    problems.append(
                        f"AstrBot 版本变了：{last} -> {ver}，看图补丁可能已被覆盖，需要重打"
                    )
                self._state["astrbot_version"] = ver
                self._save()
        except Exception as e:
            problems.append(f"版本哨兵失败：{e}")

        try:
            if VIBE_DB.is_file():
                import sqlite3

                with sqlite3.connect(f"file:{VIBE_DB}?mode=ro", uri=True, timeout=5) as c:
                    c.execute("SELECT count(*) FROM memes").fetchone()
        except Exception as e:
            problems.append(f"vibe 数据库异常：{e}")

        return problems

    # ---------- 通知 ----------
    async def _notify(self, text):
        sent = False
        try:
            for inst in getattr(self.context.platform_manager, "platform_insts", []) or []:
                bot = getattr(inst, "bot", None)
                if bot and hasattr(bot, "call_action"):
                    await bot.call_action(
                        "send_private_msg", user_id=OWNER_QQ, message=text
                    )
                    sent = True
                    break
        except Exception as e:
            logger.warning(f"[health] 私聊告警失败: {e}")
        logger.warning(f"[health] 告警：{text[:120]}（已私聊={sent}）")

    async def _rollback(self, problems):
        """有 .prev 备份且对应插件心跳异常时，自动还原并重启一次。"""
        import shutil

        restored = []
        for name, _age in WATCH:
            if not any(name in p for p in problems):
                continue
            prev = BACKUP / "astrbot_plugin_baige"
            dst = PLUGINS / "astrbot_plugin_baige"
            if not prev.is_dir():
                continue
            try:
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(prev, dst)
                shutil.rmtree(prev, ignore_errors=True)
                restored.append(name)
            except Exception as e:
                logger.warning(f"[health] 回滚 {name} 失败: {e}")
        if not restored:
            return
        logger.warning(f"[health] 已回滚：{restored}，准备重启核心")
        await self._notify("插件异常，已自动回滚：" + "、".join(restored))
        try:
            import aiohttp

            token = Path("/stage/.panel_token").read_text(encoding="utf-8").strip()
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    "http://127.0.0.1:6185/api/v1/system/restart",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
        except Exception as e:
            logger.warning(f"[health] 自动重启失败（手动重启即可生效）: {e}")

    async def _tick(self):
        problems = await asyncio.to_thread(self._checks)
        if AUTO_ROLLBACK:
            await self._rollback(problems)
        now = int(time.time())
        fresh = []
        for p in problems:
            key = p.split("：")[0][:40]
            if now - int(self._state.get(key, 0)) > COOLDOWN:
                fresh.append(p)
                self._state[key] = now
        self._save()

        lines = [
            "# 白鸽·体检 "
            + time.strftime("%F %T", time.localtime(time.time() + 8 * 3600)),
            "",
        ]
        lines.append("全部正常 ✓" if not problems else f"发现 {len(problems)} 项问题：")
        lines += [f"- {p}" for p in problems]
        try:
            REPORT.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

        if fresh:
            await self._notify("体检发现问题：\n" + "\n".join(f"· {p}" for p in fresh))

    async def _loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[health] 异常: {e}")
            await asyncio.sleep(INTERVAL)

    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[health] 已启动，每 10 分钟体检，异常私聊 {settings.owner_qq()}")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[health] 已停止")
