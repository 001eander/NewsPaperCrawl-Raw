"""
异步爬虫主模块
==============
四阶段流水线(每阶段独立、可断点续跑):

  阶段0  import_papers    从 报纸清单.csv 导入 papers 表(可重复执行)
  阶段1  crawl_issues     每报×每月调 newspaper.issue,枚举全部期次
  阶段2  crawl_boards     每期抓版面页 -> 版面列表 + 版面位置 + 版面图
  阶段3  crawl_articles   每篇抓报道页 -> 正文 + 配图

每个阶段以 SQLite 的 status 为状态机,跳过已 done。
认证失效时抛 AuthError,由 Bark 通知用户重新登录。
"""

import asyncio
import csv
import json
import logging
import re
from pathlib import Path

from . import db, parser
from .auth import AuthError
from .client import HttpClient
from .config import BASE_URL, DATA_DIR, PAPERLIST_CSV

logger = logging.getLogger(__name__)


# ---------------- 阶段0:导入报纸清单 ----------------


async def import_papers_from_csv(
    csv_path: Path | None = None, db_path: Path = db.DB_PATH
) -> int:
    csv_path = csv_path or PAPERLIST_CSV
    if not csv_path.exists():
        raise FileNotFoundError(
            f"找不到报纸清单 CSV: {csv_path}\n"
            "请用 cli.py import --csv <路径> 或设置环境变量 NEWSPAPER_PAPERLIST_CSV"
        )
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.append(
                {
                    "paperid": row["paperid"],
                    "name": row["报纸名称"],
                    "issue_no": row["刊号"],
                    "period": row["刊期"],
                    "place": row["出版地"],
                    "start_date": row["订阅开始"],
                    "end_date": row["订阅结束"],
                }
            )
    d = await db.init_db(db_path)
    try:
        return await db.import_papers(d, rows)
    finally:
        await d.close()


# ---------------- 阶段1:枚举期次 ----------------


async def crawl_issues(
    client: HttpClient,
    d,
    start_year: int,
    end_year: int,
    end_month: int | None = None,
    concurrency: int = 8,
) -> int:
    """对每份报纸,按月查询期次。返回新增期次数。"""
    papers = await db.get_papers(d)
    sem = asyncio.Semaphore(concurrency)
    total = 0

    async def one_paper(p):
        nonlocal total
        paperid = p["paperid"]
        # 枚举该报所有月份
        for y in range(start_year, end_year + 1):
            months = (
                range(1, 13)
                if y < end_year or end_month is None
                else range(1, end_month + 1)
            )
            for m in months:
                async with sem:
                    url = parser.build_issue_url(paperid, y, m)
                    try:
                        xml = await client.get(url)
                    except AuthError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.warning("期次查询失败 %s %s-%s: %s", paperid, y, m, e)
                        continue
                    dates = parser.parse_issue_dates(xml)
                    if dates:
                        n = await db.add_issues(d, paperid, dates)
                        total += n
        logger.info("完成 %s (%s)", paperid, p["name"])

    await asyncio.gather(*[one_paper(p) for p in papers])
    return total


# ---------------- 阶段2:版面 + 位置 + 版面图 ----------------


async def crawl_boards(
    client: HttpClient, d, paperid: str | None = None, limit: int = 200
) -> int:
    """处理待爬期次:抓版面列表、版面位置、下载版面图。返回处理期次数。"""
    issues = await db.pending_issues(d, paperid, limit)
    processed = 0
    for issue in issues:
        paperid_i, date_i = issue["paperid"], issue["date"]
        try:
            ok = await _crawl_one_issue(client, d, paperid_i, date_i)
            await db.set_issue_status(d, paperid_i, date_i, "done" if ok else "failed")
            processed += 1
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("期次 %s %s 失败: %s", paperid_i, date_i, e)
            await db.set_issue_status(d, paperid_i, date_i, "failed", str(e))
    return processed


async def _crawl_one_issue(client: HttpClient, d, paperid: str, date_i: str) -> bool:
    """
    抓取一个期次:先探测任一版面页,拿到全部版面。
    版面编码不统一(A01 vs A1),必须从版面页探测,不能猜。
    """
    # 探测:先请求 paperid 版面页(默认最新期)拿当日版面列表
    # 方法:构造 nb.{code}_{date}_A1 试探?不。更稳:请求 paperid 页,页面含日期导航
    # 实际上最稳的是:newspaper.page&paperid=... 会返回某期,再取其 nb.* 链接
    probe_url = BASE_URL + f"?pid=newspaper.page&paperid={paperid}&wd=&cult=CN"
    html = await client.get(probe_url)
    # 若该页是默认最新期,日期可能不是我们要的日期。我们只想要它的版面编码格式。
    # 正确做法:拿默认期版面 metaid,替换日期为目标日期,再请求。
    boards = parser.parse_boards_from_page(html, date_i)
    logger.debug("第一轮(paperid 页)解析出 %s 个版面", len(boards))
    if not boards:
        # 默认期不是目标期,尝试用日期已知格式:需要知道编码。
        # 从页面里拿一个 nb.* 的日期模板,替换日期。
        template = _find_board_metaid_template(html)
        logger.debug("模板 metaid: %s", template)
        if template:
            nb_meta = template.replace(extract_date(template), date_i.replace("-", ""))
            html2 = await client.get(parser.build_board_page_url(nb_meta))
            boards = parser.parse_boards_from_page(html2, date_i)
            logger.debug("第二轮(替换日期版)解析出 %s 个版面", len(boards))
    if not boards:
        logger.warning("期次 %s %s 无版面(可能未收录)", paperid, date_i)
        return False

    # 写入版面(board_name 列保留但已并入 board_no,置空)
    board_rows = [
        {
            "metaid": b["metaid"],
            "paperid": paperid,
            "date": date_i,
            "board_no": b["board_no"],
            "board_name": "",
        }
        for b in boards
    ]
    await db.add_boards(d, board_rows)

    # 对每个版面:下载版面图 + 解析位置
    for b in boards:
        await _crawl_one_board(client, d, paperid, date_i, b)
    return True


