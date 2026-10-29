# -*- coding: utf-8 -*-
"""统一路径与身份来源，避免在代码里写死某台机器的路径和 QQ 号。

优先级：
  1) 插件配置（WebUI 里改，见 _conf_schema.json）
  2) 环境变量（BAIGE_BRIDGE / BAIGE_QQ_ACCOUNT / BAIGE_OWNER_QQ）
  3) fish 配置里的 owner_ids（沿用群管配置）
  4) 兜底默认值
"""
import json
import os
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_baige"
_DATA = Path(get_astrbot_data_path())
ROOT = _DATA.parent                      # 一般是 AstrBot 安装根目录
PLUGIN_DATA = _DATA / "plugin_data" / PLUGIN_NAME
FISH_CONFIG = _DATA / "config" / "fish_core_config.json"
CMD_CONFIG = _DATA / "cmd_config.json"
CHATLOG_DIR = _DATA / "chatlog"

_cfg = {}


def set_plugin_config(cfg: dict | None) -> None:
    """由 main.py 在启动时注入插件配置。"""
    global _cfg
    _cfg = dict(cfg or {})


def _c(key, default=None):
    return _cfg.get(key, default)


def bridge_dir() -> Path:
    """/stage 这类桥接目录（对外写报表、收 flag 用）。"""
    raw = _c("bridge_dir") or os.environ.get("BAIGE_BRIDGE") or "/stage"
    return Path(raw)


def report(name: str) -> Path:
    return bridge_dir() / name


def owner_qq() -> int:
    """主人 QQ：插件配置 > 环境变量 > fish 配置 owner_ids > 0。"""
    v = _c("owner_qq") or os.environ.get("BAIGE_OWNER_QQ")
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    try:
        cfg = json.loads(FISH_CONFIG.read_text(encoding="utf-8-sig"))
        raw = str(cfg.get("owner_ids") or "")
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                return int(part)
    except Exception:
        pass
    return 0


def qq_account() -> str:
    """机器人自己的 QQ（给运维通道识别进程用）。"""
    return str(_c("qq_account") or os.environ.get("BAIGE_QQ_ACCOUNT") or "")


def owner_umo() -> str:
    q = owner_qq()
    return f"qq_main:FriendMessage:{q}" if q else ""


def image_probability(default: float = 0.45) -> float:
    try:
        return float(_c("image_probability", default))
    except (TypeError, ValueError):
        return default


def pause_threshold(default: float = 0.5) -> float:
    try:
        return float(_c("balance_pause_threshold", default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 面板配置 → fish 配置合并
# 面板里留空的字符串视为“没设置”，自动回退到 fish_core_config.json / 内置默认；
# 数字和布尔一律以面板为准（schema 里的默认值就是当前生效值）。
# --------------------------------------------------------------------------
def _section_items(v):
    """如果是“分组”，返回它的子项 dict；是叶子则返回 None。

    面板配置树里只要出现 dict 就是分组；schema 里的叶子会带 type/default。
    """
    if not isinstance(v, dict):
        return None
    items = v.get("items")
    if isinstance(items, dict):
        return items
    if "type" in v or "default" in v:
        return None
    return v


def _flatten(node: dict, out: dict) -> None:
    for k, v in node.items():
        sub = _section_items(v)
        if sub is None:
            out[k] = v
        else:
            _flatten(sub, out)


_SCHEMA_CACHE = None


def schema_keys() -> set:
    """_conf_schema.json 里的所有叶子键名，用来挡掉别的配置混进来。"""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    keys = set()
    try:
        raw = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
        stack = [raw]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            for k, v in node.items():
                sub = _section_items(v)
                if sub is None:
                    keys.add(k)
                else:
                    stack.append(sub)
    except Exception:
        keys = set()
    _SCHEMA_CACHE = keys
    return keys


def plugin_overrides() -> dict:
    """把面板配置拍平成一维，只返回显式设置过的项。"""
    flat = {}
    try:
        _flatten(_cfg, flat)
    except Exception:
        return {}
    allowed = schema_keys()
    out = {}
    for k, v in flat.items():
        if allowed and k not in allowed:
            continue
        if v is None:
            continue
        if isinstance(v, str):
            if not v.strip():
                continue
        out[k] = v
    return out
