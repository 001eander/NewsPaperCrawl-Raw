#!/usr/bin/env python3
"""
测速工具:采样统计某表 status 的增量速率,估算剩余时间。

用法:
  uv run scripts/speed_test.py                     # 采样 60 秒测 articles done 速率
  uv run scripts/speed_test.py --seconds 120       # 自定义采样时长
  uv run scripts/speed_test.py --table boards      # 测其他表
  uv run scripts/speed_test.py --status failed     # 统计 failed 增量(观察失败率)

依赖爬虫进程正在运行(读取 DB 中 status 计数)。只读,不触网,可与爬虫并发执行。
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.config import DB_PATH


def main():
    p = argparse.ArgumentParser(description="采样统计爬取速率与剩余时间估算")
    p.add_argument("--table", choices=["articles", "boards", "issues"], default="articles")
    p.add_argument("--status", default="done", help="统计的状态(默认 done)")
    p.add_argument("--seconds", type=int, default=60, help="采样时长,秒(默认 60)")
    args = p.parse_args()

    con = sqlite3.connect(DB_PATH)
    count_sql = f"SELECT COUNT(*) FROM {args.table} WHERE status=?"
    n1 = con.execute(count_sql, (args.status,)).fetchone()[0]
    time.sleep(args.seconds)
    n2 = con.execute(count_sql, (args.status,)).fetchone()[0]
    pending = con.execute(
        f"SELECT COUNT(*) FROM {args.table} WHERE status='pending'"
    ).fetchone()[0]
    con.close()

    rate = (n2 - n1) / args.seconds
    print(f"{args.table}.{args.status}: {n1} -> {n2}  (+{n2 - n1}/{args.seconds}s = {rate:.1f}/s)")
    print(f"pending 剩余: {pending}")
    if rate > 0:
        eta_h = pending / rate / 3600
        print(f"预计剩余: {eta_h:.1f} 小时 ({pending / rate / 60:.0f} 分钟)")
    else:
        print("速率 0:无处理活动(检查爬虫进程是否在跑 / 认证是否失效)")


if __name__ == "__main__":
    main()
