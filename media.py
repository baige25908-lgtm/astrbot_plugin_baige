# -*- coding: utf-8 -*-
"""白鸽·配图（dsh_media）

1) 回复时按 45% 概率配一张跟语境情绪对得上的表情/图片；
2) 表情库不设上限：群友发的图随手收，缺的品类定时去网上搜。
"""
import asyncio
import hashlib
import json
import random
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover
    _PILImage = None

from . import settings
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Reply

PROBABILITY = 0.45
REUSE_DAYS = 7
HARVEST_INTERVAL = 2 * 3600
PER_CATEGORY_MAX = 300
HARVEST_PER_RUN = 6
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Safari/537.36"

STEALER_DIR = settings._DATA / "plugin_data" / "astrbot_plugin_stealer" / "categories"
# 天使之笑(astrbot_plugin_angel_smile) 的贴纸库：目录名=贴纸名，里面的图=同一贴纸的多个变体
SMILE_MEME_DIR = settings._DATA / "plugin_data" / "astrbot_plugin_angel_smile" / "memes"
CATEGORIES = [
    "angry", "confused", "cry", "disgust", "dumb", "embarrassed",
    "excitement", "fear", "happy", "love", "sad", "shy", "sigh",
    "surprised", "thank", "tired", "troll",
]

SEARCH_WORDS = {
    "happy": ["开心 表情包", "高兴 沙雕图", "快乐 表情"],
    "excitement": ["激动 表情包", "好耶 表情", "起飞 表情包"],
    "love": ["喜欢 表情包", "贴贴 表情", "比心 表情包"],
    "shy": ["害羞 表情包", "脸红 表情"],
    "sad": ["难过 表情包", "失落 表情图"],
    "cry": ["哭 表情包", "呜呜 表情"],
    "angry": ["生气 表情包", "气死 表情"],
    "troll": ["沙雕 表情包", "搞笑 表情", "狗头 表情包"],
    "confused": ["疑惑 表情包", "问号 表情", "懵 表情包"],
    "dumb": ["无语 表情包", "麻了 表情"],
    "surprised": ["震惊 表情包", "惊讶 表情"],
    "tired": ["累 表情包", "困 表情", "摆烂 表情包"],
    "sigh": ["叹气 表情包", "无奈 表情"],
    "fear": ["害怕 表情包", "瑟瑟发抖 表情"],
    "disgust": ["嫌弃 表情包", "恶心 表情"],
    "embarrassed": ["尴尬 表情包", "社死 表情"],
    "thank": ["谢谢 表情包", "感谢 表情"],
}

RULES = {
    "happy": ["开心", "高兴", "哈哈", "笑死", "好耶", "不错", "喜欢", "太棒", "嘿嘿", "爽"],
    "excitement": ["激动", "冲", "牛", "厉害", "哇", "绝了", "燃", "起飞"],
    "love": ["爱你", "抱", "亲", "想你了", "喜欢你", "宝", "贴贴"],
    "shy": ["害羞", "脸红", "不好意思", "羞", "讨厌"],
    "sad": ["难过", "伤心", "低落", "失落", "唉", "可惜", "委屈"],
    "cry": ["哭", "呜呜", "泪", "难受", "破防"],
    "angry": ["生气", "气死", "烦", "滚", "无语", "离谱"],
    "troll": ["逗", "骗", "略略略", "傻", "笨", "笑你", "嘿嘿"],
    "confused": ["?", "？", "啥", "什么意思", "不懂", "没听懂"],
    "dumb": ["蠢", "憨", "呆", "麻了", "不理解"],
    "surprised": ["居然", "竟然", "真的假的", "卧槽", "不会吧"],
    "tired": ["累", "困", "睡了", "熬夜", "顶不住"],
    "sigh": ["唉", "算了", "叹气", "无奈", "行吧"],
    "fear": ["害怕", "吓", "恐怖", "不敢", "怕"],
    "disgust": ["恶心", "嫌弃", "吐", "油腻", "下头"],
    "embarrassed": ["尴尬", "社死", "丢人"],
    "thank": ["谢谢", "感谢", "多谢", "谢啦"],
}

MIN_BYTES = 8 * 1024
MAX_BYTES = 3 * 1024 * 1024
MIN_SIDE = 100
MAX_PIXELS = 40_000_000
BAD_WORDS = ["色情", "裸", "性爱", "血腥", "尸体", "凶杀", "枪支", "毒品", "自残"]
QUARANTINE = None  # 运行时初始化

MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"GIF8": ".gif",
    b"RIFF": ".webp",
}


