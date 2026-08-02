"""
parse_article 保序 blocks 测试
=============================
覆盖:
  - 段落与图片按 #zoom 文档序保序输出
  - 图片块 src/alt/index/local_path 字段正确
  - #zoom 外的装饰图被排除(cnml.files + .resbrief. 过滤)
  - 空 <p> 过滤,但其中嵌的图片保留
  - 纯图 / 纯文两种极端模式
  - 图片按 src 去重,仅保留首次出现
"""

from crawler import parser

# 一张 cnml 配图 URL(仿真实 apabi 格式)
IMG_URL = "https://http-img--enews--apabi--com.elib.zyproxy.zjlib.cn/D330100hzrb/2026-01/10/cnml.files/nt.D330100hzrb_20260110_1-1-A01.resbrief.jpg"


def _article_html(body: str) -> str:
    """构造一个带标题 + #zoom 正文的报道页"""
    return f"""
    <html><body>
    <h2 class="bo">测试标题</h2>
    <li class="daxiao">杭州日报/2026-01-10/ 第A01版面/要闻</li>
    <div id="zoom">{body}</div>
    </body></html>
    """


def test_paragraph_image_interleaving_preserves_order():
    """段落与图片交错时按文档序保序输出"""
    html = _article_html(f'<p>第一段</p><img src="{IMG_URL}" alt="配图"><p>第二段</p>')
    result = parser.parse_article(html)
    assert [b["type"] for b in result["blocks"]] == ["p", "img", "p"]
    assert result["blocks"][0]["text"] == "第一段"
    assert result["blocks"][1]["src"] == IMG_URL
    assert result["blocks"][1]["alt"] == "配图"
    assert result["blocks"][1]["index"] == 1
    assert result["blocks"][1]["local_path"] is None
    assert result["blocks"][2]["text"] == "第二段"


def test_image_in_p_is_extracted_before_paragraph():
    """图片嵌在 <p> 内部时(真实前置图模式),段落与图都保留、图为先"""
    html = _article_html(f"<center><p><img src='{IMG_URL}'></p></center><p>正文</p>")
    result = parser.parse_article(html)
    # find_all 文档序: 空 <p> -> <img> -> 正文 <p>;空段过滤后图先于正文
    assert [b["type"] for b in result["blocks"]] == ["img", "p"]
    assert result["blocks"][0]["index"] == 1
    assert result["blocks"][1]["text"] == "正文"


def test_zoom_outside_decorative_images_excluded():
    """#zoom 外的装饰图(非 cnml)被排除,不影响 blocks"""
    html = _article_html("<p>正文</p>")
    # 在 #zoom 外加装饰图
    html = html.replace(
        "</body>",
        '<img src="/Skins/default/images/img_downloadReader.jpg">'
        '<img src="x"><img src="{}"></body>'.format(
            IMG_URL.replace(".resbrief.", ".resbrief.")
        ),
    )
    result = parser.parse_article(html)
    # 只有 #zoom 内的段落,无任何图片块(装饰图在 zoom 外)
    assert [b["type"] for b in result["blocks"]] == ["p"]
    assert result["blocks"][0]["text"] == "正文"


def test_empty_p_filtered_but_image_kept():
    """纯图报道: center 内空 <p> 被过滤,图片保留"""
    html = _article_html(f"<center><p><img src='{IMG_URL}'></p></center><p></p>")
    result = parser.parse_article(html)
    assert [b["type"] for b in result["blocks"]] == ["img"]
    assert result["blocks"][0]["index"] == 1


def test_text_only_article():
    """纯文报道: 无图片,只有段落"""
    html = _article_html("<p>第一段</p><p>第二段</p><p>第三段</p>")
    result = parser.parse_article(html)
    assert [b["type"] for b in result["blocks"]] == ["p", "p", "p"]
    assert [b["text"] for b in result["blocks"]] == ["第一段", "第二段", "第三段"]


def test_duplicate_images_deduped_by_src():
    """相同 src 的图片去重,index 只对首次出现编号"""
    html = _article_html(f'<img src="{IMG_URL}"><p>文</p><img src="{IMG_URL}">')
    result = parser.parse_article(html)
    imgs = [b for b in result["blocks"] if b["type"] == "img"]
    assert len(imgs) == 1
    assert imgs[0]["index"] == 1
    assert result["blocks"][1]["text"] == "文"


def test_no_title_no_blocks_returns_none():
    """无标题且无正文时返回 None"""
    html = '<html><body><div id="zoom"><p></p></div></body></html>'
    assert parser.parse_article(html) is None


def test_returns_title_and_source_meta():
    """标题与元信息提取保持不变"""
    html = _article_html("<p>正文</p>").replace("测试标题", "河上龙灯 盛装进京")
    result = parser.parse_article(html)
    assert result["title"] == "河上龙灯 盛装进京"
    assert result["source_meta"] == "杭州日报/2026-01-10/ 第A01版面/要闻"
