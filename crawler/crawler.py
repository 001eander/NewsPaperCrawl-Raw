"""
异步爬虫主模块
==============
四阶段流水线(每阶段独立、可断点续跑):

  阶段0  import_papers    从 报纸清单.csv 导入 papers 表(可重复执行)
  阶段1  crawl_issues     每报×每月调 newspaper.issue,枚举全部期次
  阶段2  crawl_boards     每期抓版面页 -> 版面列表 + 版面位置 + 版面图
  阶段3  crawl_articles   每篇抓报道页 -> 保序正文 JSON + 配图

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
from .config import BASE_URL, CONCURRENCY, DATA_DIR, PAPERLIST_CSV
from .logging_config import ProgressLogger, setup_logging
from .notifier import notify_job_result

logger = logging.getLogger(__name__)

# 报道落库是最高频事件,用节流日志避免 crawl.log 膨胀(见 ProgressLogger)
_article_progress = ProgressLogger(__name__)


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
    concurrency: int = CONCURRENCY,
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


async def _gather_in_batches(items, worker, batch_size: int) -> None:
    """把 items 切成 batch_size 大小的批,逐批 gather,一批完成才取下一批。

    - worker 处理单个 item,内部自行捕获非致命异常并计数(nonlocal processed)。
    - 致命异常(AuthError)由 worker 重新 raise,gather 首异常取消同批其余任务并上抛。
    - items 切片已物化,不再发 DB 查询;items 为空时 range 为空,直接 no-op。
    """
    for start in range(0, len(items), batch_size):
        await asyncio.gather(*(worker(it) for it in items[start : start + batch_size]))


# ---------------- 阶段2:版面 + 位置 + 版面图 ----------------


async def crawl_boards(
    client: HttpClient,
    d,
    paperid: str | None = None,
    limit: int | None = None,
    concurrency: int = CONCURRENCY,
) -> int:
    """处理待爬期次:抓版面列表、版面位置、下载版面图。返回处理期次数。"""
    issues = await db.pending_issues(d, paperid, limit)
    processed = 0
    # 每期内部还会摊开到各版面(client._sem 已全局封顶),外批保守取小,避免峰值协程过多
    batch_size = max(concurrency, 4)

    async def one_issue(issue):
        nonlocal processed
        paperid_i, date_i = issue["paperid"], issue["date"]
        try:
            ok = await _crawl_one_issue(client, d, paperid_i, date_i)
            await db.set_issue_status(d, paperid_i, date_i, "done" if ok else "failed")
            processed += 1
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("期次 %s %s 失败: %s", paperid_i, date_i, e)
            await db.set_issue_status(d, paperid_i, date_i, "failed", str(e))

    await _gather_in_batches(issues, one_issue, batch_size)
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
    logger.info("期次落库 %s %s: 版面 %s 个", paperid, date_i, len(board_rows))

    # 各版面并行:下载版面图 + 解析位置。client._sem 全局封顶真实并发。
    # 版面异常直接冒泡:gather 首异常取消同批其余版面 → 整期 failed(与串行语义一致)。
    await asyncio.gather(
        *(_crawl_one_board(client, d, paperid, date_i, b) for b in boards)
    )
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
        logger.info(
            "版面 %s %s 落库 %s 篇报道: %s",
            paperid,
            date_i,
            len(article_rows),
            board_code,
        )
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


async def crawl_articles(
    client: HttpClient, d, limit: int | None = None, concurrency: int = CONCURRENCY
) -> int:
    articles = await db.pending_articles(d, limit)
    processed = 0
    # 单篇 worker 轻量(正文 1 请求 + 至多几张配图),批可放大;client._sem 已全局封顶
    batch_size = max(concurrency * 2, 100)

    async def one_article(art):
        nonlocal processed
        metaid = art["metaid"]
        try:
            await _crawl_one_article(client, d, art)
            processed += 1
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("报道 %s 失败: %s", metaid, e)
            await db.update_article_status(d, metaid, "failed", error=str(e))

    await _gather_in_batches(articles, one_article, batch_size)
    return processed


async def _crawl_one_article(client: HttpClient, d, art):
    metaid = art["metaid"]
    html = await client.get(parser.build_article_url(metaid))
    parsed = parser.parse_article(html)
    if not parsed:
        await db.update_article_status(d, metaid, "failed", error="无法解析")
        return
    blocks = parsed["blocks"]
    # 按 blocks 中图片顺序取 URL,保证下载序号与 img 块 index 一致
    img_urls = [b["src"] for b in blocks if b["type"] == "img"]
    # 先下载全部配图,拿到最终 local_path,再写 JSON(只写一次,内容与已下载文件一致)
    img_results = await _save_article_images(client, d, art, img_urls)
    content_rel = write_article_json(art, parsed, img_results)
    fields = {
        "title": parsed["title"],
        "content_path": content_rel,  # 相对 DATA_DIR 的路径
        "source_meta": parsed["source_meta"],
        "image_count": len(img_urls),  # = img 块数
    }
    await db.update_article_status(d, metaid, "done", **fields)
    _article_progress.tick("报道落库 %s: %s", metaid, parsed["title"])


async def _save_article_images(
    client: HttpClient, d, art, img_urls: list[str]
) -> list[dict]:
    """
    按序下载配图到文章目录(每篇文章一个目录),返回有序结果。
    文件名序号(1 起)与 blocks 中 img 块 index 对应,保证 JSON 引用最终文件。

    返回 [{url, local_path, ok}];local_path 为相对 DATA_DIR 的路径,
    下载失败为 None(JSON 里该 img 块回退 src)。
    """
    code = art["paperid"].replace("n.D", "")
    tail = art["metaid"].split(".")[-1]
    date_path = art["date"].replace("-", "/")
    art_dir = DATA_DIR / "articles" / code / date_path / tail
    art_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, url in enumerate(img_urls, 1):
        suffix = Path(url).suffix or ".jpg"
        # 相对路径统一用 / 分隔(as_posix),Windows 上跑出的 DB 路径也能被 Linux 容器解析
        rel = (Path("articles") / code / date_path / tail / f"{i}{suffix}").as_posix()
        ok = await client.download(url, DATA_DIR / rel)
        results.append({"url": url, "local_path": rel if ok else None, "ok": ok})
    return results


def _article_json_path(art: dict) -> str:
    """正文 JSON 相对 DATA_DIR 的路径: articles/{code}/{date}/{tail}/content.json

    统一用 / 分隔(as_posix),与平台无关——DB 里存的相对路径跨平台可解析,
    便于 Docker 卷在 Windows 开发机与 Linux 容器间迁移。
    """
    code = art["paperid"].replace("n.D", "")
    tail = art["metaid"].split(".")[-1]
    return (
        Path("articles") / code / art["date"].replace("-", "/") / tail / "content.json"
    ).as_posix()


def write_article_json(art: dict, parsed: dict, img_results: list[dict]) -> str:
    """
    把保序 blocks 写为 content.json;按 img_results 回填各 img 块的 local_path。
    返回相对 DATA_DIR 的路径(便于 Docker 卷可移植)。
    """
    img_by_src = {r["url"]: r for r in img_results}
    final_blocks = []
    for b in parsed["blocks"]:
        if b["type"] == "img" and b["src"] in img_by_src:
            b = {**b, "local_path": img_by_src[b["src"]]["local_path"]}
        final_blocks.append(b)
    rel = _article_json_path(art)
    abs_path = DATA_DIR / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(
        json.dumps(
            {
                "metaid": art["metaid"],
                "paperid": art["paperid"],
                "date": art["date"],
                "board_metaid": art["board_metaid"],
                "title": parsed["title"],
                "source_meta": parsed["source_meta"],
                "blocks": final_blocks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return rel


# ---------------- 入口 ----------------


async def run(stage: str, **kwargs):
    setup_logging()
    if stage == "import":
        n = await import_papers_from_csv(kwargs.get("csv"))
        logger.info("阶段0 完成,导入报纸 %s", n)
        return
    d = None
    # CLI --concurrency 未给时(被 cli.py 过滤掉)回落环境变量/默认;max(1,...) 挡掉 0
    concurrency = max(1, kwargs.get("concurrency") or CONCURRENCY)
    try:
        d = await db.init_db()
        async with HttpClient(concurrency=concurrency) as client:
            if stage == "issues":
                n = await crawl_issues(
                    client,
                    d,
                    kwargs["start_year"],
                    kwargs["end_year"],
                    kwargs.get("end_month"),
                    concurrency,
                )
                logger.info("阶段1 完成,新增期次 %s", n)
            elif stage == "boards":
                n = await crawl_boards(
                    client,
                    d,
                    kwargs.get("paperid"),
                    kwargs.get("limit"),
                    concurrency,
                )
                logger.info("阶段2 完成,处理期次 %s", n)
            elif stage == "articles":
                n = await crawl_articles(client, d, kwargs.get("limit"), concurrency)
                logger.info("阶段3 完成,处理报道 %s", n)
    except AuthError as e:
        # 认证失效:推送 Bark 告知需要重新登录,再向外抛(CLI 以非零码退出)
        notify_job_result(outcome="auth", stage=stage)
        logger.error("认证失效: %s", e)
        raise
    except Exception as e:
        # 未知错误:推送 Bark 告知异常中断
        notify_job_result(outcome="error", stage=stage, message=str(e))
        raise
    else:
        # 正常完成:推送 Bark 告知本轮处理结果
        notify_job_result(outcome="done", stage=stage, processed=n)
    finally:
        # 无论正常完成还是异常(AuthError 等),都必须关闭 DB 连接,
        # 否则 aiosqlite 后台线程永远阻塞在队列等待,进程无法退出。
        if d is not None:
            try:
                await d.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭数据库连接失败: %s", e)
