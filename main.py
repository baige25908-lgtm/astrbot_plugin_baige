# -*- coding: utf-8 -*-
"""白鸽·一体（baige）

把原来 8 个插件合成一个：
  vibe    学梗 / 复盘 / 台词 / 注入
  media   45% 情绪配图 + 收图扩库 + 同步天使之笑
  health  10 分钟体检 + 心跳 + 版本哨兵
  meter   token 账本
  balance /余额 查询 + 余额落到 /stage/balance.json
  attach  按回复速度自适应主动频率
  ops     /stage/ops/*.flag 受控运维
  updater 每 60 秒从 /stage/_updates 自动同步插件

数据目录：data/plugin_data/astrbot_plugin_baige/<component>/
首次启动会把旧插件目录里的数据搬过来（.migrated 标记，只做一次）。
"""
import shutil
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from . import settings

from . import attach as _attach
from . import fish as _fish
from . import balance as _balance
from . import health as _health
from . import media as _media
from . import meter as _meter
from . import ops as _ops
from . import updater as _updater
from . import vibe as _vibe
from .fish import P_AFTER, P_ARCHIVE, P_CMD, P_GATE, P_HISTORY, P_MUTE, P_MUTE_RESULT, P_PACE_RESULT, P_SILENT

PDATA = settings.PLUGIN_DATA.parent
# 组件 -> (旧插件目录, 需要搬的文件/目录)
LEGACY = {
    "vibe": ("astrbot_plugin_dsh_vibe", ["vibe.db"]),
    "media": (
        "astrbot_plugin_dsh_media",
        ["library", "used.json", "index.json", ".seeded_smile_v2"],
    ),
    "health": ("astrbot_plugin_dsh_health", ["alert_state.json"]),
    "meter": ("astrbot_plugin_dsh_meter", ["meter.db", "extra_offset.txt"]),
    "balance": ("astrbot_plugin_dsh_balance", ["state.json"]),
    "attach": ("astrbot_plugin_dsh_attach", ["state.json"]),
}


OLD_MERGED = PDATA / "astrbot_plugin_dsh_all"


def _migrate_merged(base: Path) -> None:
    """从上一版合并插件（dsh_all）的目录整体搬过来。"""
    if not OLD_MERGED.is_dir() or OLD_MERGED == base:
        return
    try:
        shutil.copytree(OLD_MERGED, base, dirs_exist_ok=True)
        logger.info(f"[baige] 已从 {OLD_MERGED.name} 迁移数据")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[baige] 迁移旧合并目录失败: {e}")


def _migrate(base: Path) -> None:
    marker = base / ".migrated"
    if marker.is_file():
        return
    moved = 0
    for name, (old, items) in LEGACY.items():
        src_root = PDATA / old
        if not src_root.is_dir():
            continue
        dst_root = base / name
        dst_root.mkdir(parents=True, exist_ok=True)
        for it in items:
            src, dst = src_root / it, dst_root / it
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    moved += 1
                elif src.is_file() and not dst.exists():
                    shutil.copy2(src, dst)
                    moved += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[baige] 迁移 {old}/{it} 失败: {e}")
    try:
        marker.write_text("1", encoding="utf-8")
    except Exception:
        pass
    logger.info(f"[baige] 旧插件数据已迁移 {moved} 项")


