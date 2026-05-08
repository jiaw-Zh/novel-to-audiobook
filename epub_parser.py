"""
EPUB 解析器
工作流：
  1. 解析 EPUB 文件，提取目录和章节内容
  2. 按章节拆分，保存为独立的 .txt 文件
  3. 返回章节元信息，供后续 LLM + TTS 流程使用
"""
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class EpubChapter:
    """EPUB 章节信息"""
    index: int               # 章节序号（从 1 开始）
    title: str               # 章节标题
    text: str                # 纯文本内容
    word_count: int = 0      # 字数
    file_path: str = ""      # 保存的 txt 文件路径


@dataclass
class EpubBook:
    """EPUB 书籍信息"""
    title: str = ""
    author: str = ""
    language: str = ""
    chapters: list = field(default_factory=list)
    total_chars: int = 0
    source_path: str = ""


def parse_epub(epub_path: str, output_dir: str = None) -> EpubBook:
    """
    解析 EPUB 文件，按章节拆分。

    Args:
        epub_path: EPUB 文件路径
        output_dir: 章节 txt 文件输出目录（默认: <output_dir>/chapters/）

    Returns:
        EpubBook 对象，包含书籍信息和章节列表
    """
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError:
        raise ImportError("请安装 ebooklib: pip install ebooklib")

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError("请安装 beautifulsoup4: pip install beautifulsoup4")

    epub_path = str(Path(epub_path).resolve())
    if not Path(epub_path).exists():
        raise FileNotFoundError(f"EPUB 文件不存在: {epub_path}")

    # 默认输出目录
    if output_dir is None:
        output_dir = str(Path(epub_path).parent / "chapters")
    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"📖 解析 EPUB: {epub_path}")

    # 读取 EPUB
    book = epub.read_epub(epub_path)

    # 提取元信息
    meta = EpubBook(
        title=_get_meta(book, "title"),
        author=_get_meta(book, "creator"),
        language=_get_meta(book, "language"),
        source_path=epub_path,
    )
    logger.info(f"  标题: {meta.title}")
    logger.info(f"  作者: {meta.author}")

    # 提取目录结构
    toc_items = _extract_toc(book)
    logger.info(f"  目录: {len(toc_items)} 个条目")

    # 提取所有 HTML 内容（按文档顺序）
    html_items = _get_html_items(book)

    # 按目录拆分章节
    chapters = _split_into_chapters(html_items, toc_items, output_dir)

    meta.chapters = chapters
    meta.total_chars = sum(ch.word_count for ch in chapters)

    logger.info(f"\n📊 解析结果:")
    logger.info(f"  章节数: {len(chapters)}")
    logger.info(f"  总字数: {meta.total_chars:,}")
    logger.info(f"  输出目录: {output_dir}")

    for ch in chapters:
        logger.info(f"  第{ch.index}章: {ch.title} ({ch.word_count:,}字) → {ch.file_path}")

    return meta


def _get_meta(book, field_name: str) -> str:
    """提取 EPUB 元信息"""
    try:
        values = book.get_metadata("DC", field_name)
        if values:
            return values[0][0] if isinstance(values[0], tuple) else str(values[0])
    except Exception:
        pass
    return ""


def _extract_toc(book) -> list[dict]:
    """
    提取目录结构。
    返回: [{"title": "第一章 xxx", "href": "chapter1.xhtml"}, ...]
    """
    toc_items = []

    def walk_toc(items):
        for item in items:
            if isinstance(item, tuple):
                # (Section, [children])
                section, children = item
                toc_items.append({
                    "title": section.title.strip(),
                    "href": section.href.split("#")[0],
                })
                walk_toc(children)
            elif hasattr(item, "title"):
                toc_items.append({
                    "title": item.title.strip(),
                    "href": item.href.split("#")[0],
                })

    try:
        walk_toc(book.toc)
    except Exception as e:
        logger.warning(f"  提取目录失败: {e}")

    return toc_items


def _get_html_items(book) -> list[dict]:
    """
    获取所有 HTML 文档项（按 spine 顺序）。
    返回: [{"href": "...", "title": "...", "html": "..."}]
    """
    from bs4 import BeautifulSoup

    items = []
    for item in book.get_items():
        if item.get_type() == 9:  # ebooklib.ITEM_DOCUMENT
            content = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(content, "html.parser")

            # 提取标题（如果有）
            title = ""
            for tag in ["h1", "h2", "h3", "title"]:
                found = soup.find(tag)
                if found:
                    title = found.get_text(strip=True)
                    break

            items.append({
                "href": item.get_name(),
                "title": title,
                "html": content,
                "soup": soup,
            })

    return items


