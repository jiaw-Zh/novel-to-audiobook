#!/usr/bin/env python3
"""
小说转有声书 — CLI 入口
基于小米 MiMo-V2.5-TTS 实现多角色配音

工作流：
  1. 解析小说文件（txt / epub）
  2. 按章节拆分，保存为独立的 txt 文件
  3. LLM 分析每章的角色和对话归属（多线程）
  4. TTS 合成每章语音（多线程）
  5. 每章输出一个音频文件
"""
import argparse
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

import yaml
from tqdm import tqdm

from config import ProjectConfig, TTSConfig, LLMConfig, AudioConfig, ParserConfig
from parser import NovelParser, Segment, SegmentType, Chapter
from voice_manager import VoiceManager
from tts_engine import TTSEngine, TTSRequest
from audio_merger import AudioMerger

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ═══════════════════════════════════════════════════════════
# Step 1: 解析小说文件（单线程，本身就是纯内存操作，很快）
# ═══════════════════════════════════════════════════════════

def step_parse_novel(novel_path: str, output_dir: str) -> list[dict]:
    """
    解析小说文件，按章节拆分并保存为 txt。
    返回: [{"index": 1, "title": "第一章 xxx", "file_path": ".../第001章_xxx.txt", "word_count": 1234}, ...]
    """
    ext = Path(novel_path).suffix.lower()

    if ext == ".epub":
        return _parse_epub(novel_path, output_dir)
    elif ext == ".txt":
        return _parse_txt(novel_path, output_dir)
    else:
        raise ValueError(f"不支持的文件格式: {ext}（支持 .txt / .epub）")


def _parse_epub(epub_path: str, output_dir: str) -> list[dict]:
    from epub_parser import parse_epub
    chapters_dir = os.path.join(output_dir, "chapters")
    book = parse_epub(epub_path, chapters_dir)
    return [
        {"index": ch.index, "title": ch.title, "file_path": ch.file_path, "word_count": ch.word_count}
        for ch in book.chapters
    ]