@register(
    "baige_legacy",
    "白鸽",
    "白鸽一体：学梗/配图/体检/账本/余额/分寸/运维/自更新",
    "1.0.0",
)
class DshAll(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # AstrBot 会把本插件的面板配置以 config 传进来；拿不到就退回空配置。
        self.config = config
        try:
            filled = settings.prefill_from_fish(config) if config is not None else 0
            if filled:
                try:
                    config.save_config()
                    logger.info(f"[baige] 面板配置回填 {filled} 项（原为空，取自 fish 配置）")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[baige] 回填保存失败: {e}")
            settings.set_plugin_config(dict(config) if config else {})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[baige] 读取面板配置失败: {e}")
            settings.set_plugin_config({})
        self.base = Path(StarTools.get_data_dir("astrbot_plugin_baige"))
        self.base.mkdir(parents=True, exist_ok=True)
        _migrate_merged(self.base)
        _migrate(self.base)

        self.vibe = _vibe.Component(context)
        self.media = _media.Component(context)
        self.health = _health.Component(context)
        self.meter = _meter.Component(context)
        self.balance = _balance.Component(context)
        self.attach = _attach.Component(context)
        self.ops = _ops.Component(context)
        self.updater = _updater.Component(context)
        self.fish = _fish.Component(context)
        self._parts = [
            ("vibe", self.vibe),
            ("media", self.media),
            ("health", self.health),
            ("meter", self.meter),
            ("balance", self.balance),
            ("attach", self.attach),
            ("ops", self.ops),
            ("updater", self.updater),
            ("fish", self.fish),
        ]

    def _retire_old_plugins(self):
        """合并完成后，把旧的 8 个插件目录删掉（只做一次，旧版本还在 dsh_backups 里）。"""
        flag = self.base / ".legacy_removed_v3"
        if flag.is_file():
            return
        plugins = settings._DATA / "plugins"
        removed = []
        for old, _ in LEGACY.values():
            d = plugins / old
            if d.is_dir():
                try:
                    shutil.rmtree(d)
                    removed.append(old)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[baige] 删除旧插件 {old} 失败: {e}")
        for extra in (
            "astrbot_plugin_dsh_ops",
            "astrbot_plugin_dsh_updater",
            "astrbot_plugin_dsh_all",
            "fish_core",
        ):
            d = plugins / extra
            if d.is_dir() and extra not in removed:
                try:
                    shutil.rmtree(d)
                    removed.append(extra)
                except Exception:
                    pass
        try:
            flag.write_text("1", encoding="utf-8")
        except Exception:
            pass
        if removed:
            logger.info(f"[baige] 已停用旧插件：{', '.join(removed)}（下次重启不再加载）")

    async def initialize(self):
        self._retire_old_plugins()
        for name, part in self._parts:
            try:
                await part.initialize()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[baige] 初始化 {name} 失败: {e}", exc_info=True)
        logger.info("[baige] 全部组件已启动")

    async def terminate(self):
        for name, part in reversed(self._parts):
            try:
                await part.terminate()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[baige] 停止 {name} 失败: {e}")
        logger.info("[baige] 已停止")

    # ---------- fish（唤醒闸门/节流/静默/归档/指令/补拉）----------
    @filter.on_waiting_llm_request(priority=P_GATE)
    async def _fish_gate(self, event: AstrMessageEvent):
        await self.fish.on_gate(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=P_ARCHIVE)
    async def _fish_archive(self, event: AstrMessageEvent):
        await self.fish.on_archive(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=P_CMD)
    async def _fish_cmd(self, event: AstrMessageEvent):
        await self.fish.on_owner_cmd(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=P_MUTE)
    async def _fish_mute(self, event: AstrMessageEvent):
        await self.fish.on_mute(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=P_SILENT)
    async def _fish_silent(self, event: AstrMessageEvent):
        await self.fish.on_group_silent(event)

    @filter.after_message_sent(priority=P_AFTER)
    async def _fish_after(self, event: AstrMessageEvent):
        await self.fish.on_archive_sent(event)

    @filter.on_decorating_result(priority=P_MUTE_RESULT)
    async def _fish_mute_result(self, event: AstrMessageEvent):
        await self.fish.on_mute_result(event)

    @filter.on_decorating_result(priority=P_PACE_RESULT)
    async def _fish_pace_result(self, event: AstrMessageEvent):
        await self.fish.on_pace_result(event)

    @filter.on_llm_request(priority=P_HISTORY)
    async def _fish_history(self, event: AstrMessageEvent, request: ProviderRequest):
        await self.fish.on_trim_group_history(event, request)

    # ---------- 钩子 ----------
    @filter.on_llm_request()
    async def _inject_vibe(self, event: AstrMessageEvent, request: ProviderRequest):
        await self.vibe.inject_vibe(event, request)

    @filter.on_llm_response()
    async def _meter(self, event: AstrMessageEvent, resp):
        await self.meter.on_resp(event, resp)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=270)
    async def _on_message(self, event: AstrMessageEvent):
        await self.media.steal_images(event)
        await self.attach.on_user_msg(event)

    @filter.on_decorating_result(priority=250)
    async def _decorate(self, event: AstrMessageEvent):
        await self.media.attach_emoji(event)
        await self.attach.mark_bot_spoke(event)

    @filter.command("余额", alias={"查余额", "balance", "剩多少"})
    async def _balance_cmd(self, event: AstrMessageEvent):
        async for ret in self.balance.balance_cmd(event):
            yield ret