def _split_into_chapters(
    html_items: list[dict],
    toc_items: list[dict],
    output_dir: str,
) -> list[EpubChapter]:
    """
    按目录拆分章节。

    策略：
    1. 如果有目录，按目录的 href 关联 HTML 文件
    2. 如果没有目录，每个 HTML 文件视为一个章节
    3. 合并过短的章节（< 100 字）到前一个章节
    """
    from bs4 import BeautifulSoup

    chapters = []

    if toc_items:
        # 有目录：按目录拆分
        # 建立 href → html_item 的映射
        href_map = {}
        for item in html_items:
            href_map[item["href"]] = item

        # 按目录关联
        used_hrefs = set()
        for toc in toc_items:
            href = toc["href"]
            if href in href_map:
                item = href_map[href]
                text = _html_to_text(item["soup"])
                if text.strip():
                    chapters.append({
                        "title": toc["title"] or item["title"],
                        "text": text,
                        "href": href,
                    })
                    used_hrefs.add(href)

        # 添加未被目录引用的 HTML 文件
        for item in html_items:
            if item["href"] not in used_hrefs:
                text = _html_to_text(item["soup"])
                if len(text.strip()) > 100:  # 跳过太短的
                    chapters.append({
                        "title": item["title"] or _guess_title(item["href"]),
                        "text": text,
                        "href": item["href"],
                    })
    else:
        # 无目录：每个 HTML 文件视为一个章节
        for item in html_items:
            text = _html_to_text(item["soup"])
            if len(text.strip()) > 100:
                chapters.append({
                    "title": item["title"] or _guess_title(item["href"]),
                    "text": text,
                    "href": item["href"],
                })

    # 合并过短的章节
    merged = []
    for ch in chapters:
        if merged and len(ch["text"].strip()) < 100:
            # 合并到前一个章节
            merged[-1]["text"] += "\n\n" + ch["text"]
            continue
        merged.append(ch)

    # 保存为 txt 文件
    result = []
    for i, ch in enumerate(merged, 1):
        title = _clean_title(ch["title"])
        text = _clean_text(ch["text"])

        if not text.strip():
            continue

        # 写入文件
        clean_text = text.strip()
        content = f"{title}\n\n{clean_text}" if clean_text else title

        # 文件名：第001章_xxx.txt
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:30]
        filename = f"第{i:03d}章_{safe_title}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        result.append(EpubChapter(
            index=i,
            title=title,
            text=text,
            word_count=len(text),
            file_path=filepath,
        ))

    return result


def _html_to_text(soup) -> str:
    """将 BeautifulSoup 对象转换为纯文本，保留段落结构"""
    from bs4 import BeautifulSoup, NavigableString, Tag

    # 移除不需要的标签
    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    # 移除标题标签（标题已从 TOC 获取，避免重复）
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.decompose()

    lines = []

    for element in soup.body.descendants if soup.body else soup.descendants:
        if isinstance(element, NavigableString):
            text = str(element).strip()
            if text:
                lines.append(text)
        elif isinstance(element, Tag):
            if element.name in ["p", "div", "section", "article", "blockquote"]:
                lines.append("")  # 段落分隔
            elif element.name in ["br"]:
                lines.append("")

    # 去重空行，保留段落结构
    result = []
    prev_empty = False
    for line in lines:
        if not line:
            if not prev_empty:
                result.append("")
                prev_empty = True
        else:
            result.append(line)
            prev_empty = False

    return "\n".join(result).strip()


def _clean_text(text: str) -> str:
    """清理文本"""
    # 移除多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 移除行首尾空白
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _clean_title(title: str) -> str:
    """清理标题"""
    title = title.strip()
    # 移除常见的前缀后缀
    for prefix in ["Chapter ", "CHAPTER ", "第"]:
        if title.startswith(prefix):
            break
    return title


def _guess_title(href: str) -> str:
    """从文件名猜测标题"""
    name = Path(href).stem
    # 移除数字前缀
    name = re.sub(r"^[0-9_-]+", "", name)
    return name or "未命名章节"
