"""配置管理"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TTSConfig:
    """TTS 引擎配置"""
    api_key: str = ""  # 从环境变量 MIMO_API_KEY 读取
    base_url: str = "https://api.xiaomimimo.com/v1"
    # 三个 TTS 模型
    model_preset: str = "mimo-v2.5-tts"            # 预置音色
    model_design: str = "mimo-v2.5-tts-voicedesign" # 文本设计音色
    model_clone: str = "mimo-v2.5-tts-voiceclone"   # 音频克隆音色
    # 音频输出
    audio_format: str = "wav"  # wav（用于拼接）或 pcm16
    sample_rate: int = 24000
    # 重试
    max_retries: int = 3
    retry_delay: float = 2.0


@dataclass
class LLMConfig:
    """LLM 预处理配置"""
    api_key: str = ""  # 从环境变量 MIMO_API_KEY 读取
    base_url: str = "https://api.xiaomimimo.com/v1"
    model: str = "mimo-v2.5-pro"
    chunk_size: int = 2000  # 每次分析的文本块大小（字符数）
    temperature: float = 0.1
    max_tokens: int = 4096


@dataclass
class AudioConfig:
    """音频输出配置"""
    silence_between_paragraphs_ms: int = 400   # 段落间静音
    silence_between_chapters_ms: int = 2000    # 章节间静音
    silence_after_dialogue_ms: int = 200       # 对话后静音
    output_format: str = "mp3"                 # mp3 / wav / m4b
    bitrate: str = "128k"


@dataclass
class ParserConfig:
    """文本解析配置"""
    chapter_pattern: str = r"^第[零一二三四五六七八九十百千\d]+[章节回卷集部篇]"


@dataclass
class ProjectConfig:
    """项目总配置"""
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    voices_path: str = "voices.yaml"
    output_dir: str = "audiobook"
    log_level: str = "INFO"