def _parse_txt(txt_path: str, output_dir: str) -> list[dict]:
    import re
    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read()

    chapter_pattern = re.compile(r"^(第[零一二三四五六七八九十百千\d]+[章节回卷集部篇].*)$", re.MULTILINE)
    splits = list(chapter_pattern.finditer(text))

    chapters_dir = os.path.join(output_dir, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)

    if not splits:
        filepath = os.path.join(chapters_dir, "第001章_全文.txt")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        return [{"index": 1, "title": "全文", "file_path": filepath, "word_count": len(text)}]

    chapters = []
    for i, match in enumerate(splits):
        start = match.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        title = match.group().strip()
        body = text[start:end].strip()

        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:30]
        filename = f"第{i+1:03d}章_{safe_title}.txt"
        filepath = os.path.join(chapters_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(body)

        chapters.append({"index": i + 1, "title": title, "file_path": filepath, "word_count": len(body)})

    return chapters


# ═══════════════════════════════════════════════════════════
# Step 2: LLM 分析每章（多线程）
# ═══════════════════════════════════════════════════════════

def _analyze_one_chapter(ch: dict, analysis_dir: str, llm) -> tuple[int, dict]:
    """
    分析单个章节（供线程池调用）。
    返回: (chapter_index, result_dict)
    """
    idx = ch["index"]
    cache_file = os.path.join(analysis_dir, f"chapter_{idx:03d}.json")

    # 检查缓存
    if Path(cache_file).exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            result = json.load(f)
        return (idx, result)

    # 读取章节文本
    with open(ch["file_path"], "r", encoding="utf-8") as f:
        text = f.read()

    # LLM 分析
    result = llm.analyze_text(text)

    # 保存缓存
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return (idx, result)


def step_llm_analyze(
    chapters: list[dict],
    output_dir: str,
    api_key: str,
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1",
    model: str = "mimo-v2.5",
    chunk_size: int = 2000,
    max_workers: int = 5,
) -> dict:
    """
    用 LLM 并发分析每个章节，结果缓存到本地。
    返回: {chapter_index: {"characters": [...], "segments": [...]}}
    """
    from llm_parser import LLMParser, llm_characters_to_voice_config

    analysis_dir = os.path.join(output_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)

    llm = LLMParser(api_key=api_key, base_url=base_url, model=model, chunk_size=chunk_size)

    # 区分需要分析的和有缓存的
    to_analyze = []
    cached_count = 0
    for ch in chapters:
        cache_file = os.path.join(analysis_dir, f"chapter_{ch['index']:03d}.json")
        if Path(cache_file).exists():
            cached_count += 1
        else:
            to_analyze.append(ch)

    logger.info(f"  缓存: {cached_count} 章, 待分析: {len(to_analyze)} 章, 并发数: {max_workers}")

    # 多线程分析
    all_results = {}
    lock = threading.Lock()
    pbar = tqdm(total=len(chapters), desc="  LLM 分析", unit="章")

    def _on_done(idx, result, is_cached=False):
        with lock:
            all_results[idx] = result
            pbar.update(1)
            if not is_cached:
                char_count = len(result.get("characters", []))
                seg_count = len(result.get("segments", []))
                pbar.set_postfix_str(f"第{idx}章: {char_count}角色 {seg_count}段")

    # 先加载缓存
    for ch in chapters:
        idx = ch["index"]
        cache_file = os.path.join(analysis_dir, f"chapter_{idx:03d}.json")
        if Path(cache_file).exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                result = json.load(f)
            _on_done(idx, result, is_cached=True)

    # 并发分析未缓存的章节
    if to_analyze:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_analyze_one_chapter, ch, analysis_dir, llm): ch
                for ch in to_analyze
            }

            for future in as_completed(futures):
                ch = futures[future]
                try:
                    idx, result = future.result()
                    _on_done(idx, result)
                except Exception as e:
                    logger.error(f"  ❌ 第{ch['index']}章分析失败: {e}")
                    _on_done(ch["index"], {"characters": [], "segments": []})

    pbar.close()

    # 合并全局角色表
    global_characters = {}
    for result in all_results.values():
        for char in result.get("characters", []):
            if char.get("name"):
                global_characters[char["name"]] = char

    # 保存全局角色表
    global_chars_path = os.path.join(output_dir, "characters.json")
    with open(global_chars_path, "w", encoding="utf-8") as f:
        json.dump(list(global_characters.values()), f, ensure_ascii=False, indent=2)

    # 自动生成音色配置
    voice_config = llm_characters_to_voice_config(list(global_characters.values()))
    voice_config["default"] = {
        "mode": "preset",
        "voice_id": "白桦",
        "style_instruction": "用标准的播音腔朗读，语速适中，情感克制",
    }
    voices_path = os.path.join(output_dir, "voices_auto.yaml")
    with open(voices_path, "w", encoding="utf-8") as f:
        yaml.dump(voice_config, f, allow_unicode=True, default_flow_style=False)

    logger.info(f"\n📊 LLM 分析完成:")
    logger.info(f"  识别到 {len(global_characters)} 个角色: {', '.join(global_characters.keys())}")
    logger.info(f"  角色表: {global_chars_path}")
    logger.info(f"  音色配置: {voices_path}")

    return all_results


# ═══════════════════════════════════════════════════════════
# Step 3: TTS 合成每章（多线程）
# ═══════════════════════════════════════════════════════════

