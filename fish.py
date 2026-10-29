# -*- coding: utf-8 -*-
"""fish_core（v1.5.2）—— 把原来 4 个插件合成一个

合并自：
  group_pace   群聊节流 + 唤醒词闸门
  mute_switch  静默开关（「停一下」/「可以说话了」）
  chatlog      聊天记录归档（私聊全存、群聊抽样）
  owner_cmd    主人指令（@我 状态/重载/重启/部署/配置/撤回）

为什么合并：四个插件都在 on_waiting_llm_request / on_decorating_result 上抢位置，
优先级相同或相近时执行顺序不确定；合成一个插件后顺序由**代码写死**，可预期。

执行顺序（数值越大越先）：
  事件阶段        归档 1e8 → 主人指令 1e8-1 → 静默开关 1e8-2
                  静默时你仍能用 @我 指令（主人指令排在静默前面）
  调模型前        唤醒闸门 + 节流 1e8（拦下就不产生任何 LLM 开销）
  发送前          静默拦截 1e8 → 节流兜底 1e8-1
  发送后          归档机器人回复 1e8（只读，不干扰）
"""

import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from . import settings

from astrbot.api import logger, star
from astrbot.api.event import filter
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "astrbot_plugin_baige"
STATE_SUB = "fish"

# 合并后配置与旧状态文件的位置（配置沿用原来的 fish_core_config.json）
CONFIG_FILE = settings.FISH_CONFIG
LEGACY_STATE = settings.PLUGIN_DATA.parent / "fish_core"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fish] 读取配置失败，用默认值: {e}")
        return {}


def _migrate_state(target: str) -> None:
    """把旧 fish_core 的状态文件搬进新目录（只搬缺的）。"""
    try:
        dst = Path(target)
        dst.mkdir(parents=True, exist_ok=True)
        if not LEGACY_STATE.is_dir():
            return
        for f in LEGACY_STATE.iterdir():
            if f.is_file():
                d = dst / f.name
                if not d.exists():
                    shutil.copy2(f, d)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fish] 迁移状态失败: {e}")


def _astrbot_paths() -> tuple:
    """拿到 AstrBot 的 data / plugins 目录；拿不到就退回插件自身目录。"""
    data_dir = PLUGIN_DIR
    plugins_dir = ""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = get_astrbot_data_path()
    except Exception:
        data_dir = os.path.dirname(PLUGIN_DIR)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

        plugins_dir = get_astrbot_plugin_path()
    except Exception:
        plugins_dir = ""
    return data_dir, plugins_dir


PLUGINS_DIR = _astrbot_paths()[1]


def _state_dir() -> str:
    """插件自己的数据目录（升级插件不会丢）。"""
    try:
        from astrbot.core.star.star_tools import StarTools

        return str(Path(StarTools.get_data_dir(PLUGIN_NAME)) / STATE_SUB)
    except Exception:
        return PLUGIN_DIR


HIGH = 10**8
P_ARCHIVE = HIGH
P_CMD = HIGH - 1
P_MUTE = HIGH - 2
P_GATE = HIGH
P_MUTE_RESULT = HIGH
P_PACE_RESULT = HIGH - 1
P_SILENT = HIGH - 3
P_AFTER = HIGH
# on_llm_request 的优先级是「数字越大越先跑」，别的插件用的是 40/50。
# 历史裁剪要放在最后，等它们把记忆块之类的注入都加完了再动手。
P_HISTORY = -50

# 补拉循环的全局登记表：插件热重载会新建模块与实例，普通模块变量会跟着重置，
# 所以存在 builtins 上——这样旧实例的循环能被新实例取消，不会两个循环一起提醒。
import builtins

_CATCHUP = getattr(builtins, "_fish_core_catchup", None)
if _CATCHUP is None:
    _CATCHUP = {"inst": None, "task": None}
    builtins._fish_core_catchup = _CATCHUP

DEFAULT_MUTE_WORDS = tuple('停一下,停下,停停,闭嘴,闭嘴吧,住口,安静,安静点,安静一下,静一下,静一静,消停,消停点,别说了,别说话,别出声,不许说话,不许出声,别吵,别吵了,别叫,别嚷嚷,别叭叭,少说两句,打住,收声,噤声,别插嘴'.split(","))
# 注意：不要往这个表里放「好了」这种超高频词——「那说好了，不可以离开我」曾经
# 因为含「好了」被当成解除指令，机器人就只会回一句「我回来了，继续吧。」
DEFAULT_UNMUTE_WORDS = tuple('可以说话了,可以说了,可以出声了,继续说话,继续说,继续吧,继续聊,说话吧,说吧,回来吧,出来吧,出来说话,解除静默,取消静默,恢复说话,别装了'.split(","))

# ---- 情境驱动语音：借 mimo TTS 的会话概率做开关 ----
# mimo 每轮从自己的 studio.sqlite3 里读 auto_tts_probability，
# 这里在调模型前改写它：命中情绪词 → 1.0（必发），否则 → 0.0（不发）。

DEFAULT_TTS_CTX_WORDS = (
    "难受", "累了", "好累", "难过", "不开心", "烦", "委屈", "想你", "想我",
    "抱", "疼", "痛", "哭", "怕", "睡不着", "失眠", "压力", "焦虑", "崩溃",
    "孤独", "寂寞", "晚安", "早安", "爱你", "喜欢", "陪我", "对不起", "抱歉",
    "发烧", "感冒", "头疼", "胃疼", "生病", "医院", "打扰",
)

ALLOWED_CONFIG = {
    "max_turns": ("agent_runner", "config", "compression", "max_turns", int),
    "enable_id_white_list": ("platform_settings", "enable_id_white_list", bool),
}


# ================= 智能插话：判断这句群消息值不值得接 =================
_QUESTION_WORDS = (
    "怎么", "为什么", "为啥", "如何", "是不是", "有没有", "能不能", "要不要",
    "行不行", "会不会", "哪个", "哪儿", "哪里", "谁", "啥", "什么", "多少",
    "几点", "请问", "求问", "有人知道", "有懂", "咋",
)
# 整句都是标点 / emoji / 空白 → 噪声
_NOISE_ONLY = re.compile(r"^[^\w\u4e00-\u9fff]+$")
_REPEAT_WORDS = (
    "哈哈哈", "哈哈", "嘿嘿", "呵呵", "草", "艹", "666", "笑死", "确实",
    "好的", "收到", "嗯嗯", "对对对", "行", "绝了",
)


