"""
LLM 辅助小说解析器
使用大模型精确识别角色、对话归属和情感
"""
import json
import logging
import os
import re
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# ── 系统提示词 ──────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个专业的小说文本分析助手。你的任务是分析小说片段，提取其中的角色信息和对话归属。

输出格式要求（严格 JSON）：
```json
{
  "characters": [
    {"name": "角色名", "gender": "male/female/unknown", "age": "young/middle/old/unknown", "description": "简短描述"}
  ],
  "segments": [
    {
      "text": "原始文本（含引号）",
      "type": "narration/dialogue",
      "speaker": "说话人名字（对话类型必须填写，旁白填 null）",
      "emotion": "情感（可选）",
      "voice_hint": "音色提示（可选，如：温柔、苍老、年轻男性等）"
    }
  ]
}
```

规则：
1. 对话文本保留原始引号
2. 旁白的 speaker 填 null
3. 对话的 speaker 必须是 characters 列表中的某个角色名
4. 如果无法确定说话人，speaker 填 "未知"
5. emotion 可选值：喜/怒/哀/惧/惊/急/平静/冷漠/温柔 等
6. voice_hint 用于后续 TTS 音色设计，描述该角色说话时的声音特征
7. 只输出 JSON，不要有其他文字"""


def build_analysis_prompt(text: str) -> str:
    """构建分析提示词"""
    return f"""请分析以下小说片段，提取所有角色和对话归属：

---
{text}
---

请严格按照 JSON 格式输出。"""


def build_chapter_split_prompt(text: str) -> str:
    """构建章节拆分提示词"""
    return f"""请分析以下小说文本，识别所有章节标题，并为每个章节列出主要角色。

---
{text[:3000]}
---

输出格式（严格 JSON）：
```json
{{
  "chapters": [
    {{
      "title": "第一章 初入江湖",
      "characters": ["林风", "苏瑶", "老者"]
    }}
  ]
}}
```

只输出 JSON。"""


class LLMParser:
    """基于 LLM 的小说解析器"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.5-pro",
        chunk_size: int = 2000,
    ):
        self.api_key = api_key or os.environ.get("MIMO_API_KEY", "")
        self.base_url = base_url
        self.model = model
        self.chunk_size = chunk_size

        if not self.api_key:
            raise ValueError(
                "请设置 MIMO_API_KEY 环境变量或传入 api_key。\n"
                "前往 https://platform.xiaomimimo.com 获取。"
            )

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self._all_characters: list[dict] = []

    def analyze_text(self, text: str) -> dict:
        """
        分析一段文本，返回结构化的角色和段落信息。
        自动按 chunk_size 拆分长文本。
        """
        chunks = self._split_text(text)
        all_segments = []
        all_characters = {}

        for i, chunk in enumerate(chunks):
            logger.info(f"  LLM 分析第 {i+1}/{len(chunks)} 段...")
            result = self._analyze_chunk(chunk)

            if result:
                # 合并角色信息
                for char in result.get("characters", []):
                    name = char.get("name", "")
                    if name and name not in all_characters:
                        all_characters[name] = char

                # 合并段落
                all_segments.extend(result.get("segments", []))

        self._all_characters = list(all_characters.values())
        return {
            "characters": self._all_characters,
            "segments": all_segments,
        }

    def analyze_chapters(self, text: str) -> dict:
        """分析章节结构和角色"""
        prompt = build_chapter_split_prompt(text)
        result = self._call_llm(prompt)
        return self._parse_json(result) or {}

    def get_all_characters(self) -> list[dict]:
        """获取所有已识别的角色"""
        return self._all_characters

    def _analyze_chunk(self, text: str) -> Optional[dict]:
        """分析单个文本块"""
        prompt = build_analysis_prompt(text)
        result = self._call_llm(prompt)
        return self._parse_json(result)

    def _call_llm(self, user_prompt: str) -> Optional[str]:
        """调用 LLM API（流式模式，适配推理模型）"""
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=16384,
                stream=True,
            )
            full = ""
            for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        full += delta.content
            return full if full.strip() else None
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None

    def _parse_json(self, text: Optional[str]) -> Optional[dict]:
        """从 LLM 输出中提取 JSON"""
        if not text:
            return None

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 块
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试找到第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning(f"无法解析 LLM 输出为 JSON: {text[:200]}...")
        return None

    def _split_text(self, text: str) -> list[str]:
        """按段落拆分文本，每块不超过 chunk_size 字符"""
        paragraphs = text.split("\n")
        chunks = []
        current_chunk = []
        current_size = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            para_size = len(para)

            # 如果当前块加上新段落超过限制，先保存当前块
            if current_size + para_size > self.chunk_size and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_size = 0

            current_chunk.append(para)
            current_size += para_size

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks


def llm_result_to_segments(llm_result: dict) -> list[dict]:
    """
    将 LLM 分析结果转换为与 parser.py 兼容的格式。
    返回: [{"text": ..., "type": ..., "speaker": ..., "emotion": ..., "voice_hint": ...}, ...]
    """
    segments = []
    for seg in llm_result.get("segments", []):
        segments.append({
            "text": seg.get("text", ""),
            "type": seg.get("type", "narration"),
            "speaker": seg.get("speaker"),
            "emotion": seg.get("emotion"),
            "voice_hint": seg.get("voice_hint"),
        })
    return segments


def llm_characters_to_voice_config(characters: list[dict]) -> dict:
    """
    将 LLM 识别的角色信息转换为 voices.yaml 格式。
    返回: {"characters": {"角色名": {"mode": "voicedesign", "voice_prompt": ...}}}
    """
    config = {"characters": {}}

    for char in characters:
        name = char.get("name", "")
        if not name:
            continue

        # 构建音色描述
        voice_parts = []

        gender = char.get("gender", "")
        age = char.get("age", "")
        description = char.get("description", "")
        voice_hint = char.get("voice_hint", "")

        if gender == "male":
            voice_parts.append("男性")
        elif gender == "female":
            voice_parts.append("女性")

        if age == "young":
            voice_parts.append("年轻")
        elif age == "middle":
            voice_parts.append("中年")
        elif age == "old":
            voice_parts.append("老年")

        if voice_hint:
            voice_parts.append(voice_hint)
        elif description:
            voice_parts.append(description)

        voice_prompt = "，".join(voice_parts) if voice_parts else "自然的说话声音"

        config["characters"][name] = {
            "mode": "voicedesign",
            "voice_prompt": voice_prompt,
            "gender": gender,
            "age": age,
        }

    return config