def _synthesize_one_chapter(
    ch: dict,
    analysis_results: dict,
    audio_dir: str,
    cache_dir: str,
    config: ProjectConfig,
    voices_path: str,
    voice_hints: dict = None,
) -> tuple[int, str, bool]:
    """
    合成单个章节（供线程池调用）。
    每个线程创建自己的 TTSEngine 实例（OpenAI client 非线程安全）。
    返回: (chapter_index, audio_path, success)
    """
    idx = ch["index"]
    audio_path = os.path.join(audio_dir, f"chapter_{idx:03d}.{config.audio.output_format}")

    # 检查是否已合成
    if Path(audio_path).exists():
        return (idx, audio_path, True)

    # 每个线程独立创建实例（避免线程安全问题）
    voice_mgr = VoiceManager(voices_path)

    # 合并 LLM 的 voice_hints
    if voice_hints:
        for name, hint in voice_hints.items():
            voice_mgr.merge_voice_hint(name, hint)

    tts = TTSEngine(config.tts)
    merger = AudioMerger(
        sample_rate=config.tts.sample_rate,
        output_format=config.audio.output_format,
        bitrate=config.audio.bitrate,
    )

    # 获取该章的分析结果
    analysis = analysis_results.get(idx, {})
    segments_data = analysis.get("segments", [])

    if not segments_data:
        parser = NovelParser(config.parser)
        chapters_parsed = parser.parse_file(ch["file_path"])
        segments = chapters_parsed[0].segments if chapters_parsed else []
    else:
        segments = _dicts_to_segments(segments_data)

    if not segments:
        return (idx, "", False)

    # 合成每个段落
    chapter_cache = os.path.join(cache_dir, f"chapter_{idx:03d}")
    os.makedirs(chapter_cache, exist_ok=True)
    segment_files = []

    for seg_i, seg in enumerate(segments):
        seg_key = f"ch{idx}_seg{seg_i}"
        seg_output = os.path.join(chapter_cache, f"{seg_key}.wav")

        # 检查段落缓存
        if Path(seg_output).exists():
            segment_files.append(seg_output)
            continue

        speaker = seg.speaker or "旁白"
        profile = voice_mgr.get_profile(speaker)
        style = _build_style(seg, profile)

        request = TTSRequest(
            text=seg.text,
            mode=profile.mode,
            voice_id=profile.voice_id,
            voice_prompt=profile.voice_prompt,
            clone_audio_path=profile.clone_audio_path,
            style_instruction=style,
            output_path=seg_output,
        )

        try:
            result = tts.synthesize(request)
            segment_files.append(result)
        except Exception as e:
            logger.error(f"  第{idx}章 seg{seg_i} 合成失败: {e}")
            import soundfile as sf
            silence = merger.create_silence(1000)
            sf.write(seg_output, silence, samplerate=config.tts.sample_rate)
            segment_files.append(seg_output)

    # 拼接章节音频
    if segment_files:
        merger.merge_files(
            segment_files,
            audio_path,
            silence_ms=config.audio.silence_between_paragraphs_ms,
            chapter_silence_ms=config.audio.silence_between_chapters_ms,
        )
        # 删除分段音频，只保留拼接结果
        for f in segment_files:
            if os.path.exists(f):
                os.remove(f)
        if os.path.exists(chapter_cache) and not os.listdir(chapter_cache):
            os.rmdir(chapter_cache)
        return (idx, audio_path, True)

    return (idx, "", False)


