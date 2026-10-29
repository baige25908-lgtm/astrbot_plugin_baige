# -*- coding: utf-8 -*-
"""白鸽·自更新（dsh_updater）

作用：把「外部能写的目录」(/stage/_updates，对应手机上的
/sdcard/白鸽宝宝/astrbot-setup/_updates) 里的插件版本，自动同步到
data/plugins，省掉每次手动跑安装脚本。

规则：
  - 只处理 /stage/_updates 下的一级子目录；
  - 版本号取自子目录的 VERSION 文件（没有就取 metadata.yaml 的 version）；
  - 与已安装目录的 VERSION 不一致才覆盖；
  - 目录名必须以 astrbot_plugin_ 开头，避免乱写。
"""
import asyncio
import shutil
import time
from pathlib import Path

from . import settings
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

SRC = settings.bridge_dir() / "_updates"
DST = settings._DATA / "plugins"
BACKUP = settings.PLUGIN_DATA.parent / "dsh_backups"
INTERVAL = 60


def _compile_check(d: Path):
    """编译目录下所有 .py，返回第一个出错的文件名（全通过返回空串）。"""
    for f in d.rglob("*.py"):
        try:
            compile(f.read_text(encoding="utf-8", errors="ignore"), str(f), "exec")
        except Exception:
            return f.name
    return ""


def _version(d: Path) -> str:
    v = d / "VERSION"
    if v.is_file():
        try:
            return v.read_text(encoding="utf-8").strip()[:40]
        except Exception:
            pass
    m = d / "metadata.yaml"
    if m.is_file():
        try:
            for line in m.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("version:"):
                    return line.split(":", 1)[1].strip()[:40]
        except Exception:
            pass
    return ""


class Component:
    def __init__(self, context: Context):
        self.context = context
        self._task = None

    async def _sync_once(self):
        if not SRC.is_dir():
            return
        for src in sorted(SRC.iterdir()):
            if not src.is_dir() or not src.name.startswith("astrbot_plugin_"):
                continue
            new_v = _version(src)
            dst = DST / src.name
            old_v = _version(dst) if dst.is_dir() else ""
            if dst.is_dir() and new_v and new_v == old_v:
                continue
            BACKUP.mkdir(parents=True, exist_ok=True)
            prev = BACKUP / src.name
            try:
                # 先留一份旧版，坏了能回滚
                if dst.is_dir():
                    if prev.exists():
                        shutil.rmtree(prev)
                    shutil.copytree(dst, prev)

                tmp = DST / (src.name + ".new")
                if tmp.exists():
                    shutil.rmtree(tmp)
                shutil.copytree(src, tmp)
                pyc = tmp / "__pycache__"
                if pyc.is_dir():
                    shutil.rmtree(pyc)

                bad = _compile_check(tmp)
                if bad:
                    shutil.rmtree(tmp, ignore_errors=True)
                    logger.error(f"[updater] {src.name} 新版本编译失败({bad})，已放弃，保留旧版")
                    continue

                if dst.exists():
                    shutil.rmtree(dst)
                tmp.rename(dst)
                logger.info(f"[updater] 已同步 {src.name} {old_v or '-'} -> {new_v or '-'}")
                try:
                    (SRC / ".last").write_text(
                        f"{time.strftime('%F %T')} {src.name} {new_v}", encoding="utf-8"
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[updater] 同步 {src.name} 失败: {e}")

    def _beat(self):
        try:
            d = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "updater"
            d.mkdir(parents=True, exist_ok=True)
            (d / "heartbeat").write_text(str(int(time.time())))
        except Exception:
            pass

    async def _loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                self._beat()
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[updater] 异常: {e}")
            await asyncio.sleep(INTERVAL)

    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("[updater] 已启动，每 60 秒检查 /stage/_updates")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[updater] 已停止")
