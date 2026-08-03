#!/usr/bin/env python3
"""
爬取进度统计脚本
================
输出 issues / boards / articles 三张表的完成状况(pending/done/failed)。

用法:
  uv run scripts/status.py               # 三表概览(总数/完成/待处理/失败 + 进度条)
  uv run scripts/status.py --by-paper    # 按报纸汇总各阶段完成度
  uv run scripts/status.py --failed 5    # 概览 + 每表最近 5 条失败原因
  uv run scripts/status.py --failed      # 同 --failed 5(不带参数默认 5)

只读查询,不改动数据库。

表格输出全部用 ASCII(不含中文字符),避免全角/半角混排导致终端列错位;
中文报纸名是数据,单独放每行行尾作附加列,不参与列宽对齐。
"""

import argparse
import asyncio
import sys
from pathlib import Path

import aiosqlite

# 项目未安装为包(uv sync --no-install-project),直接运行脚本时
# sys.path[0] 是 scripts/ 而非项目根,需手动把项目根加进来
# (与 pytest 的 pythonpath=["."] 配置保持一致)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.config import DB_PATH  # 依赖上方 sys.path 调整

# 与 cli.py 子命令一一对应;f-string 拼接的 table 名只来自这个白名单
STAGES = ("issues", "boards", "articles")
STATUS_ORDER = ("pending", "done", "failed")


def progress_bar(done: int, total: int, width: int = 18) -> str:
    if total == 0:
        return "-" * width
    filled = round(done / total * width)
    return "#" * filled + "-" * (width - filled)


def check_db() -> None:
    if not DB_PATH.exists():
        sys.exit(
            f"DB not found: {DB_PATH}\n"
            "run the crawler first (`uv run cli.py ...`), or point "
            "NEWSPAPER_DB_PATH at an existing database"
        )


async def table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cur.fetchone() is not None


async def stage_counts(db: aiosqlite.Connection, table: str) -> dict[str, int]:
    cur = await db.execute(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")
    counts = {s: 0 for s in STATUS_ORDER}
    for row in await cur.fetchall():
        counts[row["status"]] = row["n"]
    return counts


async def print_overview(db: aiosqlite.Connection) -> None:
    print("crawl status")
    print("=" * 58)
    label_w, total_w, prog_w, done_w, pend_w, fail_w = 10, 8, 18, 8, 9, 7
    header = (
        f"{'':<{label_w}} {'total':>{total_w}}  "
        f"{'progress':<{prog_w}} "
        f"{'done':>{done_w}} {'pending':>{pend_w}} {'failed':>{fail_w}}"
    )
    print(header)
    for table in STAGES:
        if not await table_exists(db, table):
            print(f"{table:<{label_w}}  (table not created yet)")
            continue
        counts = await stage_counts(db, table)
        total = sum(counts.values())
        print(
            f"{table:<{label_w}} {total:>{total_w}}  "
            f"{progress_bar(counts['done'], total):<{prog_w}} "
            f"{counts['done']:>{done_w}} {counts['pending']:>{pend_w}} "
            f"{counts['failed']:>{fail_w}}"
        )


async def print_by_paper(db: aiosqlite.Connection) -> None:
    if not await table_exists(db, "papers"):
        print("papers table missing; run `uv run cli.py import` first")
        return
    cur = await db.execute("SELECT paperid, name FROM papers ORDER BY paperid")
    papers = list(await cur.fetchall())
    if not papers:
        print("papers table is empty; run `uv run cli.py import` first")
        return

    # 按 paperid 汇总三表的状态计数
    per_paper: dict[str, dict[str, dict[str, int]]] = {}
    for table in STAGES:
        if not await table_exists(db, table):
            continue
        cur = await db.execute(
            f"SELECT paperid, status, COUNT(*) AS n FROM {table} "
            "GROUP BY paperid, status"
        )
        for row in await cur.fetchall():
            bucket = per_paper.setdefault(row["paperid"], {})
            table_bucket = bucket.setdefault(table, {s: 0 for s in STATUS_ORDER})
            table_bucket[row["status"]] = row["n"]

    # 单元格只含 ASCII(paperid + done/total),列宽用 len() 即可;
    # 中文报纸名单独放行尾,不参与列对齐
    rows_data: list[tuple[str, list[str], str]] = []
    for p in papers:
        cells = []
        for table in STAGES:
            tb = per_paper.get(p["paperid"], {}).get(table)
            if tb is None:
                cells.append("--")
            else:
                cells.append(f"{tb['done']}/{sum(tb.values())}")
        rows_data.append((p["paperid"], cells, p["name"]))

    headers = [f"{t} done/total" for t in STAGES]
    paper_w = max([len("paper")] + [len(r[0]) for r in rows_data])
    col_ws = [
        max([len(h)] + [len(r[1][i]) for r in rows_data]) for i, h in enumerate(headers)
    ]
    header = (
        "paper".ljust(paper_w)
        + " "
        + " ".join(h.ljust(w) for h, w in zip(headers, col_ws))
        + "  paper_name"
    )
    print("\nper-paper progress")
    print("=" * len(header))
    print(header)
    for paperid, cells, name in rows_data:
        print(
            paperid.ljust(paper_w)
            + " "
            + " ".join(c.ljust(w) for c, w in zip(cells, col_ws))
            + f"  {name}"
        )


async def print_failed(db: aiosqlite.Connection, limit: int) -> None:
    print("\nfailed details")
    print("=" * 58)
    shown_any = False
    for table in STAGES:
        if not await table_exists(db, table):
            continue
        cur = await db.execute(
            f"SELECT paperid, date, error FROM {table} "
            "WHERE status='failed' ORDER BY date DESC, paperid LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        if not rows:
            continue
        shown_any = True
        print(f"\n[{table}] {len(rows)} failed, showing first {limit}:")
        for r in rows:
            err = (r["error"] or "").strip().replace("\n", " ")
            print(f"  {r['paperid']} {r['date']}  {err[:80]}")
    if not shown_any:
        print("no failed rows in any table")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="输出 issues/boards/articles 的爬取完成状况"
    )
    ap.add_argument(
        "--by-paper",
        action="store_true",
        help="按报纸逐行汇总各阶段完成度",
    )
    ap.add_argument(
        "--failed",
        nargs="?",
        const=5,
        type=int,
        metavar="N",
        help="列出每表最近 N 条失败原因(不带参数默认 5)",
    )
    args = ap.parse_args()

    check_db()
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    try:
        await print_overview(db)
        if args.by_paper:
            await print_by_paper(db)
        if args.failed is not None:
            await print_failed(db, args.failed)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
