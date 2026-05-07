"""
音频拼接与导出
将多个 WAV 片段拼接成完整的有声书
"""
import os
import logging
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


class AudioMerger:
    """音频拼接器"""

    def __init__(
        self,
        sample_rate: int = 24000,
        output_format: str = "mp3",
        bitrate: str = "128k",
    ):
        self.sample_rate = sample_rate
        self.output_format = output_format
        self.bitrate = bitrate

    def create_silence(self, duration_ms: int) -> np.ndarray:
        """生成指定时长的静音"""
        num_samples = int(self.sample_rate * duration_ms / 1000)
        return np.zeros(num_samples, dtype=np.float32)

    def merge_files(
        self,
        audio_files: list[str],
        output_path: str,
        silence_ms: int = 400,
        chapter_silence_ms: int = 2000,
        chapter_markers: Optional[list[int]] = None,
    ) -> str:
        """
        拼接多个音频文件。

        Args:
            audio_files: WAV 文件路径列表
            output_path: 输出文件路径
            silence_ms: 段落间静音时长(ms)
            chapter_silence_ms: 章节间静音时长(ms)
            chapter_markers: 章节分界点的索引列表（在这些索引前插入章节静音）

        Returns:
            输出文件路径
        """
        if not audio_files:
            raise ValueError("没有音频文件可拼接")

        chapter_markers = chapter_markers or []
        all_chunks = []
        silence = self.create_silence(silence_ms)
        chapter_silence = self.create_silence(chapter_silence_ms)

        for i, filepath in enumerate(audio_files):
            try:
                data, sr = sf.read(filepath, dtype="float32")

                # 如果是多声道，转为单声道
                if len(data.shape) > 1:
                    data = data.mean(axis=1)

                # 重采样（如果需要）
                if sr != self.sample_rate:
                    # 简单重采样
                    ratio = self.sample_rate / sr
                    new_length = int(len(data) * ratio)
                    indices = np.linspace(0, len(data) - 1, new_length)
                    data = np.interp(indices, np.arange(len(data)), data)

                all_chunks.append(data)

                # 添加静音
                if i < len(audio_files) - 1:
                    if i in chapter_markers:
                        all_chunks.append(chapter_silence)
                    else:
                        all_chunks.append(silence)

            except Exception as e:
                logger.warning(f"读取音频文件失败 {filepath}: {e}")
                continue

        if not all_chunks:
            raise RuntimeError("没有有效的音频数据")

        # 合并
        merged = np.concatenate(all_chunks)

        # 归一化到 [-1, 1]
        max_val = np.max(np.abs(merged))
        if max_val > 1.0:
            merged = merged / max_val

        # 导出
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        wav_path = output_path
        if self.output_format != "wav":
            # 先保存为临时 WAV
            wav_path = output_path.rsplit(".", 1)[0] + ".wav"

        sf.write(wav_path, merged, samplerate=self.sample_rate)
        logger.info(f"WAV 已保存: {wav_path} ({len(merged)/self.sample_rate:.1f}秒)")

        # 转换格式
        if self.output_format == "mp3":
            final_path = self._convert_to_mp3(wav_path, output_path)
        elif self.output_format == "m4b":
            final_path = self._convert_to_m4b(wav_path, output_path)
        else:
            final_path = wav_path

        return final_path

    def _convert_to_mp3(self, wav_path: str, mp3_path: str) -> str:
        """WAV -> MP3"""
        if not mp3_path.endswith(".mp3"):
            mp3_path = mp3_path.rsplit(".", 1)[0] + ".mp3"

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", wav_path,
                    "-codec:a", "libmp3lame",
                    "-b:a", self.bitrate,
                    mp3_path,
                ],
                capture_output=True,
                check=True,
            )
            # 删除临时 WAV
            if wav_path != mp3_path and os.path.exists(wav_path):
                os.remove(wav_path)
            logger.info(f"MP3 已保存: {mp3_path}")
            return mp3_path
        except FileNotFoundError:
            logger.warning("未找到 ffmpeg，保持 WAV 格式")
            return wav_path
        except subprocess.CalledProcessError as e:
            logger.warning(f"ffmpeg 转换失败: {e.stderr.decode()}")
            return wav_path

    def _convert_to_m4b(self, wav_path: str, m4b_path: str) -> str:
        """WAV -> M4B (有声书格式)"""
        if not m4b_path.endswith(".m4b"):
            m4b_path = m4b_path.rsplit(".", 1)[0] + ".m4b"

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", wav_path,
                    "-codec:a", "aac",
                    "-b:a", self.bitrate,
                    "-movflags", "+faststart",
                    m4b_path,
                ],
                capture_output=True,
                check=True,
            )
            if wav_path != m4b_path and os.path.exists(wav_path):
                os.remove(wav_path)
            logger.info(f"M4B 已保存: {m4b_path}")
            return m4b_path
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            logger.warning(f"M4B 转换失败: {e}")
            return wav_path

    def get_duration(self, audio_path: str) -> float:
        """获取音频时长（秒）"""
        try:
            data, sr = sf.read(audio_path, dtype="float32")
            return len(data) / sr
        except Exception:
            return 0.0