def _load_cached_analysis(chapters: list[dict], output_dir: str) -> dict:
    """从缓存加载 LLM 分析结果"""
    import json
    analysis_dir = os.path.join(output_dir, "analysis")
    if not os.path.exists(analysis_dir):
        return {}

    results = {}
    for ch in chapters:
        idx = ch["index"]
        cache_file = os.path.join(analysis_dir, f"chapter_{idx:03d}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    results[idx] = json.load(f)
            except Exception as e:
                logger.warning(f"  加载第{idx}章分析缓存失败: {e}")

    return results


def _load_chapters_from_dir(chapters_dir: str) -> list[dict]:
    """从已有的 chapters 目录读取章节列表"""
    import re
    chapters = []
    for filename in sorted(os.listdir(chapters_dir)):
        if not filename.endswith(".txt"):
            continue
        # 解析文件名：第001章_xxx.txt
        match = re.match(r"第(\d+)章_(.+)\.txt", filename)
        if not match:
            continue
        idx = int(match.group(1))
        title = match.group(2)
        filepath = os.path.join(chapters_dir, filename)
        word_count = os.path.getsize(filepath)
        chapters.append({
            "index": idx,
            "title": title,
            "file_path": filepath,
            "word_count": word_count,
        })
    return chapters


def step_tts_synthesize(
    chapters: list[dict],
    analysis_results: dict,
    output_dir: str,
    config: ProjectConfig,
    voices_path: str,
    resume_from: int = 0,
    max_workers: int = 3,
):
    """
    多线程合成所有章节。
    每个章节独立合成，章节内部的段落按顺序处理。
    """
    audio_dir = os.path.join(output_dir, "audio")
    cache_dir = os.path.join(output_dir, ".cache")
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    # 从 LLM 分析结果中提取 voice_hints
    voice_hints = {}
    for result in analysis_results.values():
        for char in result.get("characters", []):
            if char.get("name") and char.get("voice_hint"):
                voice_hints[char["name"]] = char["voice_hint"]

    if voice_hints:
        logger.info(f"  LLM 音色提示: {', '.join(f'{k}({v})' for k, v in voice_hints.items())}")

    # 过滤需要合成的章节
    to_synthesize = []
    skipped = 0
    for ch in chapters:
        idx = ch["index"]
        if idx < resume_from:
            skipped += 1
            continue
        audio_path = os.path.join(audio_dir, f"chapter_{idx:03d}.{config.audio.output_format}")
        if Path(audio_path).exists():
            skipped += 1
            continue
        to_synthesize.append(ch)

    logger.info(f"  待合成: {len(to_synthesize)} 章, 跳过: {skipped} 章, 并发数: {max_workers}")

    if not to_synthesize:
        logger.info("  所有章节已合成完成！")
        return

    # 多线程合成
    pbar = tqdm(total=len(to_synthesize), desc="  TTS 合成", unit="章")
    completed = 0
    failed = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _synthesize_one_chapter,
                ch, analysis_results, audio_dir, cache_dir, config, voices_path, voice_hints,
            ): ch
            for ch in to_synthesize
        }

        for future in as_completed(futures):
            ch = futures[future]
            try:
                idx, audio_path, success = future.result()
                with lock:
                    pbar.update(1)
                    if success and audio_path:
                        completed += 1
                        pbar.set_postfix_str(f"✅ 第{idx}章完成")
                    else:
                        failed += 1
                        pbar.set_postfix_str(f"⚠️ 第{idx}章无内容")
            except Exception as e:
                with lock:
                    pbar.update(1)
                    failed += 1
                    pbar.set_postfix_str(f"❌ 第{ch['index']}章失败")
                logger.error(f"  ❌ 第{ch['index']}章合成异常: {e}")

    pbar.close()

    logger.info(f"\n🎉 合成完成!")
    logger.info(f"  成功: {completed} 章, 失败: {failed} 章")
    logger.info(f"  音频目录: {audio_dir}/")


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════
def _build_style(segment, voice_profile) -> str:
    """构建风格指令：音色基础 + 情感 + 段落类型"""
    parts = []

    # 音色基础描述（来自 voice_manager 的自动推断或 YAML 配置）
    if voice_profile.style_instruction:
        parts.append(voice_profile.style_instruction)

    # 情感指令
    emotion_map = {
        "喜": "用欢快愉悦的语气说", "怒": "用愤怒激动的语气说",
        "哀": "用悲伤低沉的语气说", "惧": "用恐惧颤抖的语气说",
        "惊": "用惊讶震惊的语气说", "急": "用急促紧张的语气说",
        "平静": "用平静从容的语气说", "冷漠": "用冷淡疏离的语气说",
        "温柔": "用温柔轻声的语气说",
    }
    if segment.emotion and segment.emotion in emotion_map:
        parts.append(emotion_map[segment.emotion])

    # 段落类型
    if segment.type == SegmentType.NARRATION:
        parts.append("用叙述的语气朗读")
    elif segment.type == SegmentType.DIALOGUE:
        parts.append("用对话的语气说")

    return "，".join(parts) if parts else ""


def _dicts_to_segments(dicts: list) -> list[Segment]:
    type_map = {
        "narration": SegmentType.NARRATION,
        "dialogue": SegmentType.DIALOGUE,
        "thought": SegmentType.THOUGHT,
    }
    return [
        Segment(
            text=d.get("text", ""),
            original=d.get("text", ""),
            type=type_map.get(d.get("type", "narration"), SegmentType.NARRATION),
            speaker=d.get("speaker"),
            emotion=d.get("emotion"),
        )
        for d in dicts
    ]


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="小说转有声书 — 基于小米 MiMo-V2.5-TTS 的多角色配音",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
工作流:
  novel (txt/epub) → 按章切割 → LLM 分析(并发) → TTS 合成(并发) → 每章一个音频

