# -*- coding: utf-8 -*-
"""白鸽·学梗（dsh_vibe）

每两小时做一次「学习」：
  1) B 站热门视频（标题 + 摘要 + 热评）
  2) 抖音热榜词条
  3) 群聊归档里最近 24h 的高频片段（群内梗/口头禅候选）
交给当前 LLM 提炼成：热梗表 + 一张很短的语气卡，写进本地 sqlite。
之后在每次 LLM 请求前，把「前 5 条热梗 + 语气卡」以很短的块注入（≤300 字）。
"""
import asyncio
import json
import random
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from . import settings
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

INTERVAL_SECONDS = 2 * 3600
try:
    from datetime import timezone as _tz

    LOCAL_TZ = _tz(timedelta(hours=8))  # 手机在 +0800；guest 默认 UTC，必须显式换算
except Exception:  # pragma: no cover
    LOCAL_TZ = None


def _now_local():
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
QUIET_START, QUIET_END = 3, 8
UA = "Mozilla/5.0 (Linux; Android 14; MIUI) AppleWebKit/537.36 Chrome/120 Safari/537.36"
def _chatlog_dir():
    """学梗用的聊天记录目录：面板 archive_dir > 本插件数据目录下的 chatlog。"""
    raw = str(settings._c("archive_dir") or "").strip()
    return Path(raw) if raw else (settings.PLUGIN_DATA / "fish" / "chatlog")


CHATLOG_DIR = _chatlog_dir()
INJECT_MAX_CHARS = 480
BALANCE_NAME = "balance.json"
MIN_BALANCE = 0.5  # 低于这个数就暂停一切主动 LLM 调用（学习/复盘），把余额留给聊天
HUMAN_RULES = (
    "像人一样说话：句子短、别写小作文；少用「首先/其次/总之/希望对你有帮助」这类 AI 腔；"
    "可以反问、抬杠、敷衍、只回一两个字；情绪起伏正常，别每句都热情；"
    "不用结尾总结，不要解释自己为什么这么说。"
)
QUOTE_CHANCE = 0.2
# 模型回忆模式默认关闭：只靠真实语料慢慢长，别自己编
RECALL_QUOTES = False
EXTRA_QUOTES_NAME = "quotes-extra.txt"
# 手写精选：格式 "台词|出处"
CURATED = [
    ("我的小鱼，你醒了，还认识早晨吗？", "魁拔"),
    ("昨夜你曾说，愿夜幕永不开启。", "魁拔"),
    ("你的香腮边轻轻滑落的，是你的泪，还是我的泪？", "魁拔"),
]

