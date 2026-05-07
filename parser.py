"""
小说文本解析器
- 按章节拆分
- 提取对话段落与旁白
- 识别说话角色
"""
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from config import ParserConfig


class SegmentType(Enum):
    NARRATION = "narration"      # 旁白/叙述
    DIALOGUE = "dialogue"        # 对话
    THOUGHT = "thought"          # 内心独白
    MIXED = "mixed"              # 混合（含对话引导语）


@dataclass
class Segment:
    """一个语音段落"""
    text: str                           # 要合成的文本
    original: str                       # 原始文本（含引号等）
    type: SegmentType
    speaker: Optional[str] = None       # 说话角色
    emotion: Optional[str] = None       # 推断的情感
    chapter: int = 0                    # 所属章节
    index: int = 0                      # 段落序号
    lead_in: Optional[str] = None       # 对话引导语（如"林风说道："）


@dataclass
class Chapter:
    """一个章节"""
    number: int
    title: str
    segments: list = field(default_factory=list)


# ── 角色名提取正则 ──────────────────────────────────────────────

# 匹配 "XXX说/道/喊/叫/问/答..." 模式
# name 组只捕获 2-4 个中文字符（人名通常 2-3 字）
_SPEAKER_PATTERNS_SRC = [
    # "XXX说/道：" — 紧跟引号前（最常见模式）
    r"(?P<name>[\u4e00-\u9fff]{2,4})(?:说|道|喊|叫|问|答|嚷|吼|应|回)(?:着)?(?:道|说|问|答|喊|叫)?(?:[：:]\s*)$",
    # "XXX，" — 引号前有逗号
    r"(?P<name>[\u4e00-\u9fff]{2,4})\s*[,，]\s*$",
]
SPEAKER_PATTERNS = [re.compile(p) for p in _SPEAKER_PATTERNS_SRC]

# 中文对话引号配对
QUOTE_PAIRS = [
    ("\u201c", "\u201d"),  # ""
    ("\u2018", "\u2019"),  # ''
    ("\u300c", "\u300d"),  # 「」
    ("\u300e", "\u300f"),  # 『』
]
# ASCII 引号（开闭相同，需要特殊处理）
ASCII_QUOTE = '"'


