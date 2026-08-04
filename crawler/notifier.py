"""
Bark 通知模块
=============
爬虫收尾统一通知:正常爬完 / 认证失效 / 未知错误 都会推送 Bark。

推送是异步 I/O,但必须在 run() 返回前完整 await——CLI 是"一次 asyncio.run 即退出"
的程序,若把推送挂成后台任务,asyncio.run 关闭事件循环时会强制取消未完成任务,
推送请求根本不会发出。因此 notify/notify_job_result 均为 async,调用方必须 await。

配置: .secrets/bark.json
  {
    "device_key": "...",           // Bark 设备 key
    "bark_server": "https://api.day.app"
  }
"""

import json
import logging
from pathlib import Path

import aiohttp

from .config import SECRETS_DIR

logger = logging.getLogger(__name__)

BARK_FILE = SECRETS_DIR / "bark.json"


class BarkNotifier:
    """推送 Bark 通知到手机(带缓存,重复构造只读一次配置)"""

    def __init__(self, bark_file: Path = BARK_FILE):
        self.bark_file = Path(bark_file)
        self._key = ""
        self._server = "https://api.day.app"
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """惰性读取配置;失败或未配置时保持空 key(后续推送直接跳过)"""
        if self._loaded:
            return
        self._loaded = True
        if not self.bark_file.exists():
            logger.warning("缺少 %s,跳过 Bark 通知", self.bark_file)
            return
        try:
            data = json.loads(self.bark_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("bark.json 读取失败,跳过 Bark 通知: %s", e)
            return
        key = data.get("device_key", "")
        if not key or key.startswith("在此填入"):
            logger.warning("bark.json 未配置有效 device_key,跳过 Bark 通知")
            return
        self._key = key
        self._server = data.get("bark_server", "https://api.day.app")

    @property
    def is_configured(self) -> bool:
        """Bark 是否已配置(有效 device_key)"""
        self._ensure_loaded()
        return bool(self._key)

    async def notify(self, title: str, body: str = "") -> None:
        """推送一条通知;配置缺失/推送失败都只记日志,不抛异常"""
        self._ensure_loaded()
        if not self._key:
            return
        url = f"{self._server}/{self._key}/{title}"
        if body:
            url += f"/{body}"
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logger.info("Bark 推送: %s (HTTP %s)", title, r.status)
            except Exception as e:  # noqa: BLE001
                logger.warning("Bark 推送失败: %s", e)


async def notify_job_result(
    *,
    outcome: str,
    stage: str,
    processed: int | None = None,
    message: str = "",
) -> None:
    """
    爬虫一次运行收尾的统一个通知。

    outcome:
      "done"      正常完成(全部处理完毕或本轮处理完)
      "auth"      认证失效,需要重新登录
      "error"     未知错误导致中断
    """
    if outcome == "auth":
        title = "浙图爬虫:需要重新登录"
        body = "登录状态失效,请重新执行登录后继续爬取"
    elif outcome == "error":
        title = "浙图爬虫:异常中断"
        body = f"阶段 {stage} 运行出错: {message or '未知错误'}"
    else:  # done
        title = "浙图爬虫:爬取完成"
        body = f"阶段 {stage} 完成,处理 {processed or 0} 项"
    notifier = BarkNotifier()
    await notifier.notify(title, body)
    # 若认证失效且 Bark 未配置,退化为提示日志(console 也能看到)
    if outcome == "auth" and not notifier.is_configured:
        logger.warning("认证失效但未配置 bark.json,无法推送通知;请配置后重试")
