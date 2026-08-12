#!/usr/bin/env python3
"""
重试失败项工具
==============
把 status='failed' 重置为 'pending',交由 cli.py 对应阶段重跑(状态机只认 pending)。

用法:
  uv run scripts/retry_failed.py --stats                          # 查看各表状态分布
  uv run scripts/retry_failed.py --reset boards                   # issues.failed -> pending,并兜底孤儿版面期次
  uv run scripts/retry_failed.py --reset articles                 # articles.failed -> pending(全部)
  uv run scripts/retry_failed.py --reset articles --only-network  # 仅重试网络/502 类错误
  uv run scripts/retry_failed.py --export articles [--out x.csv]  # 导出仍 failed 的报道清单

重试安全说明:
  - 全部阶段幂等(INSERT OR IGNORE + 状态机),重复执行不会产生脏数据
  - 重置前脚本自动备份 crawl.db -> data/crawl.db.retry-<时间戳>.bak(可用 --no-backup 跳过)
  - 重试一轮后仍 failed 的即为真失败(站点未收录/无正文),可导出清单人工确认
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.config import DB_PATH  # noqa: E402


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def backup_db() -> Path:
    bak = DB_PATH.with_name(f"crawl.db.retry-{datetime.now():%Y%m%d-%H%M%S}.bak")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(bak)
    try:
        src.backup(dst)  # SQLite 在线备份 API,对 WAL 库安全
    finally:
        src.close()
        dst.close()
    print(f"已备份数据库 -> {bak}")
    return bak


def cmd_stats(con):
    for t in ("issues", "boards", "articles"):
        rows = con.execute(
            f"SELECT status, COUNT(*) n FROM {t} GROUP BY status"
        ).fetchall()
        dist = {r["status"]: r["n"] for r in rows}
        print(f"{t:10s} {dist}")


def cmd_reset_boards(con):
    """boards 阶段重试:failed 期次 + 含孤儿 pending 版面的 done 期次 -> pending"""
    n1 = con.execute(
        "UPDATE issues SET status='pending', error=NULL WHERE status='failed'"
    ).rowcount
    # 兜底:boards 表有 pending 但期次已是 done 的孤儿(正常流程不会处理它们)
    n2 = con.execute(
        """
        UPDATE issues SET status='pending', error=NULL
        WHERE status='done' AND (paperid, date) IN (
            SELECT DISTINCT paperid, date FROM boards WHERE status='pending'
        )
        """
    ).rowcount
    con.commit()
    print(f"issues 重置 pending: {n1} 个 failed 期次 + {n2} 个含孤儿版面的 done 期次")


def cmd_reset_articles(con, only_network: bool):
    if only_network:
        n = con.execute(
            "UPDATE articles SET status='pending', error=NULL "
            "WHERE status='failed' AND error LIKE '请求重试耗尽%'"
        ).rowcount
    else:
        n = con.execute(
            "UPDATE articles SET status='pending', error=NULL WHERE status='failed'"
        ).rowcount
    con.commit()
    print(f"articles 重置 pending: {n} 条")


def cmd_export(con, table: str, out: str):
    if table == "articles":
        sql = (
            "SELECT metaid, paperid, date, title, error "
            "FROM articles WHERE status='failed' ORDER BY date"
        )
    elif table == "issues":
        sql = "SELECT paperid, date, error FROM issues WHERE status='failed' ORDER BY date"
    else:
        raise SystemExit(f"未知表: {table}(可用 issues|articles)")
    rows = con.execute(sql).fetchall()
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[r[0] for r in con.execute(sql).description])
        w.writeheader()
        w.writerows(dict(r) for r in rows)
    print(f"已导出 {len(rows)} 条 -> {out}")


def main():
    p = argparse.ArgumentParser(
        description="重试失败项:failed -> pending 后由 cli.py 对应阶段重跑"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--stats", action="store_true", help="查看各表状态分布")
    g.add_argument("--reset", choices=["boards", "articles"], help="重置某阶段失败项为 pending")
    g.add_argument("--export", metavar="TABLE", help="导出仍 failed 清单(issues|articles)")
    p.add_argument("--only-network", action="store_true", help="仅重试网络/502 类错误")
    p.add_argument("--out", default=None, help="--export 输出 CSV 路径")
    p.add_argument("--no-backup", action="store_true", help="重置前不备份数据库")
    args = p.parse_args()

    con = connect()
    try:
        if args.stats:
            cmd_stats(con)
        elif args.reset:
            if not args.no_backup:
                backup_db()
            if args.reset == "boards":
                cmd_reset_boards(con)
            else:
                cmd_reset_articles(con, args.only_network)
        elif args.export:
            out = args.out or str(
                DB_PATH.parent / f"failed_{args.export}_{datetime.now():%Y%m%d}.csv"
            )
            cmd_export(con, args.export, out)
    finally:
        con.close()


if __name__ == "__main__":
    main()
