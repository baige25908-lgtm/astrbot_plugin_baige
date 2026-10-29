# -*- coding: utf-8 -*-
"""白鸽·账本（dsh_meter）

只做一件事：把每次 LLM 响应的 usage 记下来，按天汇总成报表。
不额外调用任何 API，不往上下文里塞东西。
"""
import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import settings
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

TZ = timezone(timedelta(hours=8))
REPORT = settings.report("token-report.md")
EXTRA = settings.report("meter-extra.jsonl")


def _local_day(ts=None):
    return datetime.fromtimestamp(ts or time.time(), TZ).strftime("%F")


class Component:
    def __init__(self, context: Context):
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_baige")) / "meter"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.data_dir / "meter.db"
        self._init_db()
        self._task = None

    def _conn(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.execute(
            "CREATE TABLE IF NOT EXISTS calls("
            "ts INTEGER, day TEXT, in_other INTEGER, in_cached INTEGER, out INTEGER)"
        )
        return c

    def _init_db(self):
        try:
            with self._conn() as c:
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_meter] 建库失败: {e}")

    def _record(self, u):
        try:
            now = int(time.time())
            with self._conn() as c:
                c.execute(
                    "INSERT INTO calls(ts,day,in_other,in_cached,out) VALUES(?,?,?,?,?)",
                    (
                        now,
                        _local_day(now),
                        int(getattr(u, "input_other", 0) or 0),
                        int(getattr(u, "input_cached", 0) or 0),
                        int(getattr(u, "output", 0) or 0),
                    ),
                )
                c.commit()
        except Exception as e:
            logger.warning(f"[dsh_meter] 记账失败: {e}")

    def _ingest_extra(self):
        """把 vibe 等插件写的行读进来（按行号增量）。"""
        if not EXTRA.is_file():
            return
        try:
            meta = self.data_dir / "extra_offset.txt"
            done = int(meta.read_text().strip() or 0) if meta.is_file() else 0
            lines = EXTRA.read_text(encoding="utf-8", errors="ignore").splitlines()
            new = lines[done:]
            if not new:
                return
            with self._conn() as c:
                for ln in new:
                    parts = ln.split(",")
                    if len(parts) < 4:
                        continue
                    try:
                        ts, io, ic, out = (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
                    except ValueError:
                        continue
                    c.execute(
                        "INSERT INTO calls(ts,day,in_other,in_cached,out) VALUES(?,?,?,?,?)",
                        (ts, _local_day(ts), io, ic, out),
                    )
                c.commit()
            meta.write_text(str(len(lines)), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[dsh_meter] 读扩展用量失败: {e}")
    async def on_resp(self, event: AstrMessageEvent, resp):
        u = getattr(resp, "usage", None)
        if u is not None:
            await asyncio.to_thread(self._record, u)

    # ---------- 报表 ----------
    def _report(self):
        self._ingest_extra()
        today = _local_day()
        lines = [f"# LLM 用量账本 {today}", ""]
        try:
            with self._conn() as c:
                def agg(day):
                    r = c.execute(
                        "SELECT count(*), SUM(in_other), SUM(in_cached), SUM(out), "
                        "SUM(in_other)+SUM(in_cached)+SUM(out) FROM calls WHERE day=?",
                        (day,),
                    ).fetchone()
                    return [x or 0 for x in r]

                n, io, ic, out, tot = agg(today)
                lines.append(f"## 今天（{n} 次调用）")
                if n:
                    cache_rate = (ic / (io + ic) * 100) if (io + ic) else 0
                    lines += [
                        f"- 输入（未缓存）：{io:,}",
                        f"- 输入（命中缓存）：{ic:,} —— 缓存率 {cache_rate:.1f}%",
                        f"- 输出：{out:,}",
                        f"- 合计：{tot:,}",
                    ]
                else:
                    lines.append("- 今天还没有调用记录")

                lines += ["", "## 近 7 天"]
                for i in range(7):
                    d = (datetime.now(TZ) - timedelta(days=i)).strftime("%F")
                    n2, io2, ic2, out2, tot2 = agg(d)
                    if n2:
                        lines.append(
                            f"- {d}：{n2} 次，合计 {tot2:,}（缓存 {ic2:,} / 输出 {out2:,}）"
                        )
                hrows = c.execute(
                    "SELECT strftime('%H', datetime(ts,'unixepoch','+8 hours')) h, count(*), "
                    "SUM(in_other)+SUM(in_cached)+SUM(out) FROM calls WHERE day=? "
                    "GROUP BY h ORDER BY h",
                    (today,),
                ).fetchall()
                if hrows:
                    lines += ["", "## 今天按小时"]
                    lines += [f"- {h} 点：{n3} 次，{t3:,} token" for h, n3, t3 in hrows]
        except Exception as e:
            lines.append(f"（统计失败：{e}）")
        return "\n".join(lines)

    async def _loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                await asyncio.to_thread(
                    lambda: REPORT.write_text(self._report(), encoding="utf-8")
                )
            except Exception as e:
                logger.warning(f"[dsh_meter] 写报表失败: {e}")
            await asyncio.sleep(600)

    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        logger.info("[dsh_meter] 已启动，每 10 分钟刷新 /stage/token-report.md")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[dsh_meter] 已停止")
