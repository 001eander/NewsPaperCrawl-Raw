#!/usr/bin/env python3
"""
articles 循环守护:抓报道 → 认证失效自动重新登录 → 再抓 …
========================================================
阶段3 的无人值守循环:

  1. 执行 `cli.py articles --concurrency N`(处理全部 pending)
  2. 退出码 0   → 本轮抓完,睡 --poll-seconds 秒再轮询(等新出现的 pending)
  3. 退出码非 0 → cli.py 仅在 AuthError 时非零退出 → 自动拉起
                  `scripts/login.py` 重新登录,登录成功后自动回到步骤1

登录仍需要你在弹出的浏览器里手动输验证码并点【登录】,这是唯一需要
人工介入的环节;其余时间脚本自动循环。

用法:
  uv run scripts/run_articles_loop.py
  uv run scripts/run_articles_loop.py --concurrency 16 --poll-seconds 600 --retry-delay 30
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def now() -> str:
    return time.strftime("%F %T")


def run(argv: list[str]) -> int:
    """复用当前 venv 解释器、在项目根目录运行命令(继承 stdio,login 交互可用)。"""
    print(f"[{now()}] $ {' '.join(argv)}", flush=True)
    return subprocess.run([sys.executable, *argv], cwd=ROOT, check=False).returncode


def main() -> None:
    p = argparse.ArgumentParser(description="articles 无人值守循环")
    p.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="HTTP 并发上限(默认不传,走 cli.py 默认)",
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=600,
        help="一轮成功后轮询间隔(秒,默认 600)",
    )
    p.add_argument(
        "--retry-delay",
        type=int,
        default=30,
        help="登录失败后的重试间隔(秒,默认 30)",
    )
    args = p.parse_args()

    articles = ["cli.py", "articles"]
    if args.concurrency:
        articles += ["--concurrency", str(args.concurrency)]

    try:
        while True:
            code = run(articles)
            if code == 0:
                print(
                    f"[{now()}] articles 跑完(无 pending 或全部完成),"
                    f"{args.poll_seconds}s 后再轮询",
                    flush=True,
                )
                time.sleep(args.poll_seconds)
                continue

            print(
                f"[{now()}] articles 退出码 {code}(通常为登录过期)→ 拉起登录...",
                flush=True,
            )
            login_code = run(["scripts/login.py"])
            if login_code == 0:
                continue  # 登录成功,回到 articles

            print(
                f"[{now()}] 登录失败(退出码 {login_code}),{args.retry_delay}s 后重试",
                flush=True,
            )
            time.sleep(args.retry_delay)
    except KeyboardInterrupt:
        print(f"[{now()}] 收到中断,退出循环", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
