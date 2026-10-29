# -*- coding: utf-8 -*-
"""白鸽·自更新（dsh_updater）

⚠️ 安全默认：**关闭**。公开安装时这个组件根本不会启动，因为「从外部可写目录
整体替换 data/plugins 下的代码」等价于本地热安装任意插件。只有在你自己的机器上、
面板里显式打开「允许从 _updates 自动同步」之后才会工作。

打开后规则：
  - 只处理 bridge_dir/_updates 下的一级子目录；
  - 目录名必须在白名单（`auto_update_allow`，逗号分隔）里，默认只有本插件；
  - 版本号取自子目录的 VERSION 文件（没有就取 metadata.yaml 的 version）；
  - 与已安装目录的 VERSION 不一致才覆盖，覆盖前把旧版备份到本插件数据目录。
"""
import asyncio
import shutil
import time
from pathlib import Path

from . import settings
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

SRC = lambda: settings.bridge_dir() / "_updates"  # noqa: E731  仅在打开开关后使用
DST = settings._DATA / "plugins"
INTERVAL = 60
DEFAULT_ALLOW = "astrbot_plugin_baige"


def _allow_list() -> set:
    raw = str(settings._c("auto_update_allow") or DEFAULT_ALLOW)
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


def _backup_dir():
    d = settings.backups_dir() / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
        src_root = SRC()
        if not src_root.is_dir():
            return
        allow = _allow_list()
        for src in sorted(src_root.iterdir()):
            if not src.is_dir() or not src.name.startswith("astrbot_plugin_"):
                continue
            if src.name not in allow:
                continue
            new_v = _version(src)
            dst = DST / src.name
            old_v = _version(dst) if dst.is_dir() else ""
            if dst.is_dir() and new_v and new_v == old_v:
                continue
            prev = _backup_dir() / src.name
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
                    (src_root / ".last").write_text(
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
        if not settings.auto_update_enabled():
            logger.warning(
                "[updater] 自动更新已关闭（默认关）。它会在不确认的情况下整体替换"
                " data/plugins 下的插件代码，属于危险操作；"
                "确需使用请在面板里打开「允许从 _updates 自动同步」，并保持白名单只含本插件。"
            )
            return
        self._task = asyncio.create_task(self._loop())
        logger.warning(
            f"[updater] 自动更新已打开：每 60 秒检查 {SRC()}，白名单 {sorted(_allow_list())}"
        )

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[updater] 已停止")
