"""
认证与通知模块
==============
- cookies.json: 保存 apabi 站点的登录 cookie(私有,gitignore)
- bark.json:    Bark 推送配置(设备 key)
- 功能:
    * 加载/保存 cookie
    * 检测认证是否失效(响应特征)
    * 认证失效时通过 Bark 通知用户重新登录
"""

import json
import logging
from pathlib import Path

import aiohttp

from .config import SECRETS_DIR

logger = logging.getLogger(__name__)

COOKIES_FILE = SECRETS_DIR / "cookies.json"
BARK_FILE = SECRETS_DIR / "bark.json"

# 认证失效的响应特征:登录跳转 / 出现"登录"提示 / 会话过期页
AUTH_FAIL_MARKERS = [
    "登录",
    "session 已过期",
    "login",
    "sso/login",
    "用户登录",
]


class AuthError(Exception):
    """认证失效异常"""


class AuthManager:
    def __init__(self, cookies_file=COOKIES_FILE, bark_file=BARK_FILE):
        self.cookies_file = Path(cookies_file)
        self.bark_file = Path(bark_file)
        self.cookies: dict[str, str] = {}
        self._load()

    def _load(self):
        """从 cookies.json 加载 cookie,兼容两种格式:
        - 旧格式: {"cookies": {"name": "value"}}
        - 新格式: [{"name": "...", "value": "...", "domain": ".zjlib.cn"}, ...]  (Playwright 导出)
        """
        if not self.cookies_file.exists():
            raise AuthError(
                f"缺少认证文件 {self.cookies_file}\n"
                "请参考 .secrets/cookies.json.example 创建,填入浏览器里的登录 cookie"
            )
        try:
            data = json.loads(self.cookies_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AuthError(f"cookies.json 解析失败: {e}") from e

        self.cookies = {}
        self._cookie_by_domain: dict[str, dict[str, str]] = {}

        if isinstance(data, list):
            # 新格式: Playwright 导出的 cookie 列表
            for item in data:
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                self.cookies[name] = value
                domain = item.get("domain", "")
                if domain:
                    self._cookie_by_domain.setdefault(domain, {})[name] = value
        elif isinstance(data, dict):
            raw = data.get("cookies", data)  # 兼容 {"cookies": {...}} 或直接 {...}
            if isinstance(raw, dict):
                self.cookies = {k: str(v) for k, v in raw.items() if v is not None}
                # 旧格式无 domain 信息,全部归入空 domain
                self._cookie_by_domain.setdefault("", self.cookies)
            elif isinstance(raw, list):
                for item in raw:
                    name = item.get("name")
                    value = item.get("value")
                    if not name or value is None:
                        continue
                    self.cookies[name] = value
                    domain = item.get("domain", "")
                    if domain:
                        self._cookie_by_domain.setdefault(domain, {})[name] = value
        else:
            raise AuthError("cookies.json 格式无法识别")

        if not self.cookies:
            raise AuthError("cookies.json 为空,请填入登录 cookie")

    def save(self):
        """保存 cookie(认证更新后调用)"""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cookies": self.cookies}
        self.cookies_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("cookies 已保存到 %s", self.cookies_file)

    def cookie_header(self, url: str = "") -> str:
        """
        构造 Cookie 请求头。
        若提供了 url,则按域名匹配(apabi 域带 vpn358_sid,其余带 zjlib.cn 的)。
        否则返回全部 cookie(兼容无 domain 的旧格式)。
        """
        if not url:
            cookies = self.cookies
        else:
            host = url.split("//")[1].split("/")[0].lower() if "//" in url else url
            cookies = {}
            for domain, domain_cookies in self._cookie_by_domain.items():
                # 域匹配: 请求 host 以 domain 结尾 或 domain 为空(通用 cookie)
                if not domain or host.endswith(domain.lstrip(".")):
                    cookies.update(domain_cookies)
            # 若没有域匹配(比如 url 是 img.enews.apabi.com,不含 elib 域),退回全部
            if not cookies:
                cookies = self.cookies
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    # ---- 失效检测 ----

    def response_is_auth_fail(self, text: str) -> bool:
        """
        判断响应文本是否表示认证失效。
        核心规则: 若响应是 ELib 授权访问系统的登录页(重定向后),则 cookie 已过期。
        """
        text = text[:4000]  # 只检查前部
        low = text.lower()
        # ① ELib 登录页特征: 重定向到 index.php?r=site/login, 表单字段 FrontLoginForm
        if (
            "frontloginform" in low
            or "index.php?r=site%2flogin" in low
            or "index.php?r=site/login" in low
        ):
            return True
        # ② 微信扫码登录特征(ELib 也支持)
        if "微信扫一扫登录" in text:
            return True
        # ③ 通用: sso/login 跳转
        if "sso/login" in low or "login.aspx" in low:
            return True
        # ④ 内容为空或极小,且含"登录"
        return len(text) < 200 and any(m in text for m in ["登录", "login"])

    def check_and_raise(self, url: str, text: str):
        """若认证失效,抛 AuthError 并推送 Bark"""
        if self.response_is_auth_fail(text):
            self.notify("浙图爬虫:认证失效", f"访问 {url} 时检测到登录跳转,请重新登录")
            raise AuthError(f"认证失效于 {url}")

    # ---- Bark 通知 ----

    def notify(self, title: str, body: str = ""):
        """通过 Bark 推送通知到手机"""
        if not self.bark_file.exists():
            logger.warning("缺少 bark.json,跳过通知: %s - %s", title, body)
            return
        try:
            data = json.loads(self.bark_file.read_text(encoding="utf-8"))
            key = data.get("device_key", "")
            server = data.get("bark_server", "https://api.day.app")
        except (json.JSONDecodeError, KeyError):
            logger.warning("bark.json 无效,跳过通知")
            return
        if not key or key.startswith("在此填入"):
            logger.warning("bark.json 未配置有效 device_key,跳过通知")
            return

        import asyncio

        async def _push():
            url = f"{server}/{key}/{title}"
            if body:
                url += f"/{body}"
            async with aiohttp.ClientSession() as sess:
                try:
                    async with sess.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        logger.info("Bark 推送: %s (HTTP %s)", title, r.status)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Bark 推送失败: %s", e)

        try:
            asyncio.create_task(_push())
        except RuntimeError:
            # 无运行中的事件循环时(同步上下文)直接运行
            asyncio.run(_push())


# 单例
_auth: AuthManager | None = None


def get_auth() -> AuthManager:
    global _auth
    if _auth is None:
        _auth = AuthManager()
    return _auth
