# -*- coding: utf-8 -*-
"""白鸽·运维（dsh_ops）

⚠️ 安全默认：**关闭**。这些动作（SIGTERM/SIGKILL QQ 进程、用面板 token
重启核心）只适合作者自己的容器部署；公开安装时组件不启动。

打开方式：面板 → 安全与运维开关 → 允许运维 flag 通道。

打开后用 <bridge_dir>/ops/*.flag 触发，白名单只有三条：
  restart-qq       → 重启 QQ 进程（NapCat 会重新生成二维码）
  restart-astrbot  → 调面板 API 软重启核心
  qr-status        → 把 QQ 进程/二维码状态写到 <bridge_dir>/qr-status.txt

所有动作写 <bridge_dir>/ops.log，flag 处理完即删。
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
from astrbot.api.star import Context

QQ_MARK = "qq -q " + (settings.qq_account() or "")


def _ops_dir() -> Path:
    return settings.bridge_dir() / "ops"


def _log_path() -> Path:
    return settings.bridge_dir() / "ops.log"


def _token_path() -> Path:
    return settings.bridge_dir() / ".panel_token"


def _log(msg):
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
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
            tok = _token_path().read_text(encoding="utf-8").strip()
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
        qr = settings.bridge_dir() / "qrcode.png"
        out["qrcode_mtime"] = int(qr.stat().st_mtime) if qr.is_file() else 0
        out["qrcode_size"] = qr.stat().st_size if qr.is_file() else 0
        try:
            (settings.bridge_dir() / "qr-status.txt").write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            _log(f"登录状态已导出: {out}")
        except Exception as e:
            _log(f"导出失败: {e}")

    async def _tick(self):
        d = _ops_dir()
        if not d.is_dir():
            return
        for flag in sorted(d.glob("*.flag")):
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
        if not settings.ops_enabled():
            logger.warning(
                "[dsh_ops] 运维 flag 通道已关闭（默认关）。它能结束 QQ 进程、"
                "用面板 token 重启核心，只适合自用容器部署；"
                "确需使用请在面板里打开「允许运维 flag 通道」。"
            )
            return
        _ops_dir().mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop())
        logger.warning(f"[dsh_ops] 运维通道已打开，监听 {_ops_dir()}/*.flag")

    async def terminate(self):
        if self._task:
            self._task.cancel()