def extract_date(metaid: str) -> str:
    m = re.search(r"_(\d{8})_", metaid)
    return m.group(1) if m else ""


def _find_board_metaid_template(html: str) -> str | None:
    """从版面页 HTML 找一个 nb.* metaid 模板"""
    import re

    m = re.search(r"metaid=(nb\.[^&]+)", html)
    return m.group(1) if m else None


def extract_board_code(metaid: str) -> str:
    """从版面 metaid 取尾段作为版面编码,如 nb.D411300nyrb_20260731_A1 -> A1"""
    return metaid.split("_")[-1]


async def _crawl_one_board(
    client: HttpClient, d, paperid: str, date_i: str, board: dict
):
    """下载版面图 + 解析报道位置"""
    board_metaid = board["metaid"]
    board_code = extract_board_code(board_metaid)
    # 版面图 URL(传 metaid,由 build_board_image_url 取真实编码)
    img_url = parser.build_board_image_url(paperid, date_i, board_metaid)
    img_local = (
        DATA_DIR
        / "boards"
        / paperid.replace("n.D", "")
        / date_i.replace("-", "/")
        / f"{board_code}.jpg"
    )
    ok = await client.download(img_url, img_local)
    if ok:
        await db.set_board_status(
            d, board_metaid, "done", image_url=img_url, image_local=str(img_local)
        )
    else:
        logger.debug("版面图下载失败 %s", img_url)

    # 解析报道位置(从版面页 HTML)
    board_html = await client.get(parser.build_board_page_url(board_metaid))
    positions = parser.parse_article_positions(board_html, date_i)
    if positions:
        article_rows = []
        for pos in positions:
            bbox = pos["bbox"]
            article_rows.append(
                {
                    "metaid": pos["metaid"],
                    "paperid": paperid,
                    "date": date_i,
                    "board_metaid": board_metaid,
                    "title": pos["title"],
                    "bbox_x": bbox["x"],
                    "bbox_y": bbox["y"],
                    "bbox_w": bbox["w"],
                    "bbox_h": bbox["h"],
                    "poly_points": json.dumps(pos["poly_points"]),
                }
            )
        await db.add_articles(d, article_rows)
        # 保存位置 JSON 到 positions/
        pos_local = (
            DATA_DIR
            / "positions"
            / paperid.replace("n.D", "")
            / date_i.replace("-", "/")
            / f"{board_code}.json"
        )
        pos_local.parent.mkdir(parents=True, exist_ok=True)
        pos_local.write_text(
            json.dumps(
                {
                    "board_metaid": board_metaid,
                    "paperid": paperid,
                    "date": date_i,
                    "board_no": board["board_no"],
                    "img_w": 350,
                    "img_h": 550,
                    "articles": positions,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


# ---------------- 阶段3:报道正文 + 配图 ----------------


async def crawl_articles(client: HttpClient, d, limit: int = 500) -> int:
    articles = await db.pending_articles(d, limit)
    processed = 0
    for art in articles:
        metaid = art["metaid"]
        try:
            await _crawl_one_article(client, d, art)
            processed += 1
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("报道 %s 失败: %s", metaid, e)
            await db.update_article_status(d, metaid, "failed", error=str(e))
    return processed


async def _crawl_one_article(client: HttpClient, d, art):
    metaid = art["metaid"]
    html = await client.get(parser.build_article_url(metaid))
    parsed = parser.parse_article(html)
    if not parsed:
        await db.update_article_status(d, metaid, "failed", error="无法解析")
        return
    # 配图
    img_urls = parser.parse_article_images(html)
    fields = {
        "title": parsed["title"],
        "content": parsed["content"],
        "source_meta": parsed["source_meta"],
        "image_count": len(img_urls),
    }
    await db.update_article_status(d, metaid, "done", **fields)
    # 下载配图
    if img_urls:
        await _save_article_images(client, d, art, img_urls)


async def _save_article_images(client: HttpClient, d, art, img_urls: list[str]):
    paperid, date_i = art["paperid"], art["date"]
    metaid = art["metaid"]
    code = paperid.replace("n.D", "")
    # 文章元数据文件
    art_dir = DATA_DIR / "articles" / code / date_i.replace("-", "/")
    art_dir.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(img_urls, 1):
        suffix = Path(url).suffix or ".jpg"
        local = art_dir / f"{metaid.split('.')[-1]}_{i}{suffix}"
        await client.download(url, local)


# ---------------- 入口 ----------------


async def run(stage: str, **kwargs):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if stage == "import":
        n = await import_papers_from_csv(kwargs.get("csv"))
        logger.info("阶段0 完成,导入报纸 %s", n)
        return
    d = await db.init_db()
    async with HttpClient() as client:
        if stage == "issues":
            n = await crawl_issues(
                client,
                d,
                kwargs["start_year"],
                kwargs["end_year"],
                kwargs.get("end_month"),
            )
            logger.info("阶段1 完成,新增期次 %s", n)
        elif stage == "boards":
            n = await crawl_boards(
                client, d, kwargs.get("paperid"), kwargs.get("limit", 200)
            )
            logger.info("阶段2 完成,处理期次 %s", n)
        elif stage == "articles":
            n = await crawl_articles(client, d, kwargs.get("limit", 500))
            logger.info("阶段3 完成,处理报道 %s", n)
    await d.close()
