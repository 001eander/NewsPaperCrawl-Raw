#!/usr/bin/env python3
"""
浙图报纸爬虫 - 命令行入口
=========================

用法:
  uv run cli.py import                       # 阶段0: 导入报纸清单
  uv run cli.py issues 2014 2026 --end-month 7   # 阶段1: 枚举全部期次(2014~2026-07)
  uv run cli.py boards --limit 50           # 阶段2: 抓版面/位置/版面图(分批)
  uv run cli.py articles --limit 100        # 阶段3: 抓报道正文+配图(分批)

所有阶段可反复执行,自动跳过已完成(status=done)。
认证失效时自动通过 Bark 通知你重新登录。
"""

import argparse
import asyncio
import logging

from crawler.crawler import run


def main():
    p = argparse.ArgumentParser(description="浙图报纸异步爬虫")
    sub = p.add_subparsers(dest="stage", required=True)

    p_import = sub.add_parser("import", help="导入报纸清单 CSV 到数据库")
    p_import.add_argument("--csv", default=None, help="报纸清单 CSV 路径")

    p_issues = sub.add_parser("issues", help="枚举期次")
    p_issues.add_argument("start_year", type=int)
    p_issues.add_argument("end_year", type=int)
    p_issues.add_argument("--end-month", type=int, default=None)

    p_boards = sub.add_parser("boards", help="抓版面")
    p_boards.add_argument("--paperid", default=None)
    p_boards.add_argument("--limit", type=int, default=200)

    p_articles = sub.add_parser("articles", help="抓报道")
    p_articles.add_argument("--limit", type=int, default=500)

    args = p.parse_args()
    kwargs = {k: v for k, v in vars(args).items() if k != "stage" and v is not None}

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(args.stage, **kwargs))


if __name__ == "__main__":
    main()
