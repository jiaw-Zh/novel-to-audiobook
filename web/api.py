"""
小说有声书 Web API
基于 FastAPI，对接现有的 novel-to-audiobook 管线
"""
import os
import sys
import json
import uuid
import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from epub_parser import parse_epub
from parser import NovelParser
from llm_parser import LLMParser, llm_characters_to_voice_config
from tts_engine import TTSEngine, TTSRequest
from voice_manager import VoiceManager
from audio_merger import AudioMerger, get_ffmpeg_executable
from config import TTSConfig, LLMConfig, AudioConfig

app = FastAPI(title="小说有声书 API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 数据目录 ──────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

# ── 全局状态 ──────────────────────────────────────────
# 存储正在处理的任务状态
tasks: dict[str, dict] = {}


def start_background_job(task_id: str, coro):
    async def runner():
        try:
            await coro
        except Exception as e:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = str(e)
            print(f"后台任务 {task_id} 失败: {e}")

    asyncio.create_task(runner())


# ── Pydantic Models ──────────────────────────────────
class VoiceConfig(BaseModel):
    mode: str = "preset"  # preset / voicedesign / clone
    voice_id: Optional[str] = None
    voice_prompt: Optional[str] = None
    style_instruction: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None


class VoicesUpdate(BaseModel):
    default: VoiceConfig
    characters: dict[str, VoiceConfig]


class AnalyzeRequest(BaseModel):
    chapter_ids: list[int] = []
    llm_workers: int = 5


class SynthesizeRequest(BaseModel):
    chapter_ids: list[int] = []
    tts_workers: int = 3


class ProjectConfig(BaseModel):
    llm_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    llm_api_key: str = ""
    llm_model: str = "mimo-v2.5"
    tts_base_url: str = "https://api.xiaomimimo.com/v1"
    tts_api_key: str = ""
    tts_model: str = "mimo-v2.5-tts"


# ── 工具函数 ──────────────────────────────────────────
def get_project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def load_chapters(project_id: str) -> list[dict]:
    project_dir = get_project_dir(project_id)
    chapters_file = project_dir / "chapters.json"
    if chapters_file.exists():
        return json.loads(chapters_file.read_text(encoding="utf-8"))
    return []


def save_chapters(project_id: str, chapters: list[dict]):
    project_dir = get_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "chapters.json").write_text(
        json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_characters(project_id: str) -> list[dict]:
    project_dir = get_project_dir(project_id)
    chars_file = project_dir / "characters.json"
    if chars_file.exists():
        return json.loads(chars_file.read_text(encoding="utf-8"))
    return []


def save_characters(project_id: str, characters: list[dict]):
    project_dir = get_project_dir(project_id)
    (project_dir / "characters.json").write_text(
        json.dumps(characters, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_analysis(project_id: str, chapter_id: int) -> Optional[dict]:
    project_dir = get_project_dir(project_id)
    analysis_file = project_dir / "analysis" / f"chapter_{chapter_id:03d}.json"
    if analysis_file.exists():
        return json.loads(analysis_file.read_text(encoding="utf-8"))
    return None


def save_analysis(project_id: str, chapter_id: int, analysis: dict):
    project_dir = get_project_dir(project_id)
    analysis_dir = project_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"chapter_{chapter_id:03d}.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_voices_config(project_id: str) -> dict:
    project_dir = get_project_dir(project_id)
    voices_file = project_dir / "voices.yaml"
    if voices_file.exists():
        import yaml
        return yaml.safe_load(voices_file.read_text(encoding="utf-8"))
    return {
        "default": {"mode": "preset", "voice_id": "白桦", "style_instruction": "标准播音腔朗读"},
        "characters": {}
    }


def save_voices_config(project_id: str, config: dict):
    project_dir = get_project_dir(project_id)
    import yaml
    (project_dir / "voices.yaml").write_text(
        yaml.dump(config, allow_unicode=True, default_flow_style=False),
        encoding="utf-8"
    )


def make_default_voices_config(characters: list[dict]) -> dict:
    config = llm_characters_to_voice_config(characters)
    config["default"] = {
        "mode": "preset",
        "voice_id": "白桦",
        "style_instruction": "用标准的播音腔朗读，语速适中，情感克制",
    }
    return config


def get_analysis_segments(analysis: dict) -> list[dict]:
    return analysis.get("segments") or analysis.get("paragraphs") or []


def analyze_chapter_sync(content: str, config: dict) -> dict:
    llm_parser = LLMParser(
        api_key=config["llm_api_key"],
        base_url=config["llm_base_url"],
        model=config["llm_model"],
    )
    return llm_parser.analyze_text(content)


def synthesize_chapter_sync(
    project_dir: Path,
    ch_id: int,
    analysis: dict,
    voices_config: dict,
    config: dict,
) -> Path:
    tts_config = TTSConfig(
        api_key=config["tts_api_key"],
        base_url=config["tts_base_url"],
        model_preset=config["tts_model"],
    )
    tts_engine = TTSEngine(tts_config)
    audio_merger = AudioMerger(output_format="mp3")

    cache_dir = project_dir / ".cache" / f"chapter_{ch_id:03d}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    segment_files = []

    for seg_i, para in enumerate(get_analysis_segments(analysis)):
        speaker = para.get("speaker") or "旁白"
        text = para.get("text", "")
        if not text.strip():
            continue

        voice_config = voices_config.get("characters", {}).get(speaker) or voices_config.get("default", {})
        mode = voice_config.get("mode") or "preset"
        voice_id = voice_config.get("voice_id") or "白桦"
        voice_prompt = voice_config.get("voice_prompt") or ""
        style = voice_config.get("style_instruction") or ""

        voice_hint = para.get("voice_hint") or ""
        if voice_hint and style and voice_hint not in style:
            style = f"{style}，{voice_hint}"
        elif voice_hint:
            style = voice_hint

        segment_path = cache_dir / f"seg_{seg_i:04d}.wav"
        result = tts_engine.synthesize(TTSRequest(
            text=text,
            mode=mode,
            voice_id=voice_id,
            voice_prompt=voice_prompt,
            style_instruction=style,
            output_path=str(segment_path),
        ))
        segment_files.append(result)

    if not segment_files:
        raise RuntimeError("章节没有可合成的文本段落")

    audio_dir = project_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    output_path = audio_dir / f"chapter_{ch_id:03d}.mp3"
    final_path = Path(audio_merger.merge_files(segment_files, str(output_path)))
    if final_path.suffix.lower() != ".mp3":
        raise RuntimeError("MP3 转换失败，请确认 ffmpeg 已安装并在 PATH 中")

    for file_path in segment_files:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        cache_dir.rmdir()
    except OSError:
        pass

    return output_path


def ensure_chapter_mp3(project_dir: Path, chapter_id: int) -> Path:
    audio_dir = project_dir / "audio"
    mp3_path = audio_dir / f"chapter_{chapter_id:03d}.mp3"
    if mp3_path.exists():
        return mp3_path

    wav_path = audio_dir / f"chapter_{chapter_id:03d}.wav"
    if not wav_path.exists():
        raise FileNotFoundError("章节音频不存在")

    ffmpeg = get_ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法导出 MP3")

    try:
        subprocess.run(
            [
                ffmpeg, "-y",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame",
                "-b:a", "128k",
                str(mp3_path),
            ],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="ignore") if exc.stderr else str(exc)
        raise RuntimeError(f"ffmpeg 转换 MP3 失败: {detail}") from exc

    return mp3_path


def load_project_meta(project_id: str) -> Optional[dict]:
    """Load project metadata from meta.json"""
    project_dir = get_project_dir(project_id)
    meta_file = project_dir / "meta.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text(encoding="utf-8"))
    return None


def save_project_meta(project_id: str, meta: dict):
    """Save project metadata to meta.json"""
    project_dir = get_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_config() -> dict:
    """从 .env 或环境变量读取配置"""
    from dotenv import load_dotenv
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    return {
        "llm_base_url": os.environ.get("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"),
        "llm_api_key": os.environ.get("LLM_API_KEY", ""),
        "llm_model": os.environ.get("LLM_MODEL", "mimo-v2.5"),
        "tts_base_url": os.environ.get("TTS_BASE_URL", "https://api.xiaomimimo.com/v1"),
        "tts_api_key": os.environ.get("TTS_API_KEY", ""),
        "tts_model": os.environ.get("TTS_MODEL", "mimo-v2.5-tts"),
    }


# ── API 路由 ──────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """获取系统状态"""
    config = get_config()
    return {
        "config": {
            **config,
            "llm_api_key": bool(config["llm_api_key"]),
            "tts_api_key": bool(config["tts_api_key"]),
        },
    }


@app.get("/api/projects")
async def list_projects():
    """列出所有项目"""
    projects = []
    if PROJECTS_DIR.exists():
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if project_dir.is_dir():
                meta = load_project_meta(project_dir.name)
                if meta:
                    projects.append(meta)
    # Sort by created_at descending
    projects.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return {"projects": projects}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传 EPUB 或 TXT 文件"""
    if not file.filename:
        raise HTTPException(400, "未选择文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in (".epub", ".txt"):
        raise HTTPException(400, f"不支持的格式: {ext}（支持 .epub / .txt）")

    # 保存上传文件
    project_id = str(uuid.uuid4())[:8]
    project_dir = get_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    upload_path = UPLOADS_DIR / f"{project_id}{ext}"
    with open(upload_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 解析章节
    chapters_data = []  # list of (title, text)
    if ext == ".epub":
        book = parse_epub(str(upload_path))
        for ch in book.chapters:
            chapters_data.append((ch.title, ch.text))
    else:
        parser = NovelParser()
        with open(upload_path, "r", encoding="utf-8") as f:
            text = f.read()
        parsed_chapters = parser.parse_text(text)
        for ch in parsed_chapters:
            raw = getattr(ch, "_raw_text", "")
            chapters_data.append((ch.title, raw))

    # 保存章节文本文件
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)

    chapter_list = []
    for i, (title, text) in enumerate(chapters_data):
        ch_id = i + 1
        # 保存章节文本
        ch_file = chapters_dir / f"chapter_{ch_id:03d}.txt"
        ch_file.write_text(text, encoding="utf-8")

        chapter_list.append({
            "id": ch_id,
            "title": title or f"第{ch_id}章",
            "word_count": len(text),
            "status": "pending",  # pending / analyzing / analyzed / synthesizing / done / error
            "has_analysis": False,
            "has_audio": False,
        })

    save_chapters(project_id, chapter_list)

    # 保存项目元数据
    total_words = sum(ch["word_count"] for ch in chapter_list)
    meta = {
        "id": project_id,
        "name": file.filename,
        "created_at": datetime.now().isoformat(),
        "chapter_count": len(chapter_list),
        "total_words": total_words,
    }
    save_project_meta(project_id, meta)

    return {
        "project_id": project_id,
        "filename": file.filename,
        "chapter_count": len(chapter_list),
        "total_words": total_words,
    }


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目"""
    project_dir = get_project_dir(project_id)
    if not project_dir.exists():
        raise HTTPException(404, "项目不存在")
    shutil.rmtree(project_dir)
    return {"success": True, "deleted_project_id": project_id}


@app.get("/api/chapters")
async def list_chapters(project_id: str = Query(...)):
    """获取章节列表"""
    chapters = load_chapters(project_id)
    if not chapters:
        raise HTTPException(404, "项目不存在")
    return {"chapters": chapters}


@app.get("/api/chapters/{chapter_id}")
async def get_chapter(project_id: str, chapter_id: int):
    """获取章节内容"""
    project_dir = get_project_dir(project_id)
    ch_file = project_dir / "chapters" / f"chapter_{chapter_id:03d}.txt"
    if not ch_file.exists():
        raise HTTPException(404, "章节不存在")

    content = ch_file.read_text(encoding="utf-8")
    analysis = load_analysis(project_id, chapter_id)

    return {
        "id": chapter_id,
        "content": content,
        "analysis": analysis,
    }


@app.post("/api/analyze")
async def analyze_chapters(
    request: AnalyzeRequest,
    project_id: str = Query(..., description="项目 ID"),
):
    """LLM 分析章节"""
    chapters = load_chapters(project_id)
    if not chapters:
        raise HTTPException(400, "无章节数据")

    # 筛选待分析的章节
    chapter_ids = request.chapter_ids
    if not chapter_ids:
        chapter_ids = [ch["id"] for ch in chapters if ch["status"] == "pending"]

    if not chapter_ids:
        raise HTTPException(400, "无待分析章节")

    # 标记为分析中
    for ch in chapters:
        if ch["id"] in chapter_ids:
            ch["status"] = "analyzing"
    save_chapters(project_id, chapters)

    # 后台执行分析
    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {"status": "running", "progress": 0, "total": len(chapter_ids)}

    start_background_job(
        task_id,
        run_analysis(project_id, chapter_ids, request.llm_workers, task_id),
    )

    return {"task_id": task_id, "chapter_count": len(chapter_ids)}


async def run_analysis(project_id: str, chapter_ids: list[int], max_workers: int, task_id: str):
    """后台执行 LLM 分析"""
    config = get_config()
    project_dir = get_project_dir(project_id)

    chapters = load_chapters(project_id)
    all_characters = load_characters(project_id)
    completed = 0

    async def analyze_one(ch_id: int):
        nonlocal completed
        ch_file = project_dir / "chapters" / f"chapter_{ch_id:03d}.txt"
        content = ch_file.read_text(encoding="utf-8")

        try:
            result = await asyncio.to_thread(analyze_chapter_sync, content, config)
            save_analysis(project_id, ch_id, result)

            # 更新角色表
            for char in result.get("characters", []):
                if char.get("name") and not any(c.get("name") == char["name"] for c in all_characters):
                    all_characters.append(char)

            # 更新章节状态
            for ch in chapters:
                if ch["id"] == ch_id:
                    ch["status"] = "analyzed"
                    ch["has_analysis"] = True
                    break

        except Exception as e:
            for ch in chapters:
                if ch["id"] == ch_id:
                    ch["status"] = "error"
                    break
            print(f"分析第 {ch_id} 章失败: {e}")

        completed += 1
        tasks[task_id]["progress"] = completed

    # 并发执行
    semaphore = asyncio.Semaphore(max_workers)

    async def limited_analyze(ch_id):
        async with semaphore:
            await analyze_one(ch_id)

    await asyncio.gather(*[limited_analyze(ch_id) for ch_id in chapter_ids])

    # 保存结果
    save_chapters(project_id, chapters)
    save_characters(project_id, all_characters)

    # 自动生成音色配置
    auto_voices = make_default_voices_config(all_characters)
    save_voices_config(project_id, auto_voices)

    tasks[task_id]["status"] = "completed"


@app.get("/api/characters")
async def list_characters(project_id: str):
    """获取角色列表"""
    characters = load_characters(project_id)
    return {"characters": characters}


@app.get("/api/voices")
async def get_voices(project_id: str):
    """获取音色配置"""
    config = load_voices_config(project_id)
    return {"voices": config}


@app.put("/api/voices")
async def update_voices(project_id: str, update: VoicesUpdate):
    """更新音色配置"""
    config = {
        "default": update.default.dict(),
        "characters": {name: vc.dict() for name, vc in update.characters.items()}
    }
    save_voices_config(project_id, config)
    return {"success": True}


@app.post("/api/synthesize")
async def synthesize_chapters(
    request: SynthesizeRequest,
    project_id: str = Query(..., description="项目 ID"),
):
    """TTS 合成章节"""
    chapters = load_chapters(project_id)
    voices_config = load_voices_config(project_id)

    # 筛选待合成的章节
    chapter_ids = request.chapter_ids
    if not chapter_ids:
        chapter_ids = [ch["id"] for ch in chapters if ch["has_analysis"] and not ch["has_audio"]]

    if not chapter_ids:
        raise HTTPException(400, "无可合成章节（需要先完成 LLM 分析）")

    # 标记为合成中
    for ch in chapters:
        if ch["id"] in chapter_ids:
            ch["status"] = "synthesizing"
    save_chapters(project_id, chapters)

    # 后台执行合成
    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {"status": "running", "progress": 0, "total": len(chapter_ids)}

    start_background_job(
        task_id,
        run_synthesis(project_id, chapter_ids, request.tts_workers, task_id),
    )

    return {"task_id": task_id, "chapter_count": len(chapter_ids)}


async def run_synthesis(project_id: str, chapter_ids: list[int], max_workers: int, task_id: str):
    """后台执行 TTS 合成"""
    config = get_config()
    project_dir = get_project_dir(project_id)
    voices_config = load_voices_config(project_id)

    chapters = load_chapters(project_id)
    completed = 0

    async def synthesize_one(ch_id: int):
        nonlocal completed
        analysis = load_analysis(project_id, ch_id)
        if not analysis:
            return

        try:
            await asyncio.to_thread(
                synthesize_chapter_sync,
                project_dir,
                ch_id,
                analysis,
                voices_config,
                config,
            )

            # 更新状态
            for ch in chapters:
                if ch["id"] == ch_id:
                    ch["status"] = "done"
                    ch["has_audio"] = True
                    break

        except Exception as e:
            for ch in chapters:
                if ch["id"] == ch_id:
                    ch["status"] = "error"
                    break
            print(f"合成第 {ch_id} 章失败: {e}")

        completed += 1
        tasks[task_id]["progress"] = completed

    # 并发执行
    semaphore = asyncio.Semaphore(max_workers)

    async def limited_synthesize(ch_id):
        async with semaphore:
            await synthesize_one(ch_id)

    await asyncio.gather(*[limited_synthesize(ch_id) for ch_id in chapter_ids])

    save_chapters(project_id, chapters)
    tasks[task_id]["status"] = "completed"


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态"""
    if task_id not in tasks:
        raise HTTPException(404, "任务不存在")
    return tasks[task_id]


@app.get("/api/audio/{chapter_id}")
async def get_audio(project_id: str, chapter_id: int):
    """获取章节音频"""
    project_dir = get_project_dir(project_id)
    try:
        audio_path = ensure_chapter_mp3(project_dir, chapter_id)
    except FileNotFoundError:
        raise HTTPException(404, "音频不存在")
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return FileResponse(audio_path, media_type="audio/mpeg")


@app.get("/api/export")
async def export_audio(project_id: str, chapter_id: Optional[int] = None):
    """导出单章 MP3 或全部音频 ZIP"""
    import zipfile

    project_dir = get_project_dir(project_id)
    audio_dir = project_dir / "audio"
    if not audio_dir.exists():
        raise HTTPException(400, "无音频可导出")

    if chapter_id is not None:
        try:
            audio_path = ensure_chapter_mp3(project_dir, chapter_id)
        except FileNotFoundError:
            raise HTTPException(404, "章节 MP3 不存在")
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        return FileResponse(
            audio_path,
            media_type="audio/mpeg",
            filename=f"chapter_{chapter_id:03d}.mp3",
        )

    # 创建 ZIP
    zip_path = project_dir / "export.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for audio_file in sorted(audio_dir.glob("*.mp3")):
            zf.write(audio_file, audio_file.name)

    return FileResponse(zip_path, media_type="application/zip", filename="audiobook.zip")


# ── 静态文件服务 ──────────────────────────────────────
WEB_DIR = Path(__file__).parent

@app.get("/")
async def serve_index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/{project_id}")
async def serve_project(project_id: str):
    project_dir = get_project_dir(project_id)
    if not project_dir.exists():
        raise HTTPException(404, "项目不存在")
    return FileResponse(WEB_DIR / "index.html")


# 挂载静态资源
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8134)
