"""
数据库模块(SQLite)
==================
- 存报纸/期次/版面/报道 的元数据与爬取状态
- status 字段作为状态机: pending / done / failed
- 使用 aiosqlite 避免阻塞事件循环

表结构:
  papers         报纸清单(paperid PK)
  issues         期次(报纸×日期),UNIQUE(paperid,date)
  boards         版面(metaid PK)
  articles       报道(metaid PK,含版面位置坐标)
  article_images 报道配图(id PK)
"""

import logging
from pathlib import Path

import aiosqlite

from .config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paperid    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    issue_no   TEXT,
    period     TEXT,
    place      TEXT,
    start_date TEXT,
    end_date   TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    paperid     TEXT NOT NULL,
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    status      TEXT DEFAULT 'pending', -- pending/done/failed
    board_count INTEGER DEFAULT 0,
    error       TEXT,
    PRIMARY KEY (paperid, date)
);

CREATE TABLE IF NOT EXISTS boards (
    metaid      TEXT PRIMARY KEY,       -- nb.*
    paperid     TEXT NOT NULL,
    date        TEXT NOT NULL,
    board_no    TEXT,                   -- A01 / A1 ...
    board_name  TEXT,
    image_url   TEXT,
    img_w       INTEGER,
    img_h       INTEGER,
    image_local TEXT,                   -- 相对 data/boards/ 的路径
    status      TEXT DEFAULT 'pending',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    metaid       TEXT PRIMARY KEY,      -- nw.*
    paperid      TEXT NOT NULL,
    date         TEXT NOT NULL,
    board_metaid TEXT,
    title        TEXT,
    content_path TEXT,              -- 正文 JSON 相对 data/ 的路径(旧列 content 迁移而来)
    source_meta  TEXT,                  -- "报纸/日期/版面/栏目" 元信息
    bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL,  -- 版面位置(像素,版面图坐标)
    poly_points TEXT,                   -- 原始多边形坐标 JSON
    image_count  INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending',
    error        TEXT
);

CREATE TABLE IF NOT EXISTS article_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_metaid TEXT NOT NULL,
    url           TEXT NOT NULL,
    local_path    TEXT,
    w             INTEGER,
    h             INTEGER,
    status        TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_boards_issue ON boards(paperid, date);
CREATE INDEX IF NOT EXISTS idx_articles_issue ON articles(paperid, date);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
"""


async def init_db(path: Path = DB_PATH) -> aiosqlite.Connection:
    """初始化数据库并返回连接(启用 WAL + busy_timeout,支持多进程并发)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path), timeout=30)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await _migrate_articles_schema(db)  # 旧库: articles.content -> content_path
    # WAL 模式: 读写不互斥,多进程友好
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=30000")  # 等锁最多 30s
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.commit()
    logger.info("数据库已初始化: %s", path)
    return db


async def _migrate_articles_schema(db: aiosqlite.Connection) -> None:
    """
    既有库迁移: articles.content -> content_path(幂等)。

    正文已改为文件系统 JSON,content 列的旧文本弃置(开发环境 breaking change)。
    仅当旧列 content 存在且新列 content_path 不存在时执行一次 RENAME,
    随后立即把 content_path 置 NULL,避免"路径列里存全文"的脏状态。
    新库由 SCHEMA 直接建 content_path,此处自动跳过。
    """
    cur = await db.execute("PRAGMA table_info(articles)")
    cols = [r["name"] for r in await cur.fetchall()]
    if "content" in cols and "content_path" not in cols:
        await db.execute("ALTER TABLE articles RENAME COLUMN content TO content_path")
        await db.execute("UPDATE articles SET content_path = NULL")
        await db.commit()
        logger.info("articles.content 已迁移为 articles.content_path")


# ---- 报纸清单 ----