def _bigrams(text: str) -> set:
    """把一句话压成字符二元组集合，用来做零依赖的相似度。"""
    t = re.sub(r"[\s\W_]+", "", str(text).lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _words(raw, default):
    if isinstance(raw, str) and raw.strip():
        out = tuple(x.strip() for x in raw.replace("，", ",").split(",") if x.strip())
        if out:
            return out
    elif isinstance(raw, (list, tuple)) and raw:
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return default


_CMD_NOISE = set(
    " \t\r\n，,。.！!？?、；;：:~～…—-_·'\"“”‘’()（）[]【】<>《》*~"
)


def _norm_cmd(text) -> str:
    """去掉标点空白，只留下判断命令用的字符序列。"""
    return "".join(ch for ch in str(text or "") if ch not in _CMD_NOISE)


_TAIL_OK = "了吧啊呀嘛哦呢啦呗哈的点儿一下"


def _cmd_hit_strict(core: str, words) -> bool:
    """更严的命令匹配：整句就是该词，或只多出语气词（了/吧/啊/一下…）。

    用来判断静默这类「说错一句要安静十分钟」的指令，
    「别吵了，我就说说而已」这种带后半句的不算。
    """
    if not core:
        return False
    for w in words:
        nw = _norm_cmd(w)
        if not nw:
            continue
        if core == nw:
            return True
        if core.startswith(nw):
            rest = core[len(nw):]
            if len(rest) <= 3 and all(ch in _TAIL_OK for ch in rest):
                return True
    return False


def _cmd_hit(core: str, words) -> bool:
    """命令式匹配：整句就是该词，或以该词开头。

    以前用的是 `word in text`，于是「那说好了，不可以离开我」命中「好了」、
    「这里挺安静的」命中「安静」，把正常聊天当成命令处理。
    """
    if not core:
        return False
    for w in words:
        nw = _norm_cmd(w)
        if nw and (core == nw or core.startswith(nw)):
            return True
    return False


def _num(raw, default, cast=float):
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# ---- 群聊历史裁剪 ----
# AstrBot 存进会话历史的消息，开头会带发送者标记，两种写法都带 "ID: 数字"：
#   [群友: 东的鲸宝 (ID: 245788349, 群友)] (2026-10-27 16:33): ...
#   <system_reminder>User ID: 10001, Nickname: 示例 ...
# 用它识别「这句是谁说的」，只看历史消息，不碰别的插件注入的内容。
_SENDER_RE = re.compile(r"ID[：:]?\s*(\d{5,12})")


def _ctx_text(item) -> str:
    """把一条上下文消息压成纯文本，用来判断发送者和内容。"""
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                # think 不进正文；图片等占位符没有信息量
                if part.get("type") in ("text", None):
                    out.append(str(part.get("text") or ""))
        return "\n".join(p for p in out if p)
    return str(content or "")


class Component:
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.config = _load_config()

        self.state_dir = _state_dir()
        _migrate_state(self.state_dir)
        self.pace_file = os.path.join(self.state_dir, "pace_last.json")
        self.mute_file = os.path.join(self.state_dir, "muted.json")
        self.catchup_file = os.path.join(self.state_dir, "catchup.json")
        self.silent_file = os.path.join(self.state_dir, "silent.json")
        self._tts_studio_db = str(self.config.get("tts_studio_db") or "").strip()
        self._deploy_stage = str(self.config.get("deploy_stage_dir") or "").strip()

        # ---------------- 节流 + 唤醒 ----------------
        self.min_seconds = _num(self.config.get("pace_min_seconds"), 120.0)
        # 冷却随机浮动：不让「时间一到下一条必回」变成机械节拍
        self.pace_jitter = max(0.0, min(0.9, _num(self.config.get("pace_jitter"), 0.35)))
        # 命中后先「打一会儿字」再回，别像自动机
        self.reply_delay_max = max(0.0, _num(self.config.get("reply_delay_max"), 3.0))
        # 私聊单独一个上限：私聊等太久只会显得卡
        self.reply_delay_private_max = max(
            0.0, _num(self.config.get("reply_delay_private_max"), 1.0)
        )
        self.global_seconds = _num(self.config.get("pace_global_seconds"), 30.0)
        self.pace_only_group = bool(self.config.get("pace_only_group", True))
        self.exempt_wake = bool(self.config.get("exempt_wake", False))
        self.wake_required = bool(self.config.get("wake_required_for_owner", True))
        self.wake_owners_only = bool(self.config.get("wake_owners_only", True))
        self.wake_on_at = bool(self.config.get("wake_on_at", True))
        self.wake_words = list(
            _words(self.config.get("wake_words"), ())
        )
        self.owners = set(
            _words(self.config.get("owner_ids"), ())
        )
        self.pace_last = self._load_json(self.pace_file, {})

        # ---------------- 静默开关 ----------------
        self.mute_words = _words(self.config.get("mute_words"), DEFAULT_MUTE_WORDS)
        self.unmute_words = _words(self.config.get("unmute_words"), DEFAULT_UNMUTE_WORDS)
        self.mute_minutes = max(0.0, _num(self.config.get("mute_minutes"), 10.0))
        # 静默只对群友生效：主人 @我 或说唤醒词时仍然回复，方便随时找回控制权
        self.mute_owner_can_wake = bool(self.config.get("mute_owner_can_wake", True))
        # 静默只在群聊里生效：私聊是主人自己的对话框，说「闭嘴」也照常聊
        self.mute_only_group = bool(self.config.get("mute_only_group", True))
        # ---------------- 唤醒延长 + 连续消息合并 ----------------
        # 被叫到之后的一段时间内，同一个人接着说都算在跟它说话
        self.wake_window = max(0, _num(self.config.get("wake_window_seconds"), 60, int))
        # 这段窗口里同一人的连续消息，合并成一条再交给模型
        self.merge_window_ms = max(0, _num(self.config.get("merge_window_ms"), 2500, int))
        # 私聊是否也走合并等待；关掉能让私聊少等一整个窗口
        self.merge_private = bool(self.config.get("merge_private", False))
        self._wake_until = {}
        self._merge = {}

        # ---------------- 智能插话 ----------------
        self.smart_enabled = bool(self.config.get("smart_reply_enabled", True))
        self.smart_threshold = max(
            0.0, min(1.0, _num(self.config.get("smart_reply_threshold"), 0.25))
        )
        self.smart_interest = list(
            _words(self.config.get("smart_interest_keywords"), ())
        )
        self.w_question = _num(self.config.get("smart_w_question"), 0.40)
        self.w_relevant = _num(self.config.get("smart_w_relevant"), 0.40)
        self.w_interest = _num(self.config.get("smart_w_interest"), 0.30)
        self.w_bored = _num(self.config.get("smart_w_bored"), 0.05)
        self.w_noise = _num(self.config.get("smart_w_noise"), 0.50)
        self.smart_log_scores = bool(self.config.get("smart_log_scores", False))
        self._recent_bot = {}
        self._recent_msgs = {}
        self._last_smart_log = 0.0

        # ---------------- 群聊历史裁剪 ----------------
        # 群里的历史只留跟主人有关的那几轮，别人的闲聊不进上下文：
        # 省 token、跑得快，也免得它把群友的事记成自己的经历。
        self.gh_owner_only = bool(self.config.get("group_history_owner_only", False))
        self.gh_owner_names = list(
            _words(self.config.get("group_history_owner_names"), ())
        )
        # 末尾无条件保留条数：保证刚发生的事至少有个上下文，不至于接不上话
        self.gh_keep_recent = max(
            0, _num(self.config.get("group_history_keep_recent"), 2, int)
        )
        self._gh_log_at = 0.0

        # 群里 @ 了别人 / 引用回复别人的消息，没叫我就别插话
        self.skip_group_at_others = bool(self.config.get("skip_group_at_others", True))
        self.skip_group_quote_others = bool(
            self.config.get("skip_group_quote_others", True)
        )
        # 群聊禁言：默认限时，到点自动放开；0 分钟 = 一直禁言直到手动关掉
        self.group_silent = bool(self.config.get("group_silent", False))
        self.group_silent_minutes = max(
            0, _num(self.config.get("group_silent_minutes"), 10, int)
        )
        self._silent_log_at = 0.0
        self._gs_until = 0.0        # 0 = 未启用；inf = 永久
        self._gs_expired_logged = False
        if self.group_silent:
            if self.group_silent_minutes <= 0:
                self._gs_until = float("inf")
            else:
                st = self._load_json(self.silent_file, {})
                sig = "%s|%s" % (self.group_silent, self.group_silent_minutes)
                if st.get("sig") != sig or not st.get("start"):
                    start = time.time()
                    self._save_json(
                        self.silent_file, {"sig": sig, "start": start}
                    )
                else:
                    start = float(st["start"])
                self._gs_until = start + self.group_silent_minutes * 60
        else:
            # 关掉开关时记一笔，这样下次再打开能重新计时
            st = self._load_json(self.silent_file, {})
            if st.get("sig") != "off":
                self._save_json(self.silent_file, {"sig": "off", "start": 0})
        self.muted = self._load_muted()
        self._allow_once = set()

        # ---------------- 掉线补拉（QQ 重启那几秒的消息 AstrBot 收不到）----------------
        self.catchup_enabled = bool(self.config.get("catchup_enabled", True))
        self.catchup_interval = max(15, _num(self.config.get("catchup_interval"), 45, int))
        self.catchup_max = max(1, _num(self.config.get("catchup_max_missed"), 3, int))
        self.catchup = self._load_json(self.catchup_file, {})
        self._catchup_task = None

        # ---------------- 情境驱动语音 ----------------
        self.tts_ctx_enabled = bool(self.config.get("tts_context_enabled", True))
        self.tts_ctx_words = list(
            _words(self.config.get("tts_context_keywords"), DEFAULT_TTS_CTX_WORDS)
        )
        self.tts_ctx_fallback = max(
            0.0, min(1.0, _num(self.config.get("tts_context_fallback"), 0.0))
        )

        # ---------------- 归档 ----------------
        self.out_dir = str(
            self.config.get("archive_dir") or os.path.join(self.state_dir, "chatlog")
        )
        self.per_day = bool(self.config.get("archive_per_day", True))
        self.archive_private = bool(self.config.get("archive_private", True))
        self.archive_group = bool(self.config.get("archive_group", True))
        self.group_mode = str(self.config.get("archive_group_mode", "sampled")).lower()
        self.group_daily_cap = _num(self.config.get("archive_group_daily_cap"), 300, int)
        self.keep_senders = set(
            _words(self.config.get("archive_always_keep_senders"), ())
        )
        self.skip_empty = bool(self.config.get("archive_skip_empty", True))
        self._counters_file = os.path.join(self.out_dir, ".counters.json")
        self._counters = self._load_counters()
        self._last_bot = ("", 0.0)
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fish_core] 归档目录创建失败 {self.out_dir}: {e}")

        # ---------------- 主人指令 ----------------
        self.cmd_enabled = bool(self.config.get("cmd_enabled", True))
        self.cmd_owner = str(self.config.get("cmd_owner_qq") or "").strip()
        self.cmd_groups = _words(self.config.get("cmd_allow_groups"), ())
        self.recall_silent = bool(self.config.get("cmd_recall_silent", True))

        logger.info(
            "[fish_core] 已加载：节流 %.0fs/%.0fs，唤醒词=%s(@也算=%s)，静默=%d 个会话(%g 分钟)，"
            "延迟 %.1fs/%.1fs(群/私)，合并 %gs(私聊%s)，归档=%s，主人指令=%s(%s)"
            % (
                self.min_seconds,
                self.global_seconds,
                "/".join(self.wake_words),
                self.wake_on_at,
                len(self.muted),
                self.mute_minutes,
                self.reply_delay_max,
                self.reply_delay_private_max,
                self.merge_window_ms / 1000.0,
                "开" if self.merge_private else "关",
                self.out_dir,
                self.cmd_enabled,
                self.cmd_owner,
            )
        )

    # ============================================================ 通用工具
    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fish_core] 读取 {path} 失败: {e}")
            return default

    @staticmethod
    def _save_json(path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fish_core] 写入 {path} 失败: {e}")

    def _load_muted(self) -> dict:
        data = self._load_json(self.mute_file, {})
        now = time.time()
        out = {}
        if isinstance(data, list):
            for sid in data:
                if isinstance(sid, str):
                    out[sid] = now + self.mute_minutes * 60
        elif isinstance(data, dict):
            for sid, until in data.items():
                try:
                    until = float(until)
                except (TypeError, ValueError):
                    continue
                if until > now:
                    out[sid] = until
        return out

    @staticmethod
    def _text(event) -> str:
        try:
            return str(event.message_str or event.get_message_str() or "")
        except Exception:
            return ""

    # ---------------- 群聊历史裁剪 ----------------
    def _owner_marks(self) -> list:
        """判断「这条消息跟主人有关」用的标记：QQ 号和昵称。"""
        marks = [str(o).strip() for o in self.owners if str(o).strip()]
        marks.extend(str(n).strip() for n in self.gh_owner_names if str(n).strip())
        return marks

    @staticmethod
    def _is_injection(item) -> bool:
        """别的插件注入的内容（记忆块、笔记、灵魂状态），一律不动。"""
        if not isinstance(item, dict):
            return False
        if item.get("_no_save"):
            return True
        head = _ctx_text(item).lstrip()[:16]
        return head.startswith(
            ("<system_context>", "<memories>", "<notes>", "<user_profiles>")
        )

    def _trim_group_history(self, contexts: list):
        """只保留跟主人有关的历史轮次，返回 (新列表, 丢掉条数)。

        规则：
        - 历史里的「主人说话」和「提到主人的话」保留；
        - 紧跟这些话的回复保留，回复别人的一并丢掉；
        - 带工具调用的消息保留（否则后面的 tool 消息配对不上）；
        - 末尾 gh_keep_recent 条无条件保留，免得接不上刚发生的话。
        """
        marks = self._owner_marks()
        if not marks:
            return contexts, 0

        first = None
        for i, item in enumerate(contexts):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            if self._is_injection(item):
                continue
            if _SENDER_RE.search(_ctx_text(item)):
                first = i
                break
        if first is None:
            return contexts, 0

        total = len(contexts)
        tail_from = total
        if self.gh_keep_recent and total - self.gh_keep_recent > first:
            tail_from = total - self.gh_keep_recent

        out = list(contexts[:first])
        prev = "ctx"
        dropped = 0
        for i in range(first, total):
            item = contexts[i]
            if not isinstance(item, dict):
                out.append(item)
                prev = "ctx"
                continue
            role = item.get("role")
            if i >= tail_from:
                out.append(item)
                prev = "user" if role == "user" else ("ai" if role == "assistant" else "ctx")
                continue
            if role == "user":
                if self._is_injection(item):
                    out.append(item)
                    prev = "ctx"
                    continue
                text = _ctx_text(item)
                if _SENDER_RE.search(text):
                    if any(m in text for m in marks):
                        out.append(item)
                        prev = "user"
                    else:
                        dropped += 1
                        prev = "other"
                else:
                    out.append(item)
                    prev = "ctx"
            elif role == "assistant":
                if prev == "user" or item.get("tool_calls"):
                    out.append(item)
                    prev = "ai"
                else:
                    dropped += 1
                    prev = "drop"
            else:
                out.append(item)
                prev = "ctx"
        return out, dropped

    def _cmd_text(self, event) -> str:
        """取出用于判断命令的正文：去掉 @、去掉开头的唤醒词、去掉标点。"""
        t = self._text(event)
        t = re.sub(r"\[At:\d+\]", " ", t)
        t = re.sub(r"@\S+", " ", t)
        core = _norm_cmd(t)
        for w in sorted(self.wake_words, key=len, reverse=True):
            nw = _norm_cmd(w)
            if nw and core.startswith(nw):
                core = core[len(nw):]
                break
        return core

    @staticmethod
    def _sid(event) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _sender(event) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _is_group(event) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:
            return False

    @staticmethod
    def _at_me(event) -> bool:
        me = ""
        try:
            me = str(event.get_self_id() or "")
        except Exception:
            pass
        try:
            for comp in event.message_obj.message or []:
                ctype = str(getattr(comp, "type", "")).lower()
                qq = str(getattr(comp, "qq", "") or "")
                if "at" in ctype and qq and (not me or qq == me):
                    return True
        except Exception:
            pass
        if me:
            try:
                if ("@" + me) in Main._text(event):
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _at_others(event) -> bool:
        """这条消息是不是 @ 了别人（不是 @ 我）。"""
        me = ""
        try:
            me = str(event.get_self_id() or "")
        except Exception:
            pass
        try:
            for comp in event.message_obj.message or []:
                ctype = str(getattr(comp, "type", "")).lower()
                qq = str(getattr(comp, "qq", "") or "")
                if "at" not in ctype or not qq:
                    continue
                if qq == "all":
                    continue
                if me and qq == me:
                    continue
                return True
        except Exception:
            pass
        try:
            text = Main._text(event)
        except Exception:
            text = ""
        for qq in re.findall(r"\[At:(\d+)\]", text):
            if not me or qq != me:
                return True
        return False

    @staticmethod
    def _quoted_me(event) -> bool:
        """这条消息是不是「引用/回复了我自己的消息」。"""
        me = ""
        try:
            me = str(event.get_self_id() or "")
        except Exception:
            pass
        if not me:
            return False
        try:
            for comp in event.message_obj.message or []:
                if str(getattr(comp, "type", "")).lower() != "reply":
                    continue
                sid = str(getattr(comp, "sender_id", "") or "")
                if sid and sid == me:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _quoted_other(event) -> bool:
        """这条消息是不是「引用/回复了别人的消息」。"""
        me = ""
        try:
            me = str(event.get_self_id() or "")
        except Exception:
            pass
        try:
            for comp in event.message_obj.message or []:
                if str(getattr(comp, "type", "")).lower() != "reply":
                    continue
                sid = str(getattr(comp, "sender_id", "") or "")
                if not sid or sid == "0":
                    continue
                if me and sid == me:
                    continue
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _quoted(event):
        try:
            for comp in event.message_obj.message or []:
                if "reply" in str(getattr(comp, "type", "")).lower():
                    mid = getattr(comp, "id", None)
                    sender = getattr(comp, "sender_id", None)
                    if mid:
                        return str(mid), str(sender or "")
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _extra(event, key):
        try:
            return event.get_extra(key)
        except Exception:
            return None

    @staticmethod
    def _set_extra(event, key, value):
        try:
            event.set_extra(key, value)
        except Exception:
            pass

    @staticmethod
    def _probe(port: int) -> str:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=3) as r:
                return str(r.status)
        except Exception as e:
            code = getattr(e, "code", 0) or 0
            return str(code) if code else "000"

    async def _reply(self, event, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 回复失败: %s" % e)

    # ============================================================ 1) 归档
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load_counters(self) -> dict:
        data = self._load_json(self._counters_file, {})
        if isinstance(data, dict):
            today = self._today()
            return {k: int(v) for k, v in data.items() if str(k).startswith(today)}
        return {}

    def _archive_path(self) -> str:
        if self.per_day:
            return os.path.join(self.out_dir, "chat-%s.jsonl" % self._today())
        return os.path.join(self.out_dir, "chat-all.jsonl")

    def _group_allowed(self, sid: str, sender: str, role: str) -> bool:
        if self.group_mode != "sampled":
            return True
        if role == "bot":
            return True
        if sender and sender in self.keep_senders:
            return True
        key = "%s|%s" % (self._today(), sid)
        n = self._counters.get(key, 0)
        if n >= self.group_daily_cap:
            return False
        self._counters[key] = n + 1
        self._save_json(self._counters_file, self._counters)
        return True

    def _record(self, event, role: str, text: str = "") -> None:
        is_group = self._is_group(event)
        if is_group:
            if not self.archive_group:
                return
        elif not self.archive_private:
            return
        # 同一个事件可能从多个入口进来（事件监听器 + 闸门钩子），只写一次
        try:
            done = getattr(self, "_arch_done", None)
            if done is None:
                done = {}
                self._arch_done = done
            akey = (id(event), role)
            if akey in done:
                return
            now_ts = time.time()
            if len(done) > 3000:
                for k, ts in list(done.items()):
                    if now_ts - ts > 300:
                        del done[k]
            done[akey] = now_ts
        except Exception:
            pass

        sid = self._sid(event)
        sender_id, sender_name = "", ""
        try:
            if role == "bot":
                sender_id, sender_name = str(event.get_self_id() or ""), "bot"
            else:
                sender_id = str(event.get_sender_id() or "")
                sender_name = str(event.get_sender_name() or "")
        except Exception:
            pass

        if not text:
            text = self._text(event)
        text = (text or "").strip()
        if self.skip_empty and not text:
            return
        if is_group and not self._group_allowed(sid, sender_id, role):
            return

        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "role": role,
            "type": "group" if is_group else "private",
            "session": sid,
            "group_id": str(event.get_group_id() or "") if is_group else "",
            "sender_id": sender_id,
            "sender_name": sender_name,
            "text": text,
        }
        try:
            with open(self._archive_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 归档失败: %s" % e)
    async def on_group_silent(self, event):
        """群聊彻底禁言：在事件阶段就掐掉，后面的插件连看都看不到，自然不花 token。"""
        try:
            if not self._is_group(event) or not self._group_silent_now():
                return
            event.clear_result()
            event.stop_event()
            now = time.time()
            if now - self._silent_log_at > 300:
                self._silent_log_at = now
                if self._gs_until == float("inf"):
                    logger.info("[group_silent] 群聊永久禁言中，跳过（5 分钟内只记一次）")
                else:
                    left = max(0, int((self._gs_until - now) // 60))
                    logger.info(
                        "[group_silent] 群聊禁言中，还剩 %d 分钟（5 分钟内只记一次）" % left
                    )
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 群聊禁言处理失败: %s" % e)
    async def on_archive(self, event):
        """最先执行：只写文件，不拦截。"""
        try:
            if self._is_empty_notice(event):
                event.stop_event()
                return
            self._record(event, "user")
            self._remember(event)
            self._catchup_track(event)
            if self.catchup_enabled:
                t = _CATCHUP.get("task")
                if _CATCHUP.get("inst") is not self or t is None or t.done():
                    if t is not None and not t.done():
                        t.cancel()  # 上一个实例的循环，让它立刻退场
                    _CATCHUP["inst"] = self
                    _CATCHUP["task"] = asyncio.create_task(self._catchup_loop())
                    self._catchup_task = _CATCHUP["task"]
                    logger.info("[fish_core] 掉线补拉已启动，每 %ds 查一次" % self.catchup_interval)
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 归档入站失败: %s" % e)
    async def on_archive_sent(self, event):
        try:
            res = event.get_result()
            text = ""
            if res is not None:
                try:
                    text = res.get_plain_text(with_other_comps_mark=True) or ""
                except TypeError:
                    text = res.get_plain_text() or ""
            text = (text or "").strip()
            if not text:
                return
            sig = "%s|%s" % (self._sid(event), text[:120])
            now = time.time()
            if sig == self._last_bot[0] and now - self._last_bot[1] < 5:
                return
            self._last_bot = (sig, now)
            self._record(event, "bot", text)
            try:
                sid = self._sid(event)
                buf = self._recent_bot.setdefault(sid, [])
                buf.append((now, text))
                del buf[:-6]
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 归档机器人回复失败: %s" % e)

    # ======================================================== 2) 主人指令
    def _audit(self, raw: str, result: str) -> None:
        try:
            with open(os.path.join(self.state_dir, "owner_cmd.log"), "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "who": self.cmd_owner,
                            "raw": raw[:200],
                            "result": result[:500],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    async def _cmd_status(self) -> str:
        try:
            load = open("/proc/loadavg").read().split()[:3]
        except Exception:
            load = ["?", "?", "?"]
        try:
            n_plugin = len(
                [d for d in os.listdir(PLUGINS_DIR) if os.path.isdir(os.path.join(PLUGINS_DIR, d))]
            )
        except Exception:
            n_plugin = -1
        return "AstrBot 6185=%s  NapCat 6099=%s\n负载 %s\n插件目录 %d 个" % (
            self._probe(6185),
            self._probe(6099),
            " ".join(load),
            n_plugin,
        )

    async def _cmd_reload(self, name: str) -> str:
        mgr = getattr(self.context, "_star_manager", None)
        if mgr is None:
            return "❌ 拿不到 star_manager，重载失败"
        if not name:
            return "用法：@我 重载 <插件名>"
        try:
            await mgr.reload(name)
        except Exception as e:  # noqa: BLE001
            return "❌ 重载 %s 失败：%s" % (name, e)
        return "✅ 已重载 %s" % name

    async def _cmd_deploy(self, name: str) -> str:
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name):
            return "用法：@我 部署 <插件名>（只允许字母数字下划线横线）"
        if not self._deploy_stage or not PLUGINS_DIR:
            return "❌ 未配置 deploy_stage_dir 或找不到插件目录，部署功能不可用"
        src = os.path.join(self._deploy_stage, "plugin-" + name)
        dst = os.path.join(PLUGINS_DIR, name)
        if not os.path.isdir(src):
            return "❌ 找不到 %s\n先把插件目录放到 %s/plugin-%s" % (src, self._deploy_stage, name)
        try:
            if os.path.isdir(dst):
                # 备份放到插件数据目录，不能放 plugins/ 里——那里会被当成另一个插件加载
                bak = os.path.join(self.state_dir, "deploy_backups")
                os.makedirs(bak, exist_ok=True)
                shutil.copytree(
                    dst,
                    os.path.join(bak, "%s.%s" % (name, time.strftime("%Y%m%d-%H%M%S"))),
                    dirs_exist_ok=True,
                )
            shutil.copytree(src, dst, dirs_exist_ok=True)
            for root, dirs, _files in os.walk(dst):
                for d in list(dirs):
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                        dirs.remove(d)
        except Exception as e:  # noqa: BLE001
            return "❌ 部署 %s 失败：%s" % (name, e)

        msg = "✅ 已部署 %s" % name
        try:
            mgr = getattr(self.context, "_star_manager", None)
            if mgr is not None:
                await mgr.reload(name)
                msg += "，并已重载"
        except Exception as e:  # noqa: BLE001
            msg += "（重载失败：%s，发「@我 重载 %s」重试）" % (e, name)
        return msg

    async def _cmd_config(self, arg: str) -> str:
        if "=" not in arg:
            return "用法：@我 配置 <键>=<值>\n可用键：%s" % ", ".join(sorted(ALLOWED_CONFIG))
        key, _, val = arg.partition("=")
        key, val = key.strip(), val.strip()
        if key not in ALLOWED_CONFIG:
            return "❌ 不允许改 %s；可用键：%s" % (key, ", ".join(sorted(ALLOWED_CONFIG)))
        path, caster = ALLOWED_CONFIG[key][:-1], ALLOWED_CONFIG[key][-1]
        try:
            casted = caster(int(val)) if caster is int else caster(val.lower() in ("1", "true", "on", "yes"))
        except Exception:
            return "❌ 值格式不对：%s" % val
        cfg = self.context.get_config()
        node = cfg
        try:
            for k in path[:-1]:
                node = node[k]
            old = node.get(path[-1])
            node[path[-1]] = casted
            try:
                await cfg.save_config_async()
            except Exception:
                cfg.save_config()
        except Exception as e:  # noqa: BLE001
            return "❌ 写配置失败：%s" % e
        return "✅ %s：%s → %s（已落盘）" % (key, old, casted)

    async def _cmd_recall(self, event, arg: str) -> str:
        n = 1
        if arg.strip().isdigit():
            n = max(1, min(int(arg.strip()), 10))
        bot = getattr(event, "bot", None)
        if bot is None:
            return "❌ 拿不到 OneBot 句柄，无法撤回"

        quoted_id, quoted_sender = self._quoted(event)
        if quoted_id:
            try:
                await bot.call_action("delete_msg", message_id=quoted_id)
                logger.info("[fish_core] 撤回引用消息 id=%s sender=%s" % (quoted_id, quoted_sender or "?"))
                return "✅ 已撤回（引用那条）"
            except Exception as e:  # noqa: BLE001
                return "❌ 撤回引用那条失败：%s（可能是别人的消息且我不是管理员，或已超 2 分钟）" % str(e)[:90]

        if not self._is_group(event):
            return "❌ 撤回只支持群聊"
        me = str(event.get_self_id() or "")
        gid = int(event.get_group_id())
        try:
            hist = await bot.call_action("get_group_msg_history", group_id=gid, count=30)
        except Exception as e:  # noqa: BLE001
            return "❌ 取群消息历史失败：%s" % str(e)[:100]
        msgs = []
        if isinstance(hist, dict):
            msgs = hist.get("messages") or hist.get("data") or []
        mine = [m for m in msgs if str(((m.get("sender") or {}).get("user_id")) or "") == me]
        if not mine:
            return "❌ 最近 30 条里没找到我发的消息"
        ok, fails = 0, []
        for m in mine[-n:]:
            mid = m.get("message_id") or m.get("messageId")
            if not mid:
                fails.append("缺 message_id")
                continue
            try:
                await bot.call_action("delete_msg", message_id=mid)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fails.append(str(e)[:60])
        if ok and not fails:
            return "✅ 已撤回 %d 条" % ok
        if ok:
            return "⚠️ 撤回 %d 条，%d 条失败：%s" % (ok, len(fails), fails[0])
        return "❌ 撤回失败：%s（QQ 群消息超过 2 分钟就不能撤了）" % (fails[0] if fails else "未知原因")

    @staticmethod
    def _help() -> str:
        return (
            "@我 状态\n"
            "@我 重载 <插件名>（单个插件，安全）\n"
            "@我 重启（整个进程，约 15 秒，QQ 不掉线）\n"
            "@我 部署 <插件名>（从 Download/astrbot-setup/plugin-<名字>）\n"
            "@我 配置 max_turns=10\n"
            "@我 撤回（引用某条 = 只撤那一条；不引用 = 撤我最近 1 条）\n"
            "@我 撤回 3（撤我最近 3 条）\n"
            "群聊静默：@我 停一下 / @我 可以说话了"
        )
    async def on_owner_cmd(self, event):
        if not self.cmd_enabled or not self.cmd_owner:
            return
        try:
            if not self._is_group(event):
                return
            if str(event.get_sender_id() or "") != self.cmd_owner:
                return
            if self.cmd_groups and str(event.get_group_id()) not in self.cmd_groups:
                return
            if not self._at_me(event):
                return
            body = re.sub(r"@\S+", " ", self._text(event)).strip()
            m = re.match(r"(重载|重启|部署|配置|撤回|状态|帮助)\s*(.*)$", body, re.S)
            if not m:
                return
            cmd, arg = m.group(1), m.group(2).strip()

            if cmd == "重启":
                msg = "✅ 正在重启 AstrBot（约 15 秒后恢复，QQ 不掉线、不用扫码）"
                self._audit(body, msg)
                event.clear_result()
                event.stop_event()
                await self._reply(event, msg)
                logger.info("[fish_core] 收到重启指令，1.5 秒后 execv 自身")
                await asyncio.sleep(1.5)
                try:
                    os.execv(sys.executable, [sys.executable] + list(sys.argv))
                except Exception as e:  # noqa: BLE001
                    logger.error("[fish_core] 重启失败: %s" % e)
                return

            if cmd == "撤回":
                out = await self._cmd_recall(event, arg)
                self._audit(body, out)
                logger.info("[fish_core] 撤回结果: %s" % out)
                event.clear_result()
                event.stop_event()
                if not (self.recall_silent and out.startswith("✅")):
                    await self._reply(event, out)
                return

            if cmd == "状态":
                out = await self._cmd_status()
            elif cmd == "重载":
                out = await self._cmd_reload(arg)
            elif cmd == "部署":
                out = await self._cmd_deploy(arg)
            elif cmd == "配置":
                out = await self._cmd_config(arg)
            else:
                out = self._help()

            self._audit(body, out)
            logger.info("[fish_core] 执行 %s %s → %s" % (cmd, arg, out.replace("\n", " / ")))
            event.clear_result()
            event.stop_event()
            await self._reply(event, out)
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 主人指令处理失败: %s" % e)

    # ======================================================== 3) 静默开关
    def _is_muted(self, sid: str) -> bool:
        until = self.muted.get(sid)
        if until is None:
            return False
        if until <= time.time():
            self.muted.pop(sid, None)
            self._save_json(self.mute_file, self.muted)
            logger.info("[fish_core] 静默到期，自动解除: %s" % sid)
            return False
        return True

    def _apply_mute(self, sid: str, mute: bool) -> None:
        if mute:
            self.muted[sid] = time.time() + self.mute_minutes * 60
        else:
            self.muted.pop(sid, None)
        self._save_json(self.mute_file, self.muted)
    async def on_mute(self, event):
        """静默开关。排在主人指令之后，所以静默期间 @我 指令仍然可用。"""
        try:
            sid = self._sid(event)

            # 私聊不参与静默：既不认「闭嘴」这类词，也不拦回复
            if self.mute_only_group and not self._is_group(event):
                if sid in self.muted:
                    self._apply_mute(sid, False)
                    logger.info("[fish_core] 私聊不静默，清掉旧静默记录: %s" % sid)
                return

            if self._is_group(event) and self._group_silent_now():
                return
            core = self._cmd_text(event)
            muted_now = self._is_muted(sid)

            # 静默必须是真的在点它：@ 它，或者用 AstrBot 的唤醒前缀
            if self._at_me(event) or getattr(event, "is_at_or_wake_command", False):
                if _cmd_hit_strict(core, self.mute_words):
                    self._apply_mute(sid, True)
                    self._allow_once.add(sid)
                    logger.info("[fish_core] 进入静默: %s" % sid)
                    tip = "好，我先闭嘴。"
                    if self.mute_minutes:
                        tip += "（%g 分钟后自动恢复，想提前就 @我 说「可以说话了」）" % self.mute_minutes
                    else:
                        tip += "（想让我说话就 @我 说「可以说话了」）"
                    event.clear_result()
                    event.stop_event()
                    await self._reply(event, tip)
                    return

                # 只有确实静默着，才走「解除」分支；否则像「那说好了…」这种
                # 含关键词的普通情话会被当成命令，只回一句固定的「我回来了」。
                if muted_now and _cmd_hit_strict(core, self.unmute_words):
                    self._apply_mute(sid, False)
                    logger.info("[fish_core] 解除静默: %s" % sid)
                    event.clear_result()
                    event.stop_event()
                    await self._reply(event, "我回来了，继续吧。")
                    return

            if muted_now:
                if (
                    self.mute_owner_can_wake
                    and self._sender(event) in self.owners
                    and self._has_wake(event)
                ):
                    self._set_extra(event, "fish_mute_pass", True)
                    logger.info("[fish_core] 静默中主人唤醒，放行: %s" % sid)
                    return
                event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 静默开关处理失败: %s" % e)
    async def on_mute_result(self, event):
        try:
            sid = self._sid(event)
            if self.mute_only_group and not self._is_group(event):
                return
            if sid in self._allow_once:
                self._allow_once.discard(sid)
                return
            if self._extra(event, "fish_mute_pass"):
                return
            if self._is_muted(sid):
                event.clear_result()
                event.stop_event()
                logger.info("[fish_core] 拦下一条回复: %s" % sid)
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 静默拦截失败: %s" % e)

    # ============================================== 3.5) 掉线补拉
    def _catchup_track(self, event) -> None:
        """记下每条入站私聊的 message_id，用于发现漏掉的那几条。"""
        try:
            sid = self._sid(event)
            if "FriendMessage" not in sid:
                return
            sender = self._sender(event)
            if sender not in self.owners:
                return
            mid = self._msg_id(event)
            if not mid:
                return
            st = self.catchup.get(sid) or {}
            if int(mid) > int(st.get("last_id") or 0):
                self.catchup[sid] = {"peer": sender, "last_id": int(mid)}
                self._save_json(self.catchup_file, self.catchup)
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 补拉记账失败: %s" % e)

    @staticmethod
    def _msg_id(event):
        try:
            return int(getattr(event.message_obj, "message_id", 0) or 0)
        except Exception:
            return 0

    @staticmethod
    def _hist_text(msg) -> str:
        segs = msg.get("message") or msg.get("raw_message") or []
        if isinstance(segs, str):
            return segs.strip()
        out = []
        for seg in segs or []:
            if not isinstance(seg, dict):
                continue
            t = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if t == "text":
                out.append(str(data.get("text") or ""))
            elif t == "image":
                out.append("[图片]")
            elif t == "record":
                out.append("[语音]")
        return "".join(out).strip()

    async def _catchup_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            if _CATCHUP.get("inst") is not self:
                logger.info("[fish_core] 补拉循环被新实例接管，退出")
                return
            try:
                await self._catchup_once()
            except Exception as e:  # noqa: BLE001
                logger.error("[fish_core] 补拉异常: %s" % e)
            await asyncio.sleep(self.catchup_interval)

    async def _catchup_once(self) -> None:
        if not self.catchup:
            return
        changed = False
        for umo, st in list(self.catchup.items()):
            if "FriendMessage" not in umo:
                continue
            peer = str(st.get("peer") or "")
            last = int(st.get("last_id") or 0)
            if not peer or peer not in self.owners:
                continue
            plat = None
            try:
                plat = self.context.get_platform_inst(umo.split(":", 1)[0])
            except Exception:
                plat = None
            if plat is None:
                continue
            try:
                client = plat.get_client()
            except Exception:
                client = None
            if client is None:
                continue
            try:
                ret = await asyncio.wait_for(
                    client.call_action(
                        "get_friend_msg_history", user_id=int(peer), count=10
                    ),
                    timeout=15,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish_core] 补拉取历史失败: %s" % e)
                continue

            msgs = []
            if isinstance(ret, dict):
                msgs = ret.get("messages") or (ret.get("data") or {}).get("messages") or []
            if not isinstance(msgs, list):
                continue

            now = time.time()
            missed = []
            top = last
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                try:
                    mid = int(m.get("message_id") or 0)
                except Exception:
                    continue
                uid = str((m.get("sender") or {}).get("user_id") or m.get("user_id") or "")
                if mid > top:
                    top = mid
                if mid <= last or uid != peer:
                    continue
                ts = float(m.get("time") or 0)
                if ts and now - ts > 1800:
                    continue
                text = self._hist_text(m)
                if text:
                    missed.append(text)

            if top != last:
                st["last_id"] = top
                changed = True

            if not missed:
                continue
            tail = missed[-self.catchup_max:]
            quote = "／".join(t[:40] for t in tail)
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        [
                            Plain(
                                "刚才我这边断了一下，你发的「%s」没进来，我没接着——"
                                "再说一遍？" % quote
                            )
                        ]
                    ),
                )
                logger.info("[fish_core] 补拉提醒: %s 漏 %d 条 → %s" % (umo, len(missed), quote))
            except Exception as e:  # noqa: BLE001
                logger.error("[fish_core] 补拉发送失败: %s" % e)

        if changed:
            self._save_json(self.catchup_file, self.catchup)

    # ============================================== 4) 节流 + 唤醒闸门
    def _set_session_tts(self, umo: str, probability: float) -> None:
        """改写 mimo TTS 在该会话下的自动语音概率（它的每轮触发门）。"""
        if not self._tts_studio_db or not os.path.exists(self._tts_studio_db):
            return
        try:
            con = sqlite3.connect(self._tts_studio_db, timeout=5)
            try:
                row = con.execute(
                    "SELECT payload FROM settings WHERE category='sessions' AND id=?",
                    (umo,),
                ).fetchone()
                data = {}
                if row:
                    try:
                        data = json.loads(row[0]) or {}
                    except Exception:  # noqa: BLE001
                        data = {}
                if not isinstance(data, dict):
                    data = {}
                data["auto_tts_probability"] = float(probability)
                con.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('sessions', ?, ?)",
                    (umo, json.dumps(data, ensure_ascii=False)),
                )
                con.commit()
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 写 TTS 会话概率失败: %s" % e)

    def _sync_tts_context(self, event) -> None:
        """私聊：按这句话的情境决定这一轮要不要发语音。"""
        if not self.tts_ctx_enabled:
            return
        sid = self._sid(event)
        if "GroupMessage" in sid:
            return
        text = self._text(event)
        hit = any(w and w in text for w in self.tts_ctx_words)
        if not hit and self.tts_ctx_fallback > 0 and random.random() < self.tts_ctx_fallback:
            hit = True
        self._set_session_tts(sid, 1.0 if hit else 0.0)
        if hit:
            logger.info("[fish_core] 本轮发语音（命中情境）: %s" % sid)

    def _is_called(self, event, sender: str) -> bool:
        """这条是不是在明确叫我：@ 我，或者说了唤醒词。

        以前看的是 event.is_at_or_wake_command，但那个标记会由别的插件（比如
        angel_heart）在「继续聊」时也设上，导致节流和插话判定整段被跳过。
        """
        if sender in self.owners:
            return True
        if not self.exempt_wake:
            return False
        if self.wake_on_at and self._at_me(event):
            return True
        if not self.wake_words:
            return False
        return any(w and w in self._text(event) for w in self.wake_words)

    def _has_wake(self, event) -> bool:
        if self.wake_on_at and self._at_me(event):
            return True
        # 回复我自己的消息，也算在叫我
        if self._quoted_me(event):
            return True
        if not self.wake_words:
            return True
        text = self._text(event)
        return any(w in text for w in self.wake_words)

    def _remember(self, event) -> None:
        try:
            sid = self._sid(event)
            text = self._text(event).strip()
            if not text:
                return
            buf = self._recent_msgs.setdefault(sid, [])
            buf.append((time.time(), self._sender(event), text))
            del buf[:-20]
        except Exception:
            pass

    def _smart_score(self, event):
        """给「要不要接这句」打分，返回 (score, 明细)。"""
        sid = self._sid(event)
        core = self._text(event).strip()
        now = time.time()

        noise = 0.0
        if len(core) < 3 or _NOISE_ONLY.match(core):
            noise = 1.0
        elif core in _REPEAT_WORDS:
            noise = 1.0
        else:
            bs = _bigrams(core)
            for _ts, bt in self._recent_bot.get(sid, [])[-3:]:
                if bt and _jaccard(bs, _bigrams(bt)) >= 0.85:
                    noise = 1.0
                    break

        question = 0.0
        if "?" in core or "？" in core:
            question = 0.9
        elif any(w in core for w in _QUESTION_WORDS):
            question = 0.75

        relevant = 0.0
        if core and noise < 1.0:
            bs = _bigrams(core)
            cands = [_jaccard(bs, _bigrams(t)) for _ts, t in self._recent_bot.get(sid, [])[-5:] if t]
            if cands:
                relevant = max(cands)

        interest = 0.0
        if self.smart_interest:
            hits = sum(1 for w in self.smart_interest if w and w in core)
            interest = min(1.0, hits * 0.6)

        bored = 0.0
        bot_buf = self._recent_bot.get(sid) or []
        if bot_buf and now - bot_buf[-1][0] >= 900:
            bored = 0.6

        total = self.w_question + self.w_relevant + self.w_interest + self.w_bored
        raw = (
            self.w_question * question
            + self.w_relevant * relevant
            + self.w_interest * interest
            + self.w_bored * bored
            - self.w_noise * noise
        )
        score = max(0.0, min(1.0, raw / total if total > 0 else 0.0))
        return score, {
            "q": question,
            "r": relevant,
            "i": interest,
            "b": bored,
            "n": noise,
        }

    @staticmethod
    def _is_empty_notice(event) -> bool:
        """通知类事件（输入状态、已读回执等）被 AstrBot 转成了空消息。

        这类事件没有文字也没有消息段，却会走完整管线（记忆测算、状态机…），
        纯属白烧。戳一戳带 Poke 组件，不受影响。
        """
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            post_type = ""
            if raw is not None:
                try:
                    post_type = str(raw.get("post_type") or "")
                except Exception:
                    post_type = str(getattr(raw, "post_type", "") or "")
            if post_type != "notice":
                return False
            comps = getattr(event.message_obj, "message", None) or []
            text = (Main._text(event) or "").strip()
            return not comps and not text
        except Exception:
            return False

    def _wake_key(self, event):
        return (self._sid(event), self._sender(event))

    def _wake_window_on(self, event) -> bool:
        if self.wake_window <= 0:
            return False
        until = self._wake_until.get(self._wake_key(event)) or 0.0
        return time.time() < until

    def _open_wake_window(self, event) -> None:
        if self.wake_window > 0:
            self._wake_until[self._wake_key(event)] = time.time() + self.wake_window

    def _delay_max(self, event) -> float:
        """私聊用私聊的上限，群聊用群聊的；都是 0 就不等。"""
        if "FriendMessage" in self._sid(event):
            return self.reply_delay_private_max
        return self.reply_delay_max

    def _merge_on(self, event) -> bool:
        """私聊默认不做合并等待（那 2.5 秒纯属白等），群聊照旧。

        私聊只有你一个人说话，连发两句也接得上，等窗口没有收益；
        群聊要靠这个窗口把同一人的几个问题攒成一段再答，所以保留。
        """
        if "FriendMessage" in self._sid(event):
            return self.merge_private
        return True

    async def _merge_hold(self, event) -> bool:
        """把同一人窗口内的连续消息并进第一条；返回 True 表示这条被并掉了。"""
        if self.merge_window_ms <= 0:
            return False
        key = self._wake_key(event)
        now = time.time()
        buf = self._merge.get(key)
        if buf and buf.get("event") is not event and now < buf.get("deadline", 0):
            text = self._text(event).strip()
            if text:
                buf["texts"].append(text)
            logger.info(
                "[merge] 并进上一条（已有 %d 句）：%s" % (len(buf["texts"]), text[:30])
            )
            event.clear_result()
            event.stop_event()
            return True

        buf = {"event": event, "texts": [self._text(event).strip()], "deadline": now + self.merge_window_ms / 1000.0}
        self._merge[key] = buf
        await asyncio.sleep(self.merge_window_ms / 1000.0)
        self._merge.pop(key, None)
        texts = [t for t in buf["texts"] if t]
        if len(texts) > 1:
            merged = "\n".join(texts)
            try:
                event.message_str = merged
                logger.info("[merge] %d 句合成一条：%s" % (len(texts), merged[:60]))
            except Exception as e:  # noqa: BLE001
                logger.error("[fish_core] 合并失败: %s" % e)
        return False

    def _group_silent_now(self) -> bool:
        """群聊此刻是否处于禁言中（限时禁言到点会自动失效）。"""
        if not self.group_silent:
            return False
        if time.time() < self._gs_until:
            return True
        if not self._gs_expired_logged and self._gs_until > 0:
            self._gs_expired_logged = True
            logger.info(
                "[fish_core] 群聊限时禁言结束（%d 分钟），已恢复正常"
                % self.group_silent_minutes
            )
        return False

    def _decide(self, event):
        """返回 (verdict, message)，verdict ∈ pass / block / exempt / skip。"""
        if self._is_group(event) and self._group_silent_now():
            return "block", "[group_silent] 群聊禁言中，跳过"
        if self.min_seconds <= 0:
            return "skip", ""
        sid = self._sid(event)
        if self.pace_only_group and "GroupMessage" not in sid:
            return "skip", ""

        sender = self._sender(event)

        # 唤醒闸门：主人的群聊发言必须先命中唤醒词或 @
        if self.wake_required:
            must_wake = (sender in self.owners) if self.wake_owners_only else True
            if must_wake and not self._has_wake(event) and not self._wake_window_on(event):
                return "block", "[group_pace] 未命中唤醒词也没 @，忽略（账号 %s，唤醒词 %s / 或 @我）" % (
                    sender,
                    "/".join(self.wake_words),
                )

        # 这条在 @ 别人：不是叫我，就不插话（除非同时叫了我）
        if self.skip_group_at_others and self._at_others(event):
            return "block", "[group_pace] 这条在 @ 别人，不插话"

        # 这条是在引用/回复别人的话：同样不插嘴
        if self.skip_group_quote_others and self._quoted_other(event):
            return "block", "[group_pace] 这条在回复别人，不插话"

        if self._has_wake(event):
            self._open_wake_window(event)

        if self._is_called(event, sender):
            return "exempt", "[group_pace] 豁免（账号 %s），正常发出" % sender

        # 智能插话：这条值不值得接（没人叫我时，靠内容自己判断）
        if self.smart_enabled:
            score, d = self._smart_score(event)
            if self.smart_log_scores:
                logger.info(
                    "[smart] %.2f (问%.2f 相关%.2f 兴趣%.2f 冷场%.2f 噪声%.2f) %s"
                    % (score, d["q"], d["r"], d["i"], d["b"], d["n"], self._text(event)[:40])
                )
            if score < self.smart_threshold:
                now2 = time.time()
                if now2 - self._last_smart_log > 60:
                    self._last_smart_log = now2
                    return "block", (
                        "[smart] 插话分 %.2f < %.2f，不接（5 分钟内只记一次）"
                        % (score, self.smart_threshold)
                    )
                return "silent", ""

        now = time.time()
        key = "%s|%s" % (sid, sender)
        wait = self.min_seconds
        if self.pace_jitter > 0:
            wait *= 1.0 + random.uniform(-self.pace_jitter, self.pace_jitter)
        left = wait - (now - self.pace_last.get(key, 0.0))
        if left > 0:
            return "block", "[group_pace] 个人节流：还差 %.0f 秒，跳过 %s" % (left, key)
        gkey = "%s|*" % sid
        gleft = self.global_seconds - (now - self.pace_last.get(gkey, 0.0))
        if gleft > 0:
            return "block", "[group_pace] 全群节流：%.0f 秒内已经回过别人，跳过" % gleft

        self.pace_last[key] = now
        self.pace_last[gkey] = now
        self._save_json(self.pace_file, self.pace_last)
        return "pass", ""
    async def on_gate(self, event):
        """调模型之前：唤醒/节流判定。命中 block 就终止，不产生任何 LLM 开销。"""
        try:
            # 归档兜底：这个钩子每条消息必走；事件监听器那条路有时会被更早的
            # 处理器吞掉，导致群消息漏记。_record 内部按事件去重，不会重复写。
            self._record(event, "user")
            self._sync_tts_context(event)
            if self._extra(event, "fish_gate"):
                return
            verdict, msg = self._decide(event)
            self._set_extra(event, "fish_gate", verdict)
            if verdict in ("block", "silent"):
                event.clear_result()
                event.stop_event()
                if verdict == "silent":
                    pass
                elif msg.startswith("[group_silent]"):
                    now = time.time()
                    if now - self._silent_log_at > 300:
                        self._silent_log_at = now
                        logger.info(msg + "（5 分钟内只记这一条）")
                else:
                    logger.info(msg)
            elif verdict == "exempt":
                logger.info(msg)
            merge_ok = verdict in ("pass", "exempt") or (
                verdict == "skip" and "FriendMessage" in self._sid(event)
            )
            if merge_ok:
                if self._merge_on(event) and await self._merge_hold(event):
                    return
                delay_max = self._delay_max(event)
                if delay_max > 0:
                    await asyncio.sleep(random.uniform(min(0.4, delay_max), delay_max))
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 闸门处理失败: %s" % e)
    async def on_pace_result(self, event):
        """兜底：非 LLM 路径（命令回复等）也按节流规则走一遍。"""
        try:
            if self._extra(event, "fish_gate"):
                return
            verdict, msg = self._decide(event)
            if verdict in ("block", "silent"):
                event.clear_result()
                if verdict == "block":
                    logger.info(msg)
            elif verdict == "exempt":
                logger.info(msg)
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 兜底节流失败: %s" % e)
    async def on_trim_group_history(self, event, req):
        """群聊里只把跟主人有关的历史发给模型，别人的闲聊在进模型前就丢掉。

        只改这一次请求的 req.contexts，不动数据库——聊天记录该留的还留着，
        只是不再占上下文。私聊完全不受影响。
        """
        try:
            if not self.gh_owner_only:
                return
            if not (self._is_group(event) or "GroupMessage" in self._sid(event)):
                return
            contexts = getattr(req, "contexts", None)
            if not isinstance(contexts, list) or len(contexts) < 2:
                return
            kept, dropped = self._trim_group_history(contexts)
            if dropped <= 0:
                return
            req.contexts = kept
            now = time.time()
            if now - self._gh_log_at > 60:
                self._gh_log_at = now
                logger.info(
                    "[fish_core] 群聊历史裁剪：留 %d 条，丢 %d 条非主人消息（%s）"
                    % (len(kept), dropped, self._sid(event))
                )
        except Exception as e:  # noqa: BLE001
            logger.error("[fish_core] 群聊历史裁剪失败: %s" % e)

    async def initialize(self):
        """合并进 dsh_all 后由主类统一调度；这里只做一次就绪日志。"""
        logger.info(f"[fish] 已就绪（配置 {len(self.config)} 项，状态目录 {self.state_dir}）")

    async def terminate(self):
        task = _CATCHUP.get("task")
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
