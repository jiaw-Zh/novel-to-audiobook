"""
MiMo-V2.5-TTS 引擎封装
支持三种模式：预置音色 / 文本设计音色 / 音频克隆音色
"""
import base64
import os
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import numpy as np
import soundfile as sf
from openai import OpenAI

from config import TTSConfig

logger = logging.getLogger(__name__)


@dataclass
class TTSRequest:
    """一个 TTS 请求"""
    text: str                                    # 要合成的文本
    mode: str = "voicedesign"                    # preset / voicedesign / clone
    # 预置音色模式
    voice_id: str = "白桦"                       # 预置音色 ID
    # 音色设计模式
    voice_prompt: str = ""                       # 音色描述文本
    # 音色克隆模式
    clone_audio_path: str = ""                   # 参考音频路径
    # 风格控制
    style_instruction: str = ""                  # 自然语言风格指令（user message）
    # 输出
    output_path: str = ""                        # 输出文件路径


class TTSEngine:
    """MiMo TTS 引擎"""

    def __init__(self, config: Optional[TTSConfig] = None):
        self.config = config or TTSConfig()
        api_key = self.config.api_key or os.environ.get("MIMO_API_KEY", "")
        if not api_key:
            raise ValueError(
                "请设置 MIMO_API_KEY 环境变量，或在 config 中指定 api_key。\n"
                "前往 https://platform.xiaomimimo.com 获取 API Key。"
            )
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.config.base_url,
        )

    def synthesize(self, request: TTSRequest) -> str:
        """
        合成语音，返回输出文件路径。
        根据 request.mode 选择不同的 TTS 模型。
        """
        if request.mode == "preset":
            return self._synth_preset(request)
        elif request.mode == "voicedesign":
            return self._synth_voicedesign(request)
        elif request.mode == "clone":
            return self._synth_clone(request)
        else:
            raise ValueError(f"未知的 TTS 模式: {request.mode}")

    def _synth_preset(self, request: TTSRequest) -> str:
        """预置音色模式"""
        messages = []

        # user message: 风格指令（可选）
        if request.style_instruction:
            messages.append({
                "role": "user",
                "content": request.style_instruction,
            })

        # assistant message: 要合成的文本
        # 如果有风格指令，可以在文本前加音频标签
        content = request.text
        if request.style_instruction and not content.startswith("("):
            # 尝试从 style_instruction 提取简短标签
            tag = self._extract_style_tag(request.style_instruction)
            if tag:
                content = f"({tag}){content}"

        messages.append({
            "role": "assistant",
            "content": content,
        })

        return self._call_api(
            model=self.config.model_preset,
            messages=messages,
            voice=request.voice_id,
            output_path=request.output_path,
        )

    def _synth_voicedesign(self, request: TTSRequest) -> str:
        """文本设计音色模式"""
        if not request.voice_prompt:
            raise ValueError("voicedesign 模式需要 voice_prompt（音色描述）")

        messages = [
            {
                "role": "user",
                "content": request.voice_prompt,
            },
        ]

        # 如果有额外的风格指令，追加到 user content
        if request.style_instruction:
            messages[0]["content"] = f"{request.voice_prompt}\n{request.style_instruction}"

        # assistant message: 要合成的文本
        messages.append({
            "role": "assistant",
            "content": request.text,
        })

        return self._call_api(
            model=self.config.model_design,
            messages=messages,
            voice="",  # voicedesign 不需要 voice 参数
            output_path=request.output_path,
        )

    def _synth_clone(self, request: TTSRequest) -> str:
        """音频克隆音色模式"""
        if not request.clone_audio_path:
            raise ValueError("clone 模式需要 clone_audio_path（参考音频路径）")

        # 读取参考音频并转 base64
        audio_path = request.clone_audio_path
        mime_type = "audio/wav"
        if audio_path.endswith(".mp3"):
            mime_type = "audio/mpeg"

        with open(audio_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        voice_data = f"data:{mime_type};base64,{audio_b64}"

        messages = [
            {
                "role": "user",
                "content": request.style_instruction or "",
            },
            {
                "role": "assistant",
                "content": request.text,
            },
        ]

        return self._call_api(
            model=self.config.model_clone,
            messages=messages,
            voice=voice_data,
            output_path=request.output_path,
        )

    def _call_api(
        self,
        model: str,
        messages: list,
        voice: str,
        output_path: str,
    ) -> str:
        """调用 MiMo TTS API"""
        audio_params = {"format": self.config.audio_format}
        if voice:
            audio_params["voice"] = voice

        for attempt in range(self.config.max_retries):
            try:
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    audio=audio_params,
                )

                message = completion.choices[0].message
                if not hasattr(message, "audio") or not message.audio:
                    raise RuntimeError(f"API 返回无音频数据: {completion}")

                audio_bytes = base64.b64decode(message.audio.data)

                # 确保输出目录存在
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

                if self.config.audio_format == "wav":
                    with open(output_path, "wb") as f:
                        f.write(audio_bytes)
                elif self.config.audio_format == "pcm16":
                    # PCM16 -> WAV
                    pcm = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    sf.write(output_path, pcm, samplerate=self.config.sample_rate)
                else:
                    with open(output_path, "wb") as f:
                        f.write(audio_bytes)

                logger.debug(f"合成完成: {output_path}")
                return output_path

            except Exception as e:
                logger.warning(f"TTS 调用失败 (尝试 {attempt + 1}/{self.config.max_retries}): {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay * (attempt + 1))
                else:
                    raise

    def _extract_style_tag(self, instruction: str) -> Optional[str]:
        """从自然语言风格指令中提取简短标签"""
        tag_map = {
            "开心": "开心", "高兴": "开心", "快乐": "开心",
            "悲伤": "悲伤", "伤心": "悲伤", "难过": "悲伤",
            "愤怒": "愤怒", "生气": "愤怒", "暴怒": "愤怒",
            "恐惧": "恐惧", "害怕": "恐惧", "惊恐": "恐惧",
            "温柔": "温柔", "轻柔": "温柔",
            "严肃": "严肃", "庄重": "严肃",
            "慵懒": "慵懒", "疲惫": "疲惫",
            "磁性": "磁性", "沙哑": "沙哑",
            "苍老": "苍老", "年迈": "苍老",
            "稚嫩": "稚嫩", "童声": "稚嫩",
        }
        for keyword, tag in tag_map.items():
            if keyword in instruction:
                return tag
        return None