STOP = set(
    "的了是在我也就都而及与这那你我他她它们个不有和会要去到说很还就把被让给对从"
    "什么怎么这个那个还是然后因为所以但是可以没有自己现在时候一下一点真的不是"
)


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "vibe"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "vibe.db"
        self._task = None
        self._db_lock = asyncio.Lock()
        self._init_db()

    # ---------- 存储 ----------
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memes("
            "term TEXT PRIMARY KEY, meaning TEXT, usage TEXT, example TEXT,"
            "source TEXT, heat INTEGER DEFAULT 1, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lessons("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, ts INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS quotes("
            "q TEXT PRIMARY KEY, src TEXT, who TEXT, used_at INTEGER DEFAULT 0, ts INTEGER)"
        )
        try:
            conn.execute("ALTER TABLE quotes ADD COLUMN verified INTEGER DEFAULT 1")
        except Exception:
            pass
        return conn

    def _init_db(self):
        try:
            with self._conn() as c:
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 建库失败: {e}")

    def _kv_get(self, key, default=""):
        try:
            with self._conn() as c:
                row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
                return row[0] if row else default
        except Exception:
            return default

    def _kv_set(self, key, value):
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                    (key, value, int(time.time())),
                )
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 写 kv 失败: {e}")

    def _top_memes(self, limit=5):
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT term,meaning,usage,example,heat FROM memes "
                    "ORDER BY heat DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return rows
        except Exception:
            return []

    def _seed_curated(self):
        """内置精选 + quotes-extra.txt（一行一条，允许「台词|出处」）"""
        items = [{"hitokoto": q, "from": src, "from_who": ""} for q, src in CURATED]
        try:
            extra_quotes = settings.report(EXTRA_QUOTES_NAME)
            if extra_quotes.is_file():
                for line in extra_quotes.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    txt, _, src = line.partition("|")
                    txt = txt.strip()
                    if txt:
                        items.append(
                            {"hitokoto": txt, "from": (src.strip() or "手记"), "from_who": ""}
                        )
        except Exception as e:
            logger.warning(f"[dsh_vibe] 读取 quotes-extra 失败: {e}")
        if items:
            self._save_quotes(items)

    def _pending_quotes(self, limit=12):
        try:
            with self._conn() as c:
                return [
                    r[0]
                    for r in c.execute(
                        "SELECT q FROM quotes WHERE verified=0 ORDER BY ts DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                ]
        except Exception:
            return []

    def _approve_from_file(self):
        """quotes-approve.txt 一行一条，命中即转为已核实（自用通道，需打开 bridge 开关）。"""
        f = settings.bridge_dir() / "quotes-approve.txt"
        if not settings.bridge_io_enabled():
            return 0
        if not f.is_file():
            return 0
        n = 0
        try:
            with self._conn() as c:
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    t = line.strip()
                    if not t or t.startswith("#"):
                        continue
                    cur = c.execute(
                        "UPDATE quotes SET verified=1 WHERE q LIKE ? AND verified=0",
                        (t + "%",),
                    )
                    n += cur.rowcount
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 批准台词失败: {e}")
        if n:
            logger.info(f"[dsh_vibe] 又从 approve 文件批准了 {n} 条台词")
        return n

    def _save_quotes_many(self, items, verified):
        self._save_quotes(items, verified=verified)

    def _count_quotes(self):
        try:
            with self._conn() as c:
                return c.execute("SELECT count(*) FROM quotes").fetchone()[0]
        except Exception:
            return 0

    def _sample_quotes(self, limit=8):
        try:
            with self._conn() as c:
                return c.execute(
                    "SELECT q,src,who FROM quotes WHERE verified=1 "
                    "ORDER BY RANDOM() LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception:
            return []

    def _save_quotes(self, quotes, verified=1):
        now = int(time.time())
        try:
            with self._conn() as c:
                for it in quotes:
                    q = (it.get("hitokoto") or "").strip()[:160]
                    if not q:
                        continue
                    c.execute(
                        "INSERT INTO quotes(q,src,who,used_at,ts,verified) VALUES(?,?,?,0,?,?) "
                        "ON CONFLICT(q) DO UPDATE SET ts=excluded.ts",
                        (
                            q,
                            (it.get("from") or "")[:40],
                            (it.get("from_who") or "")[:30],
                            now,
                            int(verified),
                        ),
                    )
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 存台词失败: {e}")

    def _pick_quotes(self, limit=2):
        """挑几条很久没用的台词；用过的标记 used_at。"""
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT q,src,who FROM quotes WHERE verified=1 AND used_at < ? "
                    "ORDER BY RANDOM() LIMIT ?",
                    (int(time.time()) - 7 * 86400, limit),
                ).fetchall()
                if not rows:
                    rows = c.execute(
                        "SELECT q,src,who FROM quotes WHERE verified=1 "
                        "ORDER BY RANDOM() LIMIT ?", (limit,)
                    ).fetchall()
                if rows:
                    c.executemany(
                        "UPDATE quotes SET used_at=? WHERE q=?",
                        [(int(time.time()), r[0]) for r in rows],
                    )
                    c.commit()
                return rows
        except Exception:
            return []

    def _save_lessons(self, lessons):
        now = int(time.time())
        try:
            with self._conn() as c:
                for t in lessons:
                    t = (t or "").strip()[:120]
                    if t:
                        c.execute("INSERT INTO lessons(text,ts) VALUES(?,?)", (t, now))
                # 只留最近 10 条
                c.execute(
                    "DELETE FROM lessons WHERE id NOT IN "
                    "(SELECT id FROM lessons ORDER BY id DESC LIMIT 10)"
                )
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 存复盘失败: {e}")

    def _recent_lessons(self, limit=2):
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT text,ts FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [r[0] for r in rows if int(time.time()) - r[1] < 3 * 86400]
        except Exception:
            return []

    def _today_dialog(self, limit=140):
        f = _chatlog_dir() / f"chat-{_now_local().strftime('%Y-%m-%d')}.jsonl"
        if not f.is_file():
            return ""
        lines = []
        try:
            for raw in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                role = d.get("role")
                if role not in ("user", "bot"):
                    continue
                text = (d.get("text") or "").replace("\n", " ").strip()
                if not text:
                    continue
                who = "我" if role == "bot" else (d.get("sender_name") or "对方")
                lines.append(f"{who}: {text[:80]}")
        except Exception:
            return ""
        return "\n".join(lines[-limit:])

    async def _recall_quotes(self, topic="魁拔"):
        """让模型回忆某部作品的台词，但只进「待确认」区，核实后才用于对话。"""
        provider = self.context.get_using_provider()
        if not provider:
            return
        prompt = (
            f"请列出你高度确信出自《{topic}》系列（含电影、妖侠传、殊途）的台词，3-8 条。\n"
            "要求：\n"
            "1. 必须是原句，不要改写、不要拼接、不要自己创作；\n"
            "2. 把握不到八成以上就不要写，宁少勿错；\n"
            "3. 只输出 JSON：{\"quotes\":[{\"q\":\"台词\",\"from\":\"出处\"}]}\n"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt="你是动画电影台词考据者，只输出 JSON，绝不编造。"
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 回忆台词失败: {e}")
            return
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            items = [
                {"hitokoto": x.get("q", ""), "from": x.get("from") or topic, "from_who": ""}
                for x in (parsed.get("quotes") or [])
                if isinstance(x, dict)
            ]
        except Exception:
            logger.warning(f"[dsh_vibe] 回忆台词解析失败: {raw[:120]}")
            return
        if items:
            await asyncio.to_thread(self._save_quotes, items, 0)
            logger.info(
                f"[dsh_vibe] 从模型回忆里收了 {len(items)} 条《{topic}》台词，标为待确认"
            )
            self._dump_snapshot()

    async def _review_once(self):
        if self._pause_if_broke("复盘"):
            return
        provider = self.context.get_using_provider()
        if not provider:
            return
        dialog = await asyncio.to_thread(self._today_dialog)
        if len(dialog) < 200:
            logger.info("[dsh_vibe] 今天对话太少，跳过复盘")
            return
        prompt = (
            "下面是今天我和群友的聊天记录。请以「自我复盘」的角度，找出我说话时"
            "不像真人的地方。要求：\n"
            "1. 列出 3-6 条具体、可执行的修正（每条 ≤40 字，比如「别用首先/其次」"
            "「别每题都回答得像教程」「情绪低落时不要硬转积极」）；\n"
            "2. 不要泛泛而谈，要指向具体句式或习惯；\n"
            "3. 只输出 JSON：{\"lessons\":[\"...\"]}，不要解释。\n\n"
            + dialog[:9000]
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt="你是严格的中文对话教练，只输出 JSON。"
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            self._report_usage(resp, "vibe-review")
        except Exception as e:
            logger.warning(f"[dsh_vibe] 复盘调用失败: {e}")
            return
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            lessons = parsed.get("lessons") or []
        except Exception:
            logger.warning(f"[dsh_vibe] 复盘解析失败: {raw[:160]}")
            return
        if lessons:
            await asyncio.to_thread(self._save_lessons, lessons)
            await asyncio.to_thread(self._kv_set, "last_review", _now_local().strftime("%F"))
            logger.info(f"[dsh_vibe] 复盘完成，新增 {len(lessons)} 条自我修正")
            self._dump_snapshot()

    async def _maybe_review(self):
        now = _now_local()
        last = self._kv_get("last_review", "")
        try:
            has_lessons = bool(self._recent_lessons(1))
        except Exception:
            has_lessons = False
        if now.strftime("%F") == last:
            return
        if now.hour != 4 and has_lessons:
            return
        await self._review_once()

    def _save_memes(self, memes):
        now = int(time.time())
        try:
            with self._conn() as c:
                for m in memes:
                    term = (m.get("term") or "").strip()[:40]
                    if not term:
                        continue
                    c.execute(
                        "INSERT INTO memes(term,meaning,usage,example,source,heat,updated_at) "
                        "VALUES(?,?,?,?,?,1,?) "
                        "ON CONFLICT(term) DO UPDATE SET "
                        "meaning=excluded.meaning, usage=excluded.usage, "
                        "example=excluded.example, heat=memes.heat+1, updated_at=excluded.updated_at",
                        (
                            term,
                            (m.get("meaning") or "")[:120],
                            (m.get("usage") or "")[:80],
                            (m.get("example") or "")[:100],
                            (m.get("source") or "")[:20],
                            now,
                        ),
                    )
                # 热度衰减，避免老梗永远霸榜
                c.execute("UPDATE memes SET heat = MAX(1, heat - 1) WHERE updated_at < ?", (now - 86400 * 3,))
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 存热梗失败: {e}")

    # ---------- 抓取 ----------
    async def _get_json(self, session, url, headers=None, timeout=15):
        try:
            async with session.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception:
            return None

    async def _fetch_bili(self, session, max_videos=6):
        out = []
        data = await self._get_json(
            session, "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"
        )
        vids = (data or {}).get("data", {}).get("list") or []
        for v in vids[:max_videos]:
            aid = v.get("aid")
            item = {
                "title": (v.get("title") or "")[:60],
                "desc": (v.get("desc") or "")[:120],
                "comments": [],
            }
            if aid:
                rd = await self._get_json(
                    session,
                    f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps=20",
                )
                for rp in ((rd or {}).get("data", {}) or {}).get("replies") or []:
                    msg = (rp.get("content", {}) or {}).get("message") or ""
                    if msg:
                        item["comments"].append(msg[:80])
            out.append(item)
        return out

    async def _fetch_douyin(self, session):
        data = await self._get_json(
            session,
            "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
            headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
        )
        words = [w.get("word") for w in ((data or {}).get("word_list") or []) if w.get("word")]
        return words[:25]

    async def _fetch_quotes(self, session, n=18):
        out = []
        seen = set()
        for _ in range(n):
            try:
                async with session.get(
                    "https://v1.hitokoto.cn/?c=a&encode=json",
                    headers={"User-Agent": UA},
                    timeout=10,
                ) as r:
                    if r.status != 200:
                        continue
                    d = await r.json(content_type=None)
                    if isinstance(d, dict) and d.get("hitokoto"):
                        text = d["hitokoto"]
                        if text not in seen:
                            seen.add(text)
                            out.append(d)
            except Exception:
                continue
            await asyncio.sleep(0.3)
        return out

    def _fetch_group_phrases(self):
        """从 fish_core 的群聊归档里找最近 24h 的高频短片段。"""
        texts = []
        try:
            for days in (0, 1):
                f = _chatlog_dir() / f"chat-{(datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')}.jsonl"
                if not f.is_file():
                    continue
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("role") != "user":
                        continue
                    t = (d.get("text") or "").strip()
                    if 2 <= len(t) <= 60:
                        texts.append(t)
        except Exception:
            return []

        cnt = Counter()
        for t in texts:
            t = re.sub(r"\[[^\]]*\]", "", t)
            for n in (3, 4):
                for i in range(len(t) - n + 1):
                    g = t[i : i + n]
                    if g.isdigit() or g in STOP:
                        continue
                    if any(ch in STOP for ch in g):
                        continue
                    cnt[g] += 1
        return [g for g, c in cnt.most_common(40) if c >= 3][:30]

    # ---------- 学习 ----------
    async def _learn_once(self):
        if self._pause_if_broke("学习"):
            return
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("[dsh_vibe] 没有可用的 LLM provider，跳过本轮学习")
            return
        try:
            async with aiohttp.ClientSession() as session:
                bili, douyin, quotes = await asyncio.gather(
                    self._fetch_bili(session),
                    self._fetch_douyin(session),
                    self._fetch_quotes(session),
                )
        except Exception as e:
            logger.warning(f"[dsh_vibe] 抓取失败: {e}")
            return
        phrases = await asyncio.to_thread(self._fetch_group_phrases)

        if not bili and not douyin and not phrases:
            logger.info("[dsh_vibe] 没抓到内容，跳过")
            return

        material = {
            "b站热门": [
                {"标题": v["title"], "简介": v["desc"], "热评": v["comments"][:12]}
                for v in bili
            ],
            "抖音热榜": douyin[:20],
            "群聊高频片段": phrases,
        }
        prompt = (
            "下面是从 B 站热门、抖音热榜、QQ 群聊里抓到的原始材料。请提炼出"
            "这个群体最近真正在用的热梗和说话习惯。要求：\n"
            "1. 只挑「确实能听懂的、不低俗、不涉政」的梗，最多 6 条；\n"
            "2. 每条给 term（梗本身，≤12字）、meaning（一句话）、usage（什么时候用，≤30字）、"
            "example（一个自然例句，≤30字）；\n"
            "3. 再写一张语气卡 style：用 100 字以内描述群里的说话习惯（口头禅、语气词、"
            "标点风格、称呼方式）；\n"
            "4. 只输出 JSON：{\"memes\":[...],\"style\":\"...\"}，不要任何解释。\n\n"
            + json.dumps(material, ensure_ascii=False)[:12000]
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt="你是中文网络社区的语言学观察者，只输出 JSON。"
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            self._report_usage(resp, "vibe-learn")
        except Exception as e:
            logger.warning(f"[dsh_vibe] LLM 提炼失败: {e}")
            return

        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        except Exception:
            logger.warning(f"[dsh_vibe] 解析失败，原始输出前 200 字: {raw[:200]}")
            return

        memes = parsed.get("memes") or []
        style = (parsed.get("style") or "").strip()
        async with self._db_lock:
            if quotes:
                await asyncio.to_thread(self._save_quotes, quotes)
            if memes:
                await asyncio.to_thread(self._save_memes, memes)
            if style:
                await asyncio.to_thread(self._kv_set, "style", style[:300])
            await asyncio.to_thread(self._kv_set, "last_learn", datetime.now().strftime("%F %T"))
        self._dump_snapshot()
        logger.info(
            f"[dsh_vibe] 学习完成：梗 {len(memes)} 条，台词 {len(quotes)} 条，语气卡 {len(style)} 字，"
            f"群片段 {len(phrases)} 个"
        )

    def _balance_ok(self):
        """余额是否够用。读不到就当作够用，别因为查不到余额把功能停了。"""
        try:
            d = json.loads(settings.report(BALANCE_NAME).read_text(encoding="utf-8"))
            total = float(d.get("total"))
            return total >= MIN_BALANCE, total
        except Exception:
            return True, None

    def _pause_if_broke(self, what):
        ok, total = self._balance_ok()
        if ok:
            return False
        logger.warning(
            f"[dsh_vibe] 余额只剩 {total}（阈值 {MIN_BALANCE}），这次跳过{what}，留给聊天"
        )
        return True

    def _report_usage(self, resp, src="vibe"):
        """把本次调用的 usage 追加给账本插件（它不经过对话管线，收不到）。"""
        try:
            u = getattr(resp, "usage", None)
            if u is None:
                return
            line = "%d,%d,%d,%d,%s\n" % (
                int(time.time()),
                int(getattr(u, "input_other", 0) or 0),
                int(getattr(u, "input_cached", 0) or 0),
                int(getattr(u, "output", 0) or 0),
                src,
            )
            with open(str(settings.report("meter-extra.jsonl")), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _beat(self):
        try:
            (self.data_dir / "heartbeat").write_text(str(int(time.time())))
        except Exception:
            pass

    async def _loop(self):
        await asyncio.sleep(90)  # 启动后稍等，别和初始化抢资源
        while True:
            try:
                self._beat()
                now_h = _now_local().hour
                if not (QUIET_START <= now_h < QUIET_END):
                    await self._learn_once()
                await self._maybe_review()
                await asyncio.to_thread(self._approve_from_file)
                if RECALL_QUOTES and self._kv_get("last_recall", "") != _now_local().strftime("%F"):
                    await self._recall_quotes("魁拔")
                    await asyncio.to_thread(
                        self._kv_set, "last_recall", _now_local().strftime("%F")
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[dsh_vibe] 循环异常: {e}")
            await asyncio.sleep(INTERVAL_SECONDS)

    def _dump_snapshot(self):
        """把当前热梗/语气卡写一份快照（默认写在本插件数据目录，可用 bridge 开关改到 /stage）。"""
        try:
            rows = self._top_memes(20)
            style = self._kv_get("style", "")
            lines = ["# 白鸽·学梗 快照", f"更新时间：{_now_local().strftime('%F %T')}", ""]
            if style:
                lines += ["## 语气卡", style, ""]
            les = self._recent_lessons(6)
            if les:
                lines += ["", "## 复盘出来的注意点"]
                lines += [f"- {x}" for x in les]
            pend = self._pending_quotes(12)
            if pend:
                lines += ["", f"## 待确认台词（{len(pend)} 条，核实后才会用）"]
                lines += [f"- {x}" for x in pend]
            qrows = self._sample_quotes(8)
            if qrows:
                lines += ["", f"## 台词样本（库里共 {self._count_quotes()} 条）"]
                for q, src, who in qrows:
                    lines.append(f"- 「{q}」——{src}{('／' + who) if who else ''}")
            lines.append("")
            lines.append(f"## 热梗（{len(rows)} 条）")
            for term, meaning, usage, example, heat in rows:
                lines.append(f"- **{term}**（heat={heat}）：{meaning}；用时:{usage}；例:{example}")
            settings.report("vibe-snapshot.md").write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[dsh_vibe] 写快照失败: {e}")

    async def _first_run(self):
        try:
            if not self._top_memes(1) or self._count_quotes() == 0:
                await asyncio.sleep(25)
                logger.info("[dsh_vibe] 梗库或台词库还空着，先跑一次学习（不受静默限制）")
                await self._learn_once()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 首次学习失败: {e}")

    async def initialize(self):
        self._beat()
        try:
            self._seed_curated()
        except Exception as e:
            logger.warning(f"[dsh_vibe] 灌入精选台词失败: {e}")
        asyncio.create_task(self._first_run())
        await asyncio.to_thread(self._dump_snapshot)
        self._task = asyncio.create_task(self._loop())
        logger.info("[dsh_vibe] 已启动，每 2 小时学一次（03:00-08:00 静默）")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[dsh_vibe] 已停止")

    # ---------- 注入 ----------
    async def inject_vibe(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            memes = self._top_memes(5)
            style = self._kv_get("style", "")
            if not memes and not style:
                return
            lines = ["<vibe>"]
            lines.append(HUMAN_RULES)
            if random.random() < QUOTE_CHANCE:
                rows = self._pick_quotes(2)
                if rows:
                    qs = "；".join(
                        f"「{q}」（{src or '—'}）" for q, src, _who in rows
                    )
                    lines.append(
                        "可以借鉴这些台词的语气和节奏（别照抄原句、别提出处）：" + qs
                    )
            lessons = self._recent_lessons(2)
            if lessons:
                lines.append("今天复盘出来的注意点：" + "；".join(lessons))
            if style:
                lines.append("群里的说话习惯（自然贴合即可，别演）：" + style)
            if memes:
                lines.append("最近听来的热梗（偶尔用，别硬塞、不要说来源）：")
                for term, meaning, usage, example, _heat in memes:
                    seg = f"- {term}：{meaning}"
                    if usage:
                        seg += f"；用时:{usage}"
                    if example:
                        seg += f"；例:{example}"
                    lines.append(seg)
            lines.append("</vibe>")
            block = "\n".join(lines)
            if len(block) > INJECT_MAX_CHARS:
                block = block[:INJECT_MAX_CHARS] + "…</vibe>"
            parts = getattr(request, "extra_user_content_parts", None)
            if parts is not None:
                parts.append({"type": "text", "text": block})
            else:
                request.system_prompt = (request.system_prompt or "") + "\n\n" + block
        except Exception as e:
            logger.warning(f"[dsh_vibe] 注入失败: {e}")
