"""
HTML 解析模块
=============
把浏览器验证过的解析逻辑固化成代码:
  - 期次列表: newspaper.issue 的 XML
  - 版面列表: 版面页 HTML 里的 nb.* 链接(需探测,编码不统一)
  - 版面位置: <map> area 多边形坐标 + 对应文章 metaid
  - 报道正文: h2.bo 标题 + .daxiao 元数据 + #zoom 正文
  - 报道配图: 文章页 <img cnml.files/*.resbrief.jpg>

全部基于纯文本 HTML 解析,不依赖浏览器。
"""

import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ---------------- 期次 ----------------


def parse_issue_dates(xml_text: str) -> list[str]:
    """从 newspaper.issue 的 XML 中提取该月所有有收录的日期"""
    dates = re.findall(r"<IssueDate>([^<]+)</IssueDate>", xml_text)
    return dates


# ---------------- 版面 ----------------


def parse_boards_from_page(html: str, date: str) -> list[dict]:
    """
    从版面页 HTML 提取该期全部版面。
    返回 [{metaid, board_no, board_name}]
    排除: 上一期/下一期/上一版/下一版 等导航链接。
    """
    soup = BeautifulSoup(html, "html.parser")
    boards: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"metaid=(nb\.[^&]+)", href)
        if not m:
            continue
        metaid = m.group(1)
        # 只保留当前日期的版面(排除 上一期/下一期)
        if date.replace("-", "") not in metaid:
            continue
        # 排除导航链接(文本是"上一期/下一期/上一版/下一版")
        text = a.get_text(" ", strip=True).strip()
        if text in ("上一期", "下一期", "上一版", "下一版", "上一页", "下一页"):
            continue
        # 提取版号 + 版面名,如 "A1版：时政要闻" -> board_no="A1", name="时政要闻"
        # 匹配 "A01版" / "A1版" / "A1B版" 等
        bm = re.match(r"^(A?\d+[A-Z]?)版", text)
        if bm:
            board_no = bm.group(1)
            name = text[bm.end() :].lstrip("：: ")
        else:
            board_no = metaid.split("_")[-1]
            name = ""
        if metaid not in boards:
            boards[metaid] = {
                "metaid": metaid,
                "board_no": board_no,
                "board_name": name,
            }
    return list(boards.values())


# ---------------- 版面位置 ----------------


def parse_article_positions(html: str, date: str) -> list[dict]:
    """
    从版面页 HTML 的 <map> area 提取每篇报道的位置。
    返回 [{metaid, title, poly_points, bbox}]
    poly_points: 原始多边形坐标 [[x,y],...]
    bbox:        {x,y,w,h} 包围盒
    """
    soup = BeautifulSoup(html, "html.parser")
    map_el = soup.find("map", attrs={"name": "articles"})
    if not map_el:
        return []
    results = []
    for area in map_el.find_all("area"):
        coords = area.get("coords", "")
        metaid = ""
        onclick = area.get("onclick", "")
        m = re.search(r"nw\.[A-Z0-9a-z_.]+", onclick)
        if m:
            metaid = m.group(0)
        title = area.get("titlestr", "") or area.get("title", "") or ""
        points = parse_poly(coords)
        if not points:
            continue
        results.append(
            {
                "metaid": metaid,
                "title": title,
                "poly_points": points,
                "bbox": bbox_of(points),
            }
        )
    return results


def parse_poly(coords: str) -> list[tuple[float, float]]:
    """解析 area coords 字符串 -> [(x,y),...]"""
    nums = [float(x) for x in coords.replace(",", " ").split()]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def bbox_of(points: list[tuple[float, float]]) -> dict:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


# ---------------- 报道 ----------------


def parse_article(html: str) -> dict | None:
    """
    从报道页 HTML 提取标题、元信息、正文。
    返回 {title, source_meta, content}
    """
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("h2", class_="bo")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    # 元信息: .daxiao li 第一行如 "南阳日报/2025-06-27/ 第A1版面/时政要闻"
    meta_el = soup.select_one("li.daxiao")
    source_meta = ""
    if meta_el:
        raw = meta_el.get_text("\n", strip=True).split("\n")[0]
        source_meta = raw
    # 正文: #zoom 里的段落
    content_el = soup.select_one("#zoom")
    content = ""
    if content_el:
        # 只取 p 文本,过滤脚本/空
        parts = [p.get_text("", strip=True) for p in content_el.find_all("p")]
        content = "\n".join(p for p in parts if p)
        if not content:
            content = content_el.get_text(" ", strip=True)
    if not title and not content:
        return None
    return {"title": title, "source_meta": source_meta, "content": content}


def parse_article_images(html: str) -> list[str]:
    """
    从报道页 HTML 提取配图 URL(cnml.files/*.resbrief.jpg)。
    返回 URL 列表(去重)。
    """
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "cnml.files" in src and ".resbrief." in src and src not in seen:
            seen.add(src)
            urls.append(src)
    return urls


# ---------------- 版面图 URL ----------------


def build_board_image_url(paperid: str, date: str, board_no: str) -> str:
    """
    构造版面图 URL。
    paperid: n.D411300nyrb
    date:    YYYY-MM-DD
    board_no: A1 / A01
    注意: 版面编码 A01 vs A1 需要在版面页里拿真实 board_no(含在 metaid 中)。
    """
    code = paperid.replace("n.", "")  # D411300nyrb —— 目录名和 metaid 都带 D
    y, m, d = date.split("-")
    return (
        f"https://img.enews.apabi.com/{code}/{y}-{m}/{d}/mpml.files/"
        f"nb.{code}_{date.replace('-', '')}-{board_no}.pagebrief.1.jpg"
    )


def build_issue_url(paperid: str, year: int, month: int) -> str:
    """构造月份期次查询 URL"""
    return (
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"
        f"?pid=newspaper.issue&year={year}&month={month}&metaid={paperid}"
    )


def build_board_page_url(metaid: str) -> str:
    return (
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"
        f"?pid=newspaper.page&metaid={metaid}&cult=CN"
    )


def build_article_url(metaid: str) -> str:
    return (
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"
        f"?pid=newspaper.article&metaid={metaid}&cult=CN"
    )
