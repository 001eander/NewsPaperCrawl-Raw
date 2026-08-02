#!/usr/bin/env python3
"""
浙图报纸爬虫 - 命令行入口
=========================

用法:
  uv run cli.py import                       # 阶段0: 导入报纸清单
  uv run cli.py issues 2014 2026 --end-month 7   # 阶段1: 枚举全部期次(2014~2026-07)
  uv run cli.py boards --limit 50           # 阶段2: 抓版面/位置/版面图(分批)
  uv run cli.py articles --limit 100        # 阶段3: 抓报道正文+配图(分批)
  uv run cli.py login                       # 登录辅助: 自动填表,手动输验证码/点登录,保存 cookie

所有阶段可反复执行,自动跳过已完成(status=done)。
认证失效时自动通过 Bark 通知你重新登录。
"""

import argparse
import asyncio
import logging

from crawler.auth import AuthError
from crawler.crawler import run
from crawler.logging_config import setup_logging
from scripts.login import run_login


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

    p_login = sub.add_parser(
        "login", help="登录辅助: 自动填表,手动输验证码/点登录,保存 cookie"
    )
    p_login.add_argument(
        "--credentials",
        default=None,
        help="凭据文件路径(默认 .secrets/credentials.json)",
    )
    p_login.add_argument(
        "--cookies", default=None, help="Cookie 保存路径(默认 .secrets/cookies.json)"
    )

    args = p.parse_args()

    setup_logging()

    # login 是 Playwright 同步流程,不能混进异步 run(),单独分支执行
    if args.stage == "login":
        run_login(credentials_path=args.credentials, cookies_path=args.cookies)
        return

    kwargs = {k: v for k, v in vars(args).items() if k != "stage" and v is not None}
    try:
        asyncio.run(run(args.stage, **kwargs))
    except AuthError as e:
        # 认证失效:给出可操作提示并以非零码退出(便于 cron/systemd 感知失败)
        logging.getLogger(__name__).error(
            "认证失效: %s\n请重新执行 uv run cli.py login 更新登录状态后再试", e
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