def _sniff(head: bytes):
    for k, ext in MAGIC.items():
        if head.startswith(k):
            return ext
    return None


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "media"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lib = self.data_dir / "library"
        for c in CATEGORIES:
            (self.lib / c).mkdir(parents=True, exist_ok=True)
        self.used_path = self.data_dir / "used.json"
        self.index_path = self.data_dir / "index.json"
        self._used = self._load_json(self.used_path, {})
        self._index = self._load_json(self.index_path, {})
        self._task = None

    # ---------- 基础 ----------
    @staticmethod
    def _load_json(p: Path, default):
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return default

    @staticmethod
    def _dump_json(p: Path, data):
        try:
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[dsh_media] 写 {p.name} 失败: {e}")

    def _count(self, cat):
        d = self.lib / cat
        return len(list(d.iterdir())) if d.is_dir() else 0

    def _validate(self, raw: bytes):
        """返回 (是否合格, 原因)。不合格的图绝不进库。"""
        if len(raw) < MIN_BYTES:
            return False, "too_small"
        if len(raw) > MAX_BYTES:
            return False, "too_big"
        if _PILImage is None:
            return (True, "") if _sniff(raw[:8]) else (False, "not_image")
        try:
            from io import BytesIO

            with _PILImage.open(BytesIO(raw)) as im:
                im.verify()
            with _PILImage.open(BytesIO(raw)) as im:
                w, h = im.size
                fmt = (im.format or "").upper()
            if fmt not in {"JPEG", "PNG", "GIF", "WEBP", "BMP"}:
                return False, f"format:{fmt}"
            if w < MIN_SIDE or h < MIN_SIDE:
                return False, f"too_small_side:{w}x{h}"
            if w * h > MAX_PIXELS:
                return False, f"too_many_pixels:{w}x{h}"
            return True, ""
        except Exception as e:
            return False, f"broken:{type(e).__name__}"

    def _store(self, cat, raw: bytes, title: str = ""):
        """校验 + 去重 + 落盘，返回路径"""
        ok, reason = self._validate(raw)
        if not ok:
            logger.info(f"[dsh_media] 跳过一张图（{reason}）")
            return None
        ext = _sniff(raw[:8])
        if not ext:
            return None
        h = hashlib.sha256(raw).hexdigest()[:24]
        if h in self._index:
            return None
        d = self.lib / cat
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{h}{ext}"
        p.write_bytes(raw)
        self._index[h] = {"cat": cat, "ts": int(time.time())}
        self._mirror(cat, h, ext, raw)
        return p

    def _mirror(self, cat, h, ext, raw):
        """同步一份到天使之笑的贴纸库（:cat: 这样就能用它发出来）。"""
        try:
            d = SMILE_MEME_DIR / cat
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{h}{ext}"
            if not f.exists():
                f.write_bytes(raw)
        except Exception as e:
            logger.warning(f"[dsh_media] 同步到天使之笑失败: {e}")

    def _seed_smile_from_stealer(self):
        """第一次跑时，把 stealer 已有的图灌进天使之笑，别让它空着。"""
        flag = self.data_dir / ".seeded_smile_v2"
        if flag.exists():
            return
        n = 0
        try:
            for cat in CATEGORIES:
                src = STEALER_DIR / cat
                if not src.is_dir():
                    continue
                for f in src.iterdir():
                    try:
                        if not f.is_file():
                            continue
                        if f.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                            continue
                        d = SMILE_MEME_DIR / cat
                        d.mkdir(parents=True, exist_ok=True)
                        dst = d / f"seed_{f.name}"
                        if dst.exists():
                            continue
                        raw = f.read_bytes()
                        if not _sniff(raw[:8]):
                            continue
                        dst.write_bytes(raw)
                        n += 1
                    except Exception:
                        # 断链/坏文件直接跳过，不拖累其它
                        continue
            flag.write_text(str(int(time.time())), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[dsh_media] 灌入天使之笑失败: {e}")
        if n:
            logger.info(f"[dsh_media] 已把 {n} 张现有表情灌进天使之笑（按情绪分类命名）")

    # ---------- 选图 ----------
    def _files(self, cat):
        out = []
        for base in (self.lib / cat, STEALER_DIR / cat):
            if base.is_dir():
                out += [
                    p
                    for p in base.iterdir()
                    if p.is_file()
                    and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                ]
        return out

    def _pick(self, cat):
        files = self._files(cat)
        if not files:
            return None
        fresh = [p for p in files if str(p) not in self._used]
        return random.choice(fresh or files)

    @staticmethod
    def _infer(text):
        if not text:
            return None
        scores = {}
        for cat, words in RULES.items():
            hit = sum(1 for w in words if w in text)
            if hit:
                scores[cat] = hit
        return max(scores.items(), key=lambda kv: kv[1])[0] if scores else None

    # ---------- 网上搜图 ----------
    async def _harvest(self):
        saved = 0
        async with aiohttp.ClientSession() as session:
            for cat in CATEGORIES:
                if self._count(cat) >= PER_CATEGORY_MAX:
                    continue
                words = SEARCH_WORDS.get(cat) or [f"{cat} 表情包"]
                q = random.choice(words)
                try:
                    url = (
                        "https://image.so.com/j?q="
                        + quote(q)
                        + "&pn=0&rn=20"
                    )
                except Exception:
                    continue
                try:
                    async with session.get(url, headers={"User-Agent": UA}, timeout=15) as r:
                        data = await r.json(content_type=None)
                except Exception:
                    continue
                items = (data or {}).get("list") or []
                random.shuffle(items)
                got = 0
                for it in items:
                    if got >= HARVEST_PER_RUN:
                        break
                    title = f"{it.get('title') or ''} {it.get('word') or ''}"
                    if any(w in title for w in BAD_WORDS):
                        continue
                    src = it.get("thumb") or it.get("imgurl") or it.get("picurl")
                    if not src:
                        continue
                    try:
                        async with session.get(
                            src,
                            headers={"User-Agent": UA, "Referer": "https://image.so.com/"},
                            timeout=20,
                        ) as ir:
                            if ir.status != 200:
                                continue
                            raw = await ir.read()
                    except Exception:
                        continue
                    if self._store(cat, raw):
                        got += 1
                        saved += 1
        if saved:
            self._dump_json(self.index_path, self._index)
            logger.info(f"[dsh_media] 扩库：新增 {saved} 张，库容 {self._total()}")

    def _total(self):
        return sum(self._count(c) for c in CATEGORIES)

    def _beat(self):
        try:
            (self.data_dir / "heartbeat").write_text(str(int(time.time())))
        except Exception:
            pass

    async def _loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                self._beat()
                await self._harvest()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[dsh_media] 扩库异常: {e}")
            await asyncio.sleep(HARVEST_INTERVAL)

    async def initialize(self):
        self._beat()
        await asyncio.to_thread(self._seed_smile_from_stealer)
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[dsh_media] 已启动，当前库存 {self._total()} 张，每 2 小时补货")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[dsh_media] 已停止")

    # ---------- 收群友的图 ----------
    async def _resolve(self, event, comp):
        try:
            p = await comp.convert_to_file_path()
            if p and Path(p).is_file():
                return Path(p).read_bytes()
        except Exception:
            pass
        try:
            bot = getattr(event, "bot", None)
            call = getattr(bot, "call_action", None) or getattr(
                getattr(bot, "api", None), "call_action", None
            )
            ref = getattr(comp, "file", None) or getattr(comp, "url", None) or ""
            if call and ref:
                ret = await call("get_image", file=ref)
                data = (ret or {}).get("data") if isinstance(ret, dict) else None
                if isinstance(data, dict):
                    import base64

                    if data.get("base64"):
                        return base64.b64decode(data["base64"])
                    local = data.get("file") or ""
                    if local and Path(local).is_file():
                        return Path(local).read_bytes()
        except Exception:
            pass
        return None
    async def steal_images(self, event: AstrMessageEvent):
        try:
            comps = []
            for c in event.get_messages() or []:
                if isinstance(c, Image):
                    comps.append(c)
                elif isinstance(c, Reply) and getattr(c, "chain", None):
                    comps += [x for x in c.chain if isinstance(x, Image)]
            if not comps:
                return
            text = event.message_str or ""
            cat = self._infer(text) or "troll"
            added = 0
            for c in comps[:3]:
                raw = await self._resolve(event, c)
                if raw and self._store(cat, raw):
                    added += 1
            if added:
                self._dump_json(self.index_path, self._index)
                logger.info(f"[dsh_media] 收图 {added} 张 -> {cat}（库容 {self._total()}）")
        except Exception as e:
            logger.warning(f"[dsh_media] 收图失败: {e}")

    # ---------- 配图 ----------
    async def attach_emoji(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            chain = result.chain
            if any(isinstance(c, Image) for c in chain):
                return
            if random.random() > PROBABILITY:
                return
            reply_text = "".join(getattr(c, "text", "") or "" for c in chain)
            cat = self._infer(reply_text) or self._infer(getattr(event, "message_str", "") or "")
            if not cat:
                return
            path = self._pick(cat)
            if not path:
                for alt in ("troll", "happy", "tired"):
                    path = self._pick(alt)
                    if path:
                        cat = alt
                        break
            if not path:
                return
            chain.append(Image.fromFileSystem(str(path)))
            self._used[str(path)] = time.time()
            self._dump_json(self.used_path, self._used)
            logger.info(f"[dsh_media] 配图 {cat}: {path.name}")
        except Exception as e:
            logger.warning(f"[dsh_media] 配图失败: {e}")
