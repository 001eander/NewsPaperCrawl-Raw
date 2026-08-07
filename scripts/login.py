#!/usr/bin/env python3
"""
浙图 SSO 登录辅助脚本
=====================
用 Playwright 打开真实浏览器窗口,复刻手动登录链路:
  1. 打开浙江图书馆 share 资源列表页
  2. 点击 "方正数字报" -> JS 自动弹出新标签页(经 vpn358 中转)
     - 未登录:新标签页落到 SSO 登录页,自动填账号/选机构,等你在浏览器里输
       验证码并手动点登录,登录后站点自动回跳到方正数字报落地页
     - 已登录:新标签页直接落到 apabi
  3. 检测到方正数字报落地页后,等 cookie 连续 15s 无变化(登录稳定)再保存
     全部 Cookies 到 .secrets/cookies.json

关键点:全程不强制 goto,由站点 JS 自己带过去,这样才能拿到
vpn358_sid 会话 cookie(crawler/auth.py 认证依赖)。

用法:
  uv run scripts/login.py
  uv run scripts/login.py --credentials .secrets/credentials.json --cookies .secrets/cookies.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---- 常量 ----
# share 资源列表页(appId/typeId 指向 "方正数字报" 所在的资源列表)
SHARE_URL = (
    "https://share.zjlib.cn/engine2/general/more"
    "?appId=1292055&typeId=4312537&currentBranch=0"
    "&wfwfid=2120&pageId=35594&websiteId=28609&mhType=1"
    "&publicId=5a4b97f974953111ed0a17754b327ca0790d"
    "&mhEnc=7918f9663ed2443f881ad8f98aed56dd"
)
# 资源列表里 "方正数字报" 的标题元素(class=name)
RESOURCE_NAME = "方正数字报"
# SSO 登录页 URL 特征
LOGIN_URL_MARKER = "sso/login"
# apabi(方正数字报)代理域 URL 特征
APABI_URL_MARKERS = ("apabi--com.elib.zyproxy.zjlib.cn",)
# 登录后要确认的关键会话 cookie
VPN_COOKIE = "vpn358_sid"
# SSO 跳转后 cookie 需连续多少秒无变化,才认为登录真正完成、cookie 稳定
COOKIE_STABLE_SECONDS = 15

DEFAULT_CREDENTIALS = (
    Path(__file__).resolve().parent.parent / ".secrets" / "credentials.json"
)
DEFAULT_COOKIES = Path(__file__).resolve().parent.parent / ".secrets" / "cookies.json"


def load_credentials(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到凭据文件: {path}\n请先创建(可参考 {path}.example)")
    data = json.loads(path.read_text(encoding="utf-8"))
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password or "在此填入" in username + password:
        sys.exit(f"凭据文件 {path} 未填写账号或密码")
    return {"username": username, "password": password}


def select_province_and_library(page) -> None:
    """自动选择 省本级 -> 浙江图书馆(两级下拉框)"""
    jq_input = page.locator('input[name="jqtype"]')
    jq_input.wait_for(state="visible", timeout=15_000)
    jq_input.click()
    province = page.locator(".select-list .one-title", has_text="省本级")
    province.wait_for(state="visible", timeout=10_000)
    province.click()
    lib = page.locator('li.option[data-index="2120"]', has_text="浙江图书馆")
    lib.wait_for(state="visible", timeout=10_000)
    lib.click()
    page.wait_for_function(
        """(sel) => document.querySelector(sel)?.value?.includes('浙江图书馆')""",
        arg='input[name="jqtype"]',
        timeout=10_000,
    )


def is_fangzheng_url(url: str) -> bool:
    """方正数字报落地页 URL 特征:apabi 代理域,或 share 域上的 area 落地页"""
    if any(m in url for m in APABI_URL_MARKERS):
        return True
    # SSO 登录成功后,落地页可能回跳到 share.zjlib.cn 的 area 页面
    return "share.zjlib.cn/entry/area/" in url


def is_fangzheng_page(page) -> bool:
    """页面是否为"方正数字报":命中 URL 特征,或页面标题含"方正数字报"(URL 变动时兜底)"""
    if is_fangzheng_url(page.url or ""):
        return True
    try:
        return "方正数字报" in (page.title() or "")
    except Exception:  # noqa: BLE001  # 导航中标题不可读时按"不是"处理
        return False


def _cookies_snapshot(ctx) -> frozenset:
    """当前 context 全部 cookie 的快照,用于检测 cookie 是否仍在变化"""
    return frozenset((c["name"], c["domain"], c["value"]) for c in ctx.cookies())


def _page_label(page) -> str:
    try:
        return f"{page.url} | {page.title()}"
    except Exception:  # noqa: BLE001  # 导航中不可读时回退为 "(标题不可读)"
        return f"{page.url} | (标题不可读)"


def is_login(url: str) -> bool:
    return LOGIN_URL_MARKER in url


def run_login(
    credentials_path: Path | str | None = None,
    cookies_path: Path | str | None = None,
) -> None:
    """
    执行 SSO 登录并保存全部 cookie 到 cookies_path。

    供 cli.py 的 login 子命令复用;也可直接运行本脚本调用。
    """
    cred = load_credentials(
        Path(credentials_path) if credentials_path else DEFAULT_CREDENTIALS
    )
    cookies_path = Path(cookies_path) if cookies_path else DEFAULT_COOKIES

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chromium")
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,  # 站点 TLS 证书过期,需忽略才能正常加载
        )
        page = ctx.new_page()
        page.goto(SHARE_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"[1/6] 已打开浙江图书馆资源列表: {page.title()}")

        # 步骤2: 点击 "方正数字报",监听新标签页(JS 自动弹出)
        print("[2/6] 点击 方正数字报...")
        with ctx.expect_page(timeout=30_000) as page_info:
            page.locator("div.name", has_text=RESOURCE_NAME).first.click()
        np = page_info.value

        # 等待新标签页稳定:要么落到 SSO 登录页,要么已登录直接落到 apabi
        np.wait_for_url(
            lambda u: is_login(u) or is_fangzheng_url(u),
            timeout=45_000,
        )

        # ---- 分情况:未登录走完整登录流程,已登录直接取 cookie ----
        if is_login(np.url):
            print("[2/6] 需要登录,SSO 登录页已弹出")
            # 步骤3: 填账号密码
            np.locator(".ipt-tel").fill(cred["username"])
            np.locator(".ipt-pwd").fill(cred["password"])
            print(f"[3/6] 已填入账号 {cred['username']}")

            # 步骤4: 选机构
            select_province_and_library(np)
            print("[4/6] 已选择 省本级 -> 浙江图书馆")

            # 步骤5: 等用户输入验证码
            code_input = np.locator(".ipt-code")
            code_input.wait_for(state="visible", timeout=15_000)
            print("[5/6] ⏸ 请在弹出的浏览器窗口里输入验证码...")
            np.wait_for_function(
                """(sel) => (document.querySelector(sel)?.value || '').trim().length > 0""",
                arg=".ipt-code",
                timeout=120_000,
            )

            # 步骤6: 登录交还用户手动点击。检测到验证码输入后,等用户在浏览器里
            # 点【登录】,SSO 自己回跳到 apabi(可能同标签,也可能新开标签)。
            print(
                "[5/6] 验证码已输入,请在弹出的浏览器窗口里点击【登录】按钮,登录完成后脚本会自动继续..."
            )
            apabi_page = None
            deadline = time.time() + 180
            last_urls: list[str] = []
            while time.time() < deadline:
                for pg in ctx.pages:
                    if is_fangzheng_page(pg):
                        apabi_page = pg
                        break
                if apabi_page is not None:
                    break
                urls = sorted(_page_label(pg) for pg in ctx.pages)
                if urls != last_urls:
                    print("  标签页:", " ; ".join(urls) if urls else "(无)")
                    last_urls = urls
                time.sleep(1)
            if apabi_page is None:
                print("提示: 登录后未自动进入方正数字报,当前标签页:")
                for pg in ctx.pages:
                    print("   -", _page_label(pg))
                sys.exit("登录后未进入方正数字报,请检查验证码/账号是否正确")
            apabi_page.bring_to_front()
            print(f"[6/6] 登录成功,已进入方正数字报: {apabi_page.url}")
        else:
            apabi_page = np
            apabi_page.bring_to_front()
            print("[2/6] 已登录,新标签页直接进入方正数字报")
            print("[6/6] 已进入方正数字报")

        # cookie 稳定检测:跳转后 vpn358_sid 等会话 cookie 可能还会刷新几次,
        # 连续 COOKIE_STABLE_SECONDS 秒无变化才认为登录真正完成。以 cookie 自身
        # 稳定性为准,比比对落地页 URL 更可靠(参数顺序/编码有差异也不会卡住)。
        last_snapshot = _cookies_snapshot(ctx)
        stable_since = time.time()
        stable_deadline = time.time() + 120  # 总超时,防止登录未完成时无限等待
        while time.time() < stable_deadline:
            time.sleep(2)
            snapshot = _cookies_snapshot(ctx)
            if snapshot == last_snapshot:
                if time.time() - stable_since >= COOKIE_STABLE_SECONDS:
                    print(f"cookie 已稳定(连续 {COOKIE_STABLE_SECONDS}s 无变化)")
                    break
            else:
                last_snapshot = snapshot
                stable_since = time.time()
        else:
            print("⚠ 等待 cookie 稳定超时(可能未完整落定,仍保存,靠 VPN_COOKIE 检查兜底)")

        # 已进入方正数字报落地页,保存全部 cookie(所有标签页共享 context)
        time.sleep(1)
        cookies = ctx.cookies()
        payload = [
            {"name": c["name"], "value": c["value"], "domain": c["domain"]}
            for c in cookies
        ]
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        cookies_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        names = {c["name"] for c in payload}
        if VPN_COOKIE in names:
            print(f"已保存 {len(payload)} 个 cookie 到 {cookies_path}(含 {VPN_COOKIE})")
        else:
            print(
                f"已保存 {len(payload)} 个 cookie 到 {cookies_path}(提示: 未含 {VPN_COOKIE},爬虫认证可能失效,建议重新登录)"
            )

        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="浙图 SSO 登录辅助")
    ap.add_argument(
        "--credentials",
        default=str(DEFAULT_CREDENTIALS),
        help=f"凭据文件(默认 {DEFAULT_CREDENTIALS})",
    )
    ap.add_argument(
        "--cookies",
        default=str(DEFAULT_COOKIES),
        help=f"Cookie 保存路径(默认 {DEFAULT_COOKIES})",
    )
    args = ap.parse_args()
    run_login(credentials_path=args.credentials, cookies_path=args.cookies)
