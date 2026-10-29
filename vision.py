# -*- coding: utf-8 -*-
"""白鸽·看图（dsh_vision）

背景：QQ 图床 gchat.qpic.cn 的 rkey 经常整片失效（HTTP 400 invalid rkey）。
NapCat 自己的 rkey 拿不到、两个公共 rkey 源挂掉时，它只能拼一个假 rkey，
AstrBot 拿到这种 URL 必然 400，图就丢了，模型只看得到文字。

这里不走 URL，直接让 NapCat 把媒体落盘（OneBot `get_image` → downloadMedia），
三层兜底，并且**全部是插件实现，升级 AstrBot 不会丢**：

1. `install()` 给 `Image.convert_to_file_path` / `convert_to_base64` 打包装：
   原生下载失败时接管，换成 get_image 取到的本地文件；
2. 同一条消息的图片按 (file,url) 缓存 900 秒，不会重复取；
3. 最后还有 `on_llm_request` 的兜底（`fix_request`），保证发给模型的
   `image_urls` 一定是本地可读文件。

取图顺序：get_image(ref) → URL 里的 32 位 md5 去 QQ 缓存目录找同名文件
→ 最近 3 分钟缓存里只多出一张时认定是它。

自测：放一个 `<bridge>/vision-selftest.flag`，重载插件后会拉最近群消息里的
图片做一次真取图，结果写在日志（[dsh_vision] 自测）。
"""
import asyncio
import base64
import hashlib
import os
import re
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest

try:  # 4.x 两种导入路径都兼容
    from astrbot.api.message_components import Image
except Exception:  # pragma: no cover
    from astrbot.core.message.components import Image

from . import settings

HASH_RE = re.compile(r"([0-9a-f]{32})")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
CACHE_ROOTS = ("/root/.config/QQ", os.path.expanduser("~/.config/QQ"))
DIR_PATTERNS = (
    "*/nt_data/Pic/*/Ori",
    "*/nt_data/Pic/*/Thumb",
    "*/nt_data/Emoji/emoji-recv/*/Ori",
)
RECENT_WINDOW = 180
TTL = 900
TMP_DIR = "/tmp"


# ---------------------------------------------------------------------------
# 全局持有者：挂在 astrbot 的 message.components 模块上，插件重载后仍然是同一个对象，
# 旧包装函数也能继续用最新的恢复逻辑。
# ---------------------------------------------------------------------------
_MOD = sys.modules.get(Image.__module__)
HOLDER = getattr(_MOD, "_dsh_vision_holder", None) if _MOD else None
if not isinstance(HOLDER, dict):
    HOLDER = {}
    if _MOD:
        setattr(_MOD, "_dsh_vision_holder", HOLDER)
HOLDER.setdefault("context", None)
HOLDER.setdefault("bot", None)
HOLDER.setdefault("dirs", [])
HOLDER.setdefault("dirs_at", 0.0)
HOLDER.setdefault("cache", {})
HOLDER.setdefault("hits", 0)
HOLDER.setdefault("recover", None)


def _is_local(path):
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(("http://", "https://", "base64://", "data:")):
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _refs(comp):
    out = []
    for attr in ("file", "url", "path"):
        v = getattr(comp, attr, None)
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# 找 bot（aiocqhttp 平台实例上的 CQHttp 客户端）
# ---------------------------------------------------------------------------
def find_bot(context=None):
    context = context or HOLDER.get("context")
    if context is None:
        return None
    try:
        insts = list(context.platform_manager.get_insts())
    except Exception:
        insts = list(getattr(context.platform_manager, "platform_insts", []) or [])
    for inst in insts:
        bot = getattr(inst, "bot", None)
        if bot is None and hasattr(inst, "get_client"):
            try:
                bot = inst.get_client()
            except Exception:
                bot = None
        if bot is not None and hasattr(bot, "call_action"):
            return bot
    return None


