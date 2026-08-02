"""
全局配置模块
============
所有"运行时路径"统一收口在这里,支持环境变量覆盖 + 合理默认值。

环境变量:
  NEWSPAPER_DATA_DIR     数据目录(DB/版面图/正文/配图/位置JSON)
  NEWSPAPER_DB_PATH      数据库文件路径(默认 <DATA_DIR>/crawl.db)
  NEWSPAPER_SECRETS_DIR  认证与通知配置目录(默认 .secrets)
  NEWSPAPER_PAPERLIST_CSV  报纸清单 CSV 路径(默认项目根目录 报纸清单.csv)

Docker / Dev Container 里通过 environment 注入;宿主机用 export 覆盖。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


DATA_DIR = _env("NEWSPAPER_DATA_DIR", BASE_DIR / "data")
SECRETS_DIR = _env("NEWSPAPER_SECRETS_DIR", BASE_DIR / ".secrets")
DB_PATH = _env("NEWSPAPER_DB_PATH", DATA_DIR / "crawl.db")
PAPERLIST_CSV = _env("NEWSPAPER_PAPERLIST_CSV", BASE_DIR / "archive" / "报纸清单.csv")

BASE_URL = "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"
