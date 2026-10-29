# -*- coding: utf-8 -*-
"""白鸽·运维（dsh_ops）

只用 /stage/ops/ 下的 flag 文件触发，白名单命令：
  restart-qq       → 重启 QQ 进程（NapCat 会重新生成二维码）
  restart-astrbot  → 调面板 API 软重启核心
  qr-status        → 把 NapCat 登录状态写到 /stage/qr-status.txt

所有动作写 /stage/ops.log，flag 处理完即删。
"""
import asyncio
import json
import os
import signal
import time
from pathlib import Path

import aiohttp

from . import settings
from astrbot.api import logger
from astrbot.api.star import Context, Star, register

OPS = settings.bridge_dir() / "ops"
LOG = settings.bridge_dir() / "ops.log"
TOKEN = settings.bridge_dir() / ".panel_token"
QQ_MARK = "qq -q " + (settings.qq_account() or "")


def _log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%F %T')}] {msg}\n")
    except Exception:
        pass
    logger.info(f"[dsh_ops] {msg}")


def _find_qq_pids():
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            continue
        if QQ_MARK in cmd and "dsh_ops" not in cmd:
            pids.append(int(d.name))
    return pids


class Component:
    def __init__(self, context: Context):
        self.context = context
        self._task = None

    async def _restart_qq(self):
        pids = _find_qq_pids()
        if not pids:
            _log("没找到 QQ 进程，跳过")
            return
        _log(f"重启 QQ：SIGTERM -> {pids}")
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except Exception as e:
                _log(f"kill {p} 失败: {e}")
        await asyncio.sleep(8)
        left = _find_qq_pids()
        if left:
            _log(f"仍在运行，SIGKILL -> {left}")
            for p in left:
                try:
                    os.kill(p, signal.SIGKILL)
                except Exception:
                    pass
        else:
            _log("QQ 已退出（ab-watch 会在 1 分钟内拉起并重新出码）")

    async def _restart_core(self):
        try:
            tok = TOKEN.read_text(encoding="utf-8").strip()
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "http://127.0.0.1:6185/api/v1/system/restart",
                    headers={"Authorization": f"Bearer {tok}"},
                    timeout=15,
                ) as r:
                    _log(f"核心重启请求已发送 HTTP {r.status}")
        except Exception as e:
            _log(f"核心重启失败: {e}")

    async def _qr_status(self):
        out = {"ts": int(time.time()), "qq_pids": _find_qq_pids()}
        qr = Path("/stage/qrcode.png")
        out["qrcode_mtime"] = int(qr.stat().st_mtime) if qr.is_file() else 0
        out["qrcode_size"] = qr.stat().st_size if qr.is_file() else 0
        try:
            Path("/stage/qr-status.txt").write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            _log(f"登录状态已导出: {out}")
        except Exception as e:
            _log(f"导出失败: {e}")

    async def _tick(self):
        if not OPS.is_dir():
            return
        for flag in sorted(OPS.glob("*.flag")):
            name = flag.stem
            try:
                flag.unlink()
            except Exception:
                pass
            if name == "restart-qq":
                await self._restart_qq()
            elif name == "restart-astrbot":
                await self._restart_core()
            elif name == "qr-status":
                await self._qr_status()
            else:
                _log(f"未知指令（已忽略）: {name}")

    async def _loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"巡检异常: {e}")
            await asyncio.sleep(15)

    async def initialize(self):
        OPS.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop())
        logger.info("[dsh_ops] 已启动，监听 /stage/ops/*.flag")

    async def terminate(self):
        if self._task:
            self._task.cancel()