class NovelParser:
    """小说文本解析器"""

    def __init__(self, config: Optional[ParserConfig] = None):
        self.config = config or ParserConfig()
        self._known_speakers: set[str] = set()

    def parse_file(self, filepath: str) -> list[Chapter]:
        """解析小说文件，返回章节列表"""
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
        return self.parse_text(text)

    def parse_text(self, text: str) -> list[Chapter]:
        """解析文本，返回章节列表"""
        text = self._preprocess(text)
        chapters = self._split_chapters(text)
        for chapter in chapters:
            chapter.segments = self._parse_segments(chapter)
        return chapters

    def _preprocess(self, text: str) -> str:
        """文本预处理"""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _split_chapters(self, text: str) -> list[Chapter]:
        """按章节标题拆分"""
        pattern = re.compile(self.config.chapter_pattern, re.MULTILINE)
        splits = list(pattern.finditer(text))

        if not splits:
            ch = Chapter(number=1, title="\u5168\u6587")
            ch._raw_text = text
            return [ch]

        chapters = []
        for i, match in enumerate(splits):
            start = match.start()
            end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
            title = match.group().strip()
            ch = Chapter(number=i + 1, title=title)
            ch._raw_text = text[start:end].strip()
            chapters.append(ch)

        return chapters

    def _parse_segments(self, chapter: Chapter) -> list[Segment]:
        """解析章节中的语音段落"""
        raw = getattr(chapter, "_raw_text", "")
        if not raw:
            return []

        segments = []
        paragraphs = [p.strip() for p in raw.split("\n") if p.strip()]

        start_idx = 0
        if paragraphs and re.match(self.config.chapter_pattern, paragraphs[0]):
            start_idx = 1

        seg_index = 0
        for para in paragraphs[start_idx:]:
            parts = self._split_paragraph(para)
            for part in parts:
                part.chapter = chapter.number
                part.index = seg_index
                segments.append(part)
                seg_index += 1

        # 后处理：用已知角色名推断未知说话人
        segments = self._fill_unknown_speakers(segments)

        return segments

    def _fill_unknown_speakers(self, segments: list[Segment]) -> list[Segment]:
        """用已知角色名推断未知说话人（保守策略）"""
        if not self._known_speakers:
            return segments

        for i, seg in enumerate(segments):
            if seg.type != SegmentType.DIALOGUE or seg.speaker:
                continue

            # 优先：检查对话后的紧邻旁白
            # 中文模式："终于要到了。"林风长舒一口气
            # 旁白开头就是说话人
            if i + 1 < len(segments) and segments[i + 1].type == SegmentType.NARRATION:
                after_text = segments[i + 1].text
                for name in sorted(self._known_speakers, key=len, reverse=True):
                    # 名字必须在旁白最开头（前 5 字内）
                    if after_text.startswith(name) or after_text[:5].find(name) >= 0:
                        seg.speaker = name
                        break

            # 次选：检查对话前的紧邻旁白末尾
            # 中文模式：林风淡淡说道："路见不平，自然要管。"
            if not seg.speaker and i > 0 and segments[i - 1].type == SegmentType.NARRATION:
                before_text = segments[i - 1].text
                for name in sorted(self._known_speakers, key=len, reverse=True):
                    # 名字必须在旁白末尾（最后 15 字内，且后面紧跟说/道/问）
                    idx = before_text.rfind(name)
                    if idx >= 0 and idx >= len(before_text) - 15:
                        # 检查名字后面是否跟了说/道/问等
                        after_name = before_text[idx + len(name):]
                        if re.search(r'^[\s]*(?:说|道|问|答|喊|叫|笑|叹|吼|嚷)', after_name):
                            seg.speaker = name
                            break

        return segments

    def _split_paragraph(self, paragraph: str) -> list[Segment]:
        """将一个段落拆分为对话和旁白"""
        segments = []
        dialogues = self._extract_dialogues(paragraph)

        if not dialogues:
            if paragraph.strip():
                segments.append(Segment(
                    text=paragraph.strip(),
                    original=paragraph,
                    type=SegmentType.NARRATION,
                    speaker="\u65c1\u767d",
                ))
            return segments

        last_end = 0
        for dq_start, dq_end, dq_text, speaker in dialogues:
            if dq_start > last_end:
                narration = paragraph[last_end:dq_start].strip()
                if narration:
                    segments.append(Segment(
                        text=narration,
                        original=narration,
                        type=SegmentType.NARRATION,
                        speaker="\u65c1\u767d",
                    ))

            clean_text = self._clean_dialogue_text(dq_text)
            if clean_text:
                emotion = self._infer_emotion(dq_text, paragraph)
                segments.append(Segment(
                    text=clean_text,
                    original=dq_text,
                    type=SegmentType.DIALOGUE,
                    speaker=speaker,
                    emotion=emotion,
                ))

            last_end = dq_end

        if last_end < len(paragraph):
            narration = paragraph[last_end:].strip()
            if narration:
                segments.append(Segment(
                    text=narration,
                    original=narration,
                    type=SegmentType.NARRATION,
                    speaker="\u65c1\u767d",
                ))

        return segments

    def _extract_dialogues(self, text: str) -> list[tuple]:
        """
        提取文本中的所有对话
        返回: [(start, end, dialogue_text, speaker), ...]
        """
        results = []

        for open_mark, close_mark in QUOTE_PAIRS:
            pattern = re.compile(
                re.escape(open_mark) + r"(.*?)" + re.escape(close_mark),
                re.DOTALL
            )
            for match in pattern.finditer(text):
                dq_text = match.group(1)
                dq_start = match.start()
                dq_end = match.end()

                before = text[max(0, dq_start - 50):dq_start]
                after = text[dq_end:min(len(text), dq_end + 30)]
                speaker = self._infer_speaker(before, after)

                full = open_mark + dq_text + close_mark
                results.append((dq_start, dq_end, full, speaker))

        # 处理 ASCII 双引号（开闭相同字符）
        ascii_pattern = re.compile(r'"(.*?)"', re.DOTALL)
        for match in ascii_pattern.finditer(text):
            dq_text = match.group(1)
            dq_start = match.start()
            dq_end = match.end()

            before = text[max(0, dq_start - 50):dq_start]
            after = text[dq_end:min(len(text), dq_end + 30)]
            speaker = self._infer_speaker(before, after)

            full = '"' + dq_text + '"'
            results.append((dq_start, dq_end, full, speaker))

        results.sort(key=lambda x: x[0])

        # 去重（嵌套引号场景）
        deduped = []
        last_end = 0
        for item in results:
            if item[0] >= last_end:
                deduped.append(item)
                last_end = item[1]

        return deduped

    def _infer_speaker(self, before: str, after: str) -> Optional[str]:
        """从对话前后文本推断说话人"""
        stopwords = {
            # 代词
            "这个", "那个", "什么", "怎么", "为什么",
            "我们", "你们", "他们", "自己", "大家",
            "一下", "一个", "一些", "所有", "每个",
            "这里", "那里", "哪里", "这样", "那样",
            # 动词/形容词/副词（误捕获）
            "说道", "说着", "笑着", "叫着", "应道", "回道",
            "冷笑", "大笑", "哈哈", "嘿嘿",
            "淡淡", "缓缓", "慢慢", "急忙", "连忙",
            "突然", "忽然", "竟然", "居然", "微微",
            "轻轻", "慢慢", "静静", "默默", "悄悄",
            "低声", "高声", "大声", "小声", "轻声",
            # 常见误匹配
            "众人", "只见", "就在这", "正是",
            # 动作+说的组合
            "淡淡说", "缓缓说", "慢慢说", "急忙说",
            "冷冷说", "哈哈笑", "试探地", "忽然开",
            "者笑了",
        }

        # 从对话前文本推断（优先）
        for pattern in SPEAKER_PATTERNS:
            match = pattern.search(before)
            if match:
                name = match.group("name")
                # 处理 "林风淡淡说道" → 提取 "林风"
                name = self._clean_speaker_name(name)
                if name and name not in stopwords and len(name) >= 2:
                    self._known_speakers.add(name)
                    return name

        # 从对话后文本推断（"XXX说/道："格式）
        after_match = re.search(
            r"^[\s,，]*(?P<name>[\u4e00-\u9fff]{2,3})\s*(?:说|道|问|答|喊|叫)(?:[：:]?\s*)",
            after
        )
        if after_match:
            name = after_match.group("name")
            if name not in stopwords:
                self._known_speakers.add(name)
                return name

        return None

    # 常见副词后缀（用于清理误捕获的角色名）
    _ADVERB_SUFFIXES = [
        "淡淡", "缓缓", "慢慢", "急忙", "连忙", "轻轻", "静静",
        "默默", "悄悄", "微微", "哈哈", "嘿嘿", "冷冷", "狠狠",
        "拱手", "摇头", "点头", "笑道", "怒道", "叹道", "喊道",
        "笑了笑", "叹了口气", "说道", "问道", "开口", "地问",
        "地笑", "地说", "地说", "试探", "忽然",
    ]

    def _clean_speaker_name(self, name: str) -> str:
        """清理误捕获的角色名，去除副词后缀"""
        if len(name) <= 2:
            return name
        # 检查是否以常见副词结尾
        for suffix in self._ADVERB_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)]
        return name

    def _clean_dialogue_text(self, text: str) -> str:
        """清理对话文本"""
        return text.strip()

    def _infer_emotion(self, dialogue: str, context: str) -> Optional[str]:
        """推断对话情感（简单规则）"""
        emotion_keywords = {
            "\u559c": ["\u7b11", "\u9ad8\u5174", "\u5f00\u5fc3", "\u6b22\u559c", "\u54c8\u54c8", "\u563f\u563f", "\u592a\u597d\u4e86", "\u68d2"],
            "\u6012": ["\u6012", "\u9a82", "\u6df7\u86cb", "\u53ef\u6076", "\u8be5\u6b7b", "\u6eda", "\u53bb\u6b7b", "\u755c\u751f"],
            "\u54c0": ["\u54ed", "\u6cea", "\u4f24\u5fc3", "\u96be\u8fc7", "\u75db\u82e6", "\u5509", "\u545c\u545c", "\u60b2"],
            "\u60e7": ["\u6015", "\u6050\u60e7", "\u60ca", "\u5413", "\u98a4\u6296", "\u6218\u6218\u5141\u5141", "\u4e0d\u6562"],
            "\u60ca": ["\u554a", "\u4ec0\u4e48", "\u7adf\u7136", "\u5c45\u7136", "\u6ca1\u60f3\u5230", "\u5929\u54ea", "\u4e0d\u4f1a\u5427"],
            "\u6025": ["\u5feb", "\u8d76\u7d27", "\u9a6c\u4e0a", "\u7acb\u523b", "\u6025", "\u6765\u4e0d\u53ca", "\u7cdf\u7cd5"],
        }

        for emotion, keywords in emotion_keywords.items():
            for kw in keywords:
                if kw in dialogue or kw in context[-30:]:
                    return emotion
        return None

    def get_all_speakers(self) -> set[str]:
        """获取所有已识别的角色名"""
        return self._known_speakers.copy()
