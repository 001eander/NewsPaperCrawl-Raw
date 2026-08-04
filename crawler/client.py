"""
HTTP 客户端模块
===============
- aiohttp 异步会话
- 并发信号量 + 随机延迟(礼貌限速)
- 自动注入 cookie,检测认证失效
- 证书校验关闭(对应浏览器的 --ignore-https-errors)
"""

import asyncio
import logging
import random
import ssl

import aiohttp

from .auth import AuthError, AuthManager, get_auth
from .config import CONCURRENCY

logger = logging.getLogger(__name__)

BASE = "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"


class HttpClient:
    def __init__(
        self,
        auth: AuthManager | None = None,
        concurrency: int = CONCURRENCY,
        min_delay: float = 0.3,
        max_delay: float = 1.0,
        retries: int = 3,
    ):
        self.auth = auth or get_auth()
        self.concurrency = concurrency
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.retries = retries
        self._sem = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None
        # 关闭证书校验(图书馆代理证书过期)
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(
                ssl=self._ssl, limit=self.concurrency, limit_per_host=self.concurrency
            ),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _throttle(self):
        """礼貌限速:随机延迟"""
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def get(self, url: str, **kwargs) -> str:
        """GET 并返回文本,带认证检测 + 重试"""
        headers = kwargs.pop("headers", {})
        headers["Cookie"] = self.auth.cookie_header(url)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        )
        # 重要:图片等静态资源不需要 Cookie(避免无谓暴露),但 html 需要
        last_err: Exception | None = None
        for attempt in range(self.retries):
            await self._throttle()
            async with self._sem:
                try:
                    async with self._session.get(
                        url, headers=headers, **kwargs
                    ) as resp:
                        if resp.status == 404:
                            return ""
                        # 认证检测①: 被重定向到登录域(cookie 过期 → 302 -> login.xxx)
                        if _redirected_to_login(resp):
                            raise AuthError(f"认证失效于 {url}(被重定向到登录页)")
                        text = await resp.text()
                        # 认证检测②: 响应内容呈现登录页(兜底)
                        await self.auth.check_and_raise(url, text)
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} for {url}")
                        return text
                except AuthError:
                    raise  # 认证失效,直接向上抛
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.debug("第 %s 次请求失败 %s: %s", attempt + 1, url, e)
                    await asyncio.sleep(2**attempt)
        raise RuntimeError(f"请求重试耗尽: {url}: {last_err}")

    async def download(self, url: str, save_path) -> bool:
        """下载二进制文件(图片等),返回是否成功"""
        save_path.parent.mkdir(parents=True, exist_ok=True)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            )
        }
        for attempt in range(self.retries):
            await self._throttle()
            async with self._sem:
                try:
                    async with self._session.get(url, headers=headers) as resp:
                        if resp.status == 404:
                            return False
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.read()
                        save_path.write_bytes(data)
                        return True
                except Exception as e:  # noqa: BLE001
                    logger.debug("下载失败 %s 第%s次: %s", url, attempt + 1, e)
                    await asyncio.sleep(2**attempt)
        return False


# 登录域特征:被重定向到这些入口即判定为认证失效
_LOGIN_HOST_MARKERS = (
    "login.",
    "sso.",
    ".sso.",
)
_LOGIN_PATH_MARKERS = (
    "/sso/login",
    "/index.php?",
    "/login.aspx",
    "/login",
)


def _redirected_to_login(resp) -> bool:
    """
    判定响应是否被重定向到了登录入口。

    cookie 过期时,服务端会 302 跳到登录域:
      history: 302 apabi--com.xxx/zjlib/?pid=... -> login.elib.zyproxy.zjlib.cn/index.php?pre=...
    aiohttp 默认跟随重定向,但 history 保留了全部 3xx;最终 URL 落在登录域即为失效。
    """
    if resp.history:
        for h in resp.history:
            loc = (h.headers.get("Location") or "").lower()
            if "login" in loc or "sso" in loc:
                return True
    final = str(resp.url).lower()
    if final.startswith("http"):
        host = final.split("//")[1].split("/")[0]
        if any(m in host for m in _LOGIN_HOST_MARKERS):
            return True
        path = "/" + final.split("//")[1].split("/", 1)[-1]
        if any(m in path for m in _LOGIN_PATH_MARKERS):
            return True
    return False