# ---------------------------------------------------------------------------
# QQ 本地缓存目录 / 哈希查找
# ---------------------------------------------------------------------------
def _base_dirs():
    now = time.time()
    dirs = HOLDER["dirs"]
    if dirs and now - HOLDER["dirs_at"] < 600:
        return dirs
    found = []
    for root in CACHE_ROOTS:
        r = Path(root)
        if not r.is_dir():
            continue
        for pat in DIR_PATTERNS:
            try:
                found.extend([d for d in r.glob(pat) if d.is_dir()])
            except OSError:
                continue
    seen, out = set(), []
    for d in found:
        k = str(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    HOLDER["dirs"], HOLDER["dirs_at"] = out, now
    return out


def _find_by_hash(h):
    for d in _base_dirs():
        for ext in IMG_EXTS:
            p = d / (h + ext)
            if _is_local(str(p)):
                return str(p)
    return None


def _newest_recent():
    now = time.time()
    recent = []
    for d in _base_dirs():
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.is_file():
                continue
            if os.path.splitext(e.name)[1].lower() not in IMG_EXTS:
                continue
            try:
                if now - e.stat().st_mtime <= RECENT_WINDOW:
                    recent.append(e.path)
            except OSError:
                continue
    recent = list(dict.fromkeys(recent))
    return recent[0] if len(recent) == 1 else None


# ---------------------------------------------------------------------------
# get_image
# ---------------------------------------------------------------------------
async def _napcat_get(ref, bot=None):
    bot = bot or HOLDER.get("bot") or find_bot()
    call = getattr(bot, "call_action", None) if bot is not None else None
    if not call or not ref:
        return None
    try:
        ret = await call("get_image", file=ref)
    except Exception:
        return None
    if not isinstance(ret, dict):
        return None
    data = ret.get("data") if isinstance(ret.get("data"), dict) else ret
    b64 = data.get("base64") or ""
    if b64:
        try:
            raw = base64.b64decode(b64)
            path = os.path.join(TMP_DIR, f"dsh_vis_{int(time.time() * 1000)}.img")
            with open(path, "wb") as f:
                f.write(raw)
            return path if _is_local(path) else None
        except Exception:
            pass
    local = data.get("file") or data.get("path") or ""
    if _is_local(local):
        return local
    return None


async def recover(comp, bot=None):
    """把一个 Image 组件换成本地可读文件路径，失败返回 None。"""
    refs = _refs(comp)
    for ref in refs:
        if _is_local(ref):
            return ref

    key = hashlib.md5("|".join(refs).encode("utf-8", "ignore")).hexdigest()
    cache = HOLDER["cache"]
    hit = cache.get(key)
    if isinstance(hit, tuple) and _is_local(hit[0]) and time.time() - hit[1] < TTL:
        return hit[0]

    path = None
    for ref in refs:
        path = await _napcat_get(ref, bot)
        if path:
            break
    if not path:
        hashes = []
        for ref in refs:
            hashes.extend(HASH_RE.findall(ref.lower()))
        for h in dict.fromkeys(hashes):
            path = _find_by_hash(h)
            if path:
                break
        if not path and hashes:
            path = _newest_recent()

    if path:
        cache[key] = (path, time.time())
        HOLDER["hits"] += 1
    return path


def _swap(comp, path):
    try:
        comp.path = path
        comp.url = path
        comp.file = path
    except Exception:
        pass


def install(context=None):
    """给 Image 的两个转换方法打包装（幂等，重启后才有必要重打）。"""
    if context is not None:
        HOLDER["context"] = context
    HOLDER["recover"] = recover
    if HOLDER.get("installed"):
        HOLDER["bot"] = find_bot()
        return False

    orig_path = Image.convert_to_file_path
    orig_b64 = Image.convert_to_base64

    async def _patched_path(self, *a, **kw):
        try:
            return await orig_path(self, *a, **kw)
        except Exception:
            p = await recover(self)
            if not p:
                raise
            _swap(self, p)
            HOLDER["hits"] += 1
            logger.info("[dsh_vision] 原生下载失败，get_image 接管成功")
            return p

    async def _patched_b64(self, *a, **kw):
        try:
            return await orig_b64(self, *a, **kw)
        except Exception:
            p = await recover(self)
            if not p:
                raise
            _swap(self, p)
            try:
                with open(p, "rb") as f:
                    return base64.b64encode(f.read()).decode()
            except OSError:
                raise

    Image.convert_to_file_path = _patched_path
    Image.convert_to_base64 = _patched_b64
    HOLDER["installed"] = True
    HOLDER["bot"] = find_bot()
    logger.info("[dsh_vision] 已接管 Image.convert_to_file_path / convert_to_base64")
    return True


# ---------------------------------------------------------------------------
# 自测：拿最近群消息里的图，走一遍真取图
# ---------------------------------------------------------------------------
async def selftest():
    bot = HOLDER.get("bot") or find_bot()
    if bot is None:
        logger.warning("[dsh_vision] 自测：没找到 bot 实例")
        return
    try:
        groups = await bot.call_action("get_group_list")
    except Exception as e:
        logger.warning(f"[dsh_vision] 自测：取群列表失败 {e}")
        return
    if isinstance(groups, dict):
        groups = groups.get("data") or []
    ids = [g.get("group_id") for g in groups if isinstance(g, dict)][:3]
    for gid in ids:
        try:
            hist = await bot.call_action("get_group_msg_history", group_id=gid, count=15)
        except Exception:
            continue
        if isinstance(hist, dict):
            hist = hist.get("data") if isinstance(hist.get("data"), dict) else hist
        msgs = (hist or {}).get("messages") or []
        for msg in reversed(msgs):
            for seg in msg.get("message") or []:
                if seg.get("type") != "image":
                    continue
                d = seg.get("data") or {}
                comp = Image(file=d.get("file") or "", url=d.get("url") or "")
                try:
                    p = await comp.convert_to_file_path()
                    size = os.path.getsize(p) if _is_local(p) else -1
                    logger.info(f"[dsh_vision] 自测 OK 群{gid} -> {p} ({size} B)")
                    return
                except Exception as e:
                    logger.warning(f"[dsh_vision] 自测 失败 群{gid}: {e}")
    logger.warning("[dsh_vision] 自测：最近消息里没找到可测的图")


# ---------------------------------------------------------------------------
class Component:
    def __init__(self, context):
        self.context = context
        self._task = None

    async def initialize(self):
        installed = install(self.context)
        logger.info(
            "[dsh_vision] 已启动："
            + ("接管图片转换 + " if installed else "")
            + "发图前兜底 / 缓存 md5 / 最新一张"
        )
        flag = settings.bridge_dir() / "vision-selftest.flag"
        try:
            if flag.is_file():
                flag.unlink()
                self._task = asyncio.create_task(self._delayed_selftest())
        except Exception:
            pass

    async def _delayed_selftest(self):
        await asyncio.sleep(10)
        HOLDER["bot"] = find_bot()
        await selftest()

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info(f"[dsh_vision] 已停止，本次补图 {HOLDER['hits']} 次")

    # ---------- 最后一道：LLM 请求前 ----------
    async def fix_request(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            comps = []
            for comp in event.get_messages() or []:
                if isinstance(comp, Image):
                    comps.append(comp)
                    continue
                chain = getattr(comp, "chain", None)
                if chain:
                    comps.extend([x for x in chain if isinstance(x, Image)])
            if not comps:
                return
            current = list(getattr(request, "image_urls", None) or [])
            if current and all(_is_local(u) for u in current):
                return
            paths = []
            for comp in comps:
                p = None
                for attr in ("path", "file", "url"):
                    v = getattr(comp, attr, None)
                    if _is_local(v):
                        p = v
                        break
                if not p:
                    p = await recover(comp)
                if p:
                    paths.append(p)
            paths = list(dict.fromkeys(paths))
            if not paths:
                logger.warning("[dsh_vision] 这条消息带图但没取到，只能按文字处理")
                return
            request.image_urls = paths
            logger.info(f"[dsh_vision] 补图 {len(paths)} 张")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dsh_vision] 补图失败: {e}")
