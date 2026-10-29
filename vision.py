# -*- coding: utf-8 -*-
"""白鸽·看图（dsh_vision）

QQ 图床 gchat.qpic.cn 的 rkey 经常整片失效（HTTP 400 invalid rkey），
AstrBot 自己拉不到图，模型就只收到文字，等于「看不见图」。

这里在真正把请求交给模型之前再兜一次底（on_llm_request 钩子，纯插件实现，
不依赖任何核心补丁，升级 AstrBot 也不会丢）：

1) 消息链里已经有可读的本地图片路径 → 直接用；
2) 否则用 ref 调 NapCat 的 get_image 现取（base64 / 本地文件）；
3) 还不行就把 URL / 文件名里的 32 位哈希拿去 QQ 图片缓存目录里找同名文件；
4) 最后：最近 3 分钟内缓存里只多出这一张图时，认定就是它。

只在该请求确实带图、而且现有 image_urls 不是本地路径时才动手。
"""
import base64
import os
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest

try:  # 4.x 两种导入路径都兼容
    from astrbot.api.message_components import Image
except Exception:  # pragma: no cover
    from astrbot.core.message.components import Image

HASH_RE = re.compile(r"([0-9a-f]{32})")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
CACHE_ROOTS = ("/root/.config/QQ", os.path.expanduser("~/.config/QQ"))
DIR_PATTERNS = (
    "*/nt_data/Pic/*/Ori",
    "*/nt_data/Pic/*/Thumb",
    "*/nt_data/Emoji/emoji-recv/*/Ori",
)
RECENT_WINDOW = 180
TMP_DIR = "/tmp"


def _is_local(path):
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(("http://", "https://", "base64://", "data:")):
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _iter_images(messages):
    """从消息链（含引用消息链）里收集 Image 组件。"""
    out = []
    for comp in messages or []:
        if isinstance(comp, Image):
            out.append(comp)
            continue
        chain = getattr(comp, "chain", None)
        if chain:
            out.extend([x for x in chain if isinstance(x, Image)])
    return out


class Component:
    def __init__(self, context):
        self.context = context
        self._dirs = []
        self._dirs_at = 0.0
        self._hits = 0

    async def initialize(self):
        logger.info("[dsh_vision] 已启动：发图前兜底取图（get_image / 缓存哈希 / 最新一张）")

    async def terminate(self):
        logger.info(f"[dsh_vision] 已停止，本次共补图 {self._hits} 张")

    # ---------- 缓存目录 ----------
    def _base_dirs(self):
        now = time.time()
        if self._dirs and now - self._dirs_at < 600:
            return self._dirs
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
        self._dirs, self._dirs_at = out, now
        return out

    def _find_by_hash(self, h):
        for d in self._base_dirs():
            for ext in IMG_EXTS:
                p = d / (h + ext)
                if _is_local(str(p)):
                    return str(p)
        return None

    def _newest_recent(self):
        """最近 RECENT_WINDOW 秒内缓存里只多出一张图时，认为就是它。"""
        now = time.time()
        recent = []
        for d in self._base_dirs():
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

    # ---------- 取图 ----------
    async def _napcat_get(self, event, ref):
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None) or getattr(
            getattr(bot, "api", None), "call_action", None
        )
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
        local = data.get("file") or ""
        if _is_local(local):
            return local
        return None

    async def _recover(self, event, comp):
        refs = []
        for attr in ("file", "url", "path"):
            v = getattr(comp, attr, None)
            if isinstance(v, str) and v and v not in refs:
                refs.append(v)

        for ref in refs:
            p = await self._napcat_get(event, ref)
            if p:
                return p

        hashes = []
        for ref in refs:
            hashes.extend(HASH_RE.findall(ref.lower()))
        for h in dict.fromkeys(hashes):
            p = self._find_by_hash(h)
            if p:
                return p

        if hashes:
            return self._newest_recent()
        return None

    async def fix_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """在请求发给模型之前，把图片换成确实可读的本地文件。"""
        try:
            comps = _iter_images(event.get_messages())
            if not comps:
                return
            current = list(getattr(request, "image_urls", None) or [])
            if current and all(_is_local(u) for u in current):
                return

            paths = []
            for comp in comps:
                path = None
                for attr in ("path", "file", "url"):
                    v = getattr(comp, attr, None)
                    if _is_local(v):
                        path = v
                        break
                if not path:
                    path = await self._recover(event, comp)
                if path:
                    paths.append(path)

            paths = list(dict.fromkeys(paths))
            if not paths:
                logger.warning("[dsh_vision] 这条消息带图但没取到，只能按文字处理")
                return
            request.image_urls = paths
            self._hits += len(paths)
            logger.info(f"[dsh_vision] 补图 {len(paths)} 张")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dsh_vision] 补图失败: {e}")