async def import_papers(db: aiosqlite.Connection, rows: list[dict]) -> int:
    """批量导入报纸清单(幂等,ON CONFLICT 忽略)"""
    await db.executemany(
        """INSERT OR IGNORE INTO papers
           (paperid, name, issue_no, period, place, start_date, end_date)
           VALUES (:paperid, :name, :issue_no, :period, :place, :start_date, :end_date)""",
        rows,
    )
    await db.commit()
    return len(rows)


async def get_papers(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cur = await db.execute("SELECT * FROM papers ORDER BY paperid")
    return list(await cur.fetchall())


# ---- 期次 ----


async def add_issues(db: aiosqlite.Connection, paperid: str, dates: list[str]) -> int:
    """插入某报纸的一批日期为 pending 期次(幂等)"""
    await db.executemany(
        """INSERT OR IGNORE INTO issues (paperid, date) VALUES (?, ?)""",
        [(paperid, d) for d in dates],
    )
    await db.commit()
    return len(dates)


async def pending_issues(
    db: aiosqlite.Connection, paperid: str | None = None, limit: int = 500
) -> list[aiosqlite.Row]:
    """取待爬取的期次(可限定报纸)"""
    if paperid:
        cur = await db.execute(
            "SELECT * FROM issues WHERE paperid=? AND status='pending' LIMIT ?",
            (paperid, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM issues WHERE status='pending' LIMIT ?", (limit,)
        )
    return list(await cur.fetchall())


# ---- 版面 ----


async def add_boards(db: aiosqlite.Connection, boards: list[dict]) -> None:
    await db.executemany(
        """INSERT OR IGNORE INTO boards
           (metaid, paperid, date, board_no, board_name, status)
           VALUES (:metaid, :paperid, :date, :board_no, :board_name, 'pending')""",
        boards,
    )
    await db.commit()


async def get_boards_for_issue(
    db: aiosqlite.Connection, paperid: str, date: str
) -> list[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT * FROM boards WHERE paperid=? AND date=? ORDER BY board_no",
        (paperid, date),
    )
    return list(await cur.fetchall())


# ---- 报道 ----


async def add_articles(db: aiosqlite.Connection, articles: list[dict]) -> None:
    """批量插入报道(metaid 主键,幂等)"""
    await db.executemany(
        """INSERT OR IGNORE INTO articles
           (metaid, paperid, date, board_metaid, title, bbox_x, bbox_y, bbox_w, bbox_h, poly_points, status)
           VALUES (:metaid, :paperid, :date, :board_metaid, :title,
                   :bbox_x, :bbox_y, :bbox_w, :bbox_h, :poly_points, 'pending')""",
        articles,
    )
    await db.commit()


async def pending_articles(
    db: aiosqlite.Connection, limit: int = 500
) -> list[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT * FROM articles WHERE status='pending' LIMIT ?", (limit,)
    )
    return list(await cur.fetchall())


async def update_article_status(
    db: aiosqlite.Connection, metaid: str, status: str, **fields
) -> None:
    """更新报道状态与内容字段"""
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE articles SET {cols}, status=? WHERE metaid=?"
        await db.execute(sql, (*fields.values(), status, metaid))
    else:
        await db.execute(
            "UPDATE articles SET status=? WHERE metaid=?", (status, metaid)
        )
    await db.commit()


# ---- 状态工具 ----


async def set_issue_status(
    db: aiosqlite.Connection,
    paperid: str,
    date: str,
    status: str,
    error: str | None = None,
) -> None:
    if error:
        await db.execute(
            "UPDATE issues SET status=?, error=? WHERE paperid=? AND date=?",
            (status, error, paperid, date),
        )
    else:
        await db.execute(
            "UPDATE issues SET status=? WHERE paperid=? AND date=?",
            (status, paperid, date),
        )
    await db.commit()


async def set_board_status(
    db: aiosqlite.Connection, metaid: str, status: str, **fields
) -> None:
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE boards SET {cols}, status=? WHERE metaid=?"
        await db.execute(sql, (*fields.values(), status, metaid))
    else:
        await db.execute("UPDATE boards SET status=? WHERE metaid=?", (status, metaid))
    await db.commit()