示例:
  # EPUB 一键转换
  python main.py novel.epub

  # 8 线程 LLM 分析 + 4 线程 TTS 合成
  python main.py novel.epub --llm-workers 8 --tts-workers 4

  # 只切割 + LLM 分析
  python main.py novel.epub --llm-only

  # 用已有分析结果合成
  python main.py novel.epub --skip-llm

  # 从第 50 章恢复
  python main.py novel.epub --skip-llm --resume-from 50
        """,
    )
    parser.add_argument("novel", nargs="?", help="小说文件路径 (.txt / .epub)，--skip-llm 时可省略")

    # 输出选项
    parser.add_argument("--output", "-o", default=None, help="输出目录（默认: 小说同目录/audiobook/）")
    parser.add_argument("--format", "-f", default="mp3", choices=["mp3", "wav", "m4b"], help="输出格式")
    parser.add_argument("--voices", "-v", default=None, help="角色音色配置 (YAML)")

    # 流程控制
    flow_group = parser.add_argument_group("流程控制")
    flow_group.add_argument("--use-llm", action="store_true", help="使用 LLM 分析")
    flow_group.add_argument("--llm-only", action="store_true", help="只切割 + 分析，不合成")
    flow_group.add_argument("--skip-llm", action="store_true", help="跳过 LLM，用规则解析")
    flow_group.add_argument("--resume-from", type=int, default=0, help="从第 N 章恢复")
    flow_group.add_argument("--start-chapter", type=int, default=1, help="起始章节编号 (默认: 1)")
    flow_group.add_argument("--end-chapter", type=int, default=0, help="结束章节编号 (默认: 0 表示全部)")

    # 并发控制
    perf_group = parser.add_argument_group("并发控制")
    perf_group.add_argument("--llm-workers", type=int, default=5, help="LLM 分析并发数 (默认: 5)")
    perf_group.add_argument("--tts-workers", type=int, default=3, help="TTS 合成并发数 (默认: 3)")

    # LLM 选项
    llm_group = parser.add_argument_group("LLM 选项")
    llm_group.add_argument("--llm-model", default="mimo-v2.5", help="LLM 模型 (默认: mimo-v2.5)")
    llm_group.add_argument("--llm-base-url", default=os.environ.get("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"), help="LLM API 地址")
    llm_group.add_argument("--llm-api-key", default=None, help="LLM API Key (默认从 .env 读取)")
    llm_group.add_argument("--llm-chunk-size", type=int, default=2000, help="LLM 文本块大小")

    # 其他
    parser.add_argument("--api-key", help="MiMo API Key (或设置 MIMO_API_KEY)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = parser.parse_args()
    setup_logging(args.log_level)

    # ── 验证输入 ──────────────────────────────────────────
    if args.novel:
        if not Path(args.novel).exists():
            logger.error(f"文件不存在: {args.novel}")
            sys.exit(1)

        ext = Path(args.novel).suffix.lower()
        if ext not in (".txt", ".epub"):
            logger.error(f"不支持的格式: {ext}（支持 .txt / .epub）")
            sys.exit(1)
    else:
        if not args.skip_llm:
            logger.error("未指定小说文件，请提供 .epub 或 .txt 文件")
            sys.exit(1)
        ext = None

    api_key = args.api_key or os.environ.get("TTS_API_KEY", os.environ.get("MIMO_API_KEY", ""))
    output_dir = args.output or (str(Path(args.novel).parent / "audiobook") if args.novel else "output")

    # EPUB 默认启用 LLM
    if not args.use_llm and not args.llm_only and not args.skip_llm:
        if ext == ".epub":
            args.use_llm = True
            logger.info("📖 EPUB 文件，自动启用 LLM 分析")

    # LLM 使用独立的 key，不依赖 TTS key
    # TTS key 仅在合成阶段检查

    # ── Step 1: 解析小说 ──────────────────────────────────
    logger.info(f"\n{'═'*60}")
    logger.info("📖 Step 1: 解析小说文件")
    logger.info(f"{'═'*60}")

    if args.skip_llm:
        # --skip-llm 模式：从已有的 chapters 目录读取章节列表
        chapters_dir = os.path.join(output_dir, "chapters")
        if os.path.exists(chapters_dir):
            chapters = _load_chapters_from_dir(chapters_dir)
            logger.info(f"  从缓存加载 {len(chapters)} 章")
        elif args.novel:
            chapters = step_parse_novel(args.novel, output_dir)
        else:
            logger.error("未找到 chapters 目录，请先运行切割")
            sys.exit(1)
    else:
        chapters = step_parse_novel(args.novel, output_dir)

    total_words = sum(ch["word_count"] for ch in chapters)
    logger.info(f"\n✅ 切割完成: {len(chapters)} 章, {total_words:,} 字")

    # ── 章节范围过滤 ──────────────────────────────────────
    start = max(args.start_chapter, 1)
    end = args.end_chapter if args.end_chapter > 0 else len(chapters)

    if start > 1 or end < len(chapters):
        chapters = [ch for ch in chapters if start <= ch["index"] <= end]
        logger.info(f"📌 章节范围: 第 {start} ~ {end} 章, 共 {len(chapters)} 章")

    if not chapters:
        logger.error(f"指定范围内无章节: {start}-{end}")
        sys.exit(1)

    # ── Step 2: LLM 分析 ──────────────────────────────────
    analysis_results = {}

    if args.use_llm or args.llm_only:
        llm_api_key = args.llm_api_key or os.environ.get("LLM_API_KEY", "")

        logger.info(f"\n{'═'*60}")
        logger.info(f"🤖 Step 2: LLM 分析 (并发: {args.llm_workers})")
        logger.info(f"  模型: {args.llm_model} @ {args.llm_base_url}")
        logger.info(f"{'═'*60}")

        analysis_results = step_llm_analyze(
            chapters=chapters,
            output_dir=output_dir,
            api_key=llm_api_key,
            base_url=args.llm_base_url,
            model=args.llm_model,
            chunk_size=args.llm_chunk_size,
            max_workers=args.llm_workers,
        )

        if args.llm_only:
            logger.info(f"\n✅ LLM 分析完成！")
            logger.info(f"📁 输出: {output_dir}/")
            logger.info(f"  chapters/       — 按章切割的 txt")
            logger.info(f"  analysis/       — LLM 分析缓存")
            logger.info(f"  characters.json — 角色表")
            logger.info(f"  voices_auto.yaml — 自动音色配置")
            logger.info(f"\n💡 下一步:")
            logger.info(f"  1. 编辑 {output_dir}/voices_auto.yaml 调整音色")
            logger.info(f"  2. python main.py {args.novel} --skip-llm --voices {output_dir}/voices_auto.yaml")
            return

    elif args.skip_llm:
        # 从缓存加载分析结果
        analysis_results = _load_cached_analysis(chapters, output_dir)
        if analysis_results:
            logger.info(f"  从缓存加载 {len(analysis_results)} 章分析结果")
        else:
            logger.warning("  未找到缓存的分析结果，TTS 将使用纯文本模式")

    # ── Step 3: TTS 合成 ──────────────────────────────────
    if not api_key:
        logger.error("TTS 合成需要 API Key: 在 .env 中设置 TTS_API_KEY 或使用 --api-key")
        sys.exit(1)

    logger.info(f"\n{'═'*60}")
    logger.info(f"🔊 Step 3: TTS 合成 (并发: {args.tts_workers})")
    logger.info(f"{'═'*60}")

    voices_path = args.voices
    if not voices_path:
        auto_voices = os.path.join(output_dir, "voices_auto.yaml")
        if Path(auto_voices).exists():
            voices_path = auto_voices
            logger.info(f"  使用自动音色配置: {voices_path}")
        else:
            voices_path = "voices.yaml"

    config = ProjectConfig(
        tts=TTSConfig(api_key=api_key),
        audio=AudioConfig(output_format=args.format),
        output_dir=output_dir,
    )

    step_tts_synthesize(
        chapters=chapters,
        analysis_results=analysis_results,
        output_dir=output_dir,
        config=config,
        voices_path=voices_path,
        resume_from=args.resume_from,
        max_workers=args.tts_workers,
    )


if __name__ == "__main__":
    main()
