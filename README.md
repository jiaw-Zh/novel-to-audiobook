# 小说转有声书 — 基于小米 MiMo TTS 的多角色配音

将小说（EPUB/TXT）自动转为多角色有声书。LLM 分析角色和对话，MiMo TTS 为每个角色分配不同音色和语气。

## 工作流

```
novel.epub / novel.txt
    │
    ▼
┌──────────────────────┐
│ Step 1: 按章切割      │  → chapters/第001章_xxx.txt ...
│ (单线程，秒级完成)    │
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│ Step 2: LLM 分析      │  → analysis/chapter_001.json ...
│ (多线程，默认 5 并发) │  → characters.json (全局角色表)
│                      │  → voices_auto.yaml (自动音色配置)
└────────┬─────────────┘
         │
         ▼
┌──────────────────────┐
│ Step 3: TTS 合成      │  → audio/chapter_001.mp3 ...
│ (多线程，默认 3 并发) │  (分段音频自动清理，只保留拼接结果)
└──────────────────────┘
```

## 支持格式

| 格式 | 说明 |
|------|------|
| `.epub` | 电子书（自动启用 LLM 分析） |
| `.txt` | 纯文本（默认规则解析，可用 `--use-llm` 启用 LLM） |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

复制模板并填入密钥：

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
# TTS 配置
TTS_API_KEY=your_tts_api_key
TTS_BASE_URL=https://api.xiaomimimo.com/v1
TTS_MODEL=mimo-v2.5-tts

# LLM 配置
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
LLM_MODEL=mimo-v2.5
```

### 3. 运行

```bash
# EPUB 一键转换（自动切割 + LLM 分析 + TTS 合成）
python main.py novel.epub

# TXT + LLM 分析
python main.py novel.txt --use-llm

# 只切割 + LLM 分析（不合成，先看分析结果）
python main.py novel.epub --llm-only
```

## 三种使用模式

### 模式 1: 一键完成

```bash
python main.py novel.epub
# 自动: 切割 → LLM 分析(5线程) → TTS 合成(3线程) → audio/chapter_001.mp3 ...
```

### 模式 2: 分步执行（推荐）

```bash
# Step 1+2: 切割 + LLM 分析（不合成）
python main.py novel.epub --llm-only

# 查看分析结果
cat output/characters.json
cat output/voices_auto.yaml

# 手动调整音色配置
vim output/voices_auto.yaml

# Step 3: 用调整后的配置合成（自动从缓存加载分析结果）
python main.py novel.epub --skip-llm --voices output/voices_auto.yaml
```

### 模式 3: 断点续传

```bash
# 从第 50 章恢复合成
python main.py novel.epub --skip-llm --resume-from 50
```

### 模式 4: 使用已有缓存直接合成

当 `chapters/` 和 `analysis/` 目录已存在时，无需原始小说文件，直接合成音频：

```bash
# 自动从当前 output/ 目录加载 chapters + analysis 缓存
python main.py --skip-llm

# 指定缓存目录（analysis/ 和 chapters/ 的父目录）
python main.py --skip-llm --output /path/to/audiobook

# 指定章节范围
python main.py --skip-llm --start-chapter 5 --end-chapter 10
```

**缓存目录结构要求：**

```
output/  (或 --output 指定的目录)
├── chapters/          # 已切割的章节 txt（_load_chapters_from_dir 读取）
│   ├── 第001章_xxx.txt
│   └── ...
└── analysis/          # LLM 分析结果 JSON（_load_cached_analysis 读取）
    ├── chapter_001.json
    └── ...
```

> ⚠️ 两个目录缺一不可：没有 `chapters/` 会报错，没有 `analysis/` 则 TTS 使用纯文本模式（无角色/语气信息）。

### 模式 5: 分析指定章节

```bash
# 只分析第 6-10 章（适合先验证效果再跑全量）
python main.py novel.epub --llm-only --start-chapter 6 --end-chapter 10

# 分析第 100 章到末尾
python main.py novel.epub --llm-only --start-chapter 100
```

## 音色与角色

### 自动音色分配策略

系统根据 LLM 识别的角色性别自动分配音色，并在同一性别的角色间轮换以避免重复：

| 性别 | 可用音色 | 说明 |
|------|----------|------|
| 男性 | 白桦、苏打 | 白桦浑厚磁性，苏打清澈明亮 |
| 女性 | 冰糖、茉莉 | 冰糖温暖亲和，茉莉清甜柔和 |
| 未知 | 白桦 | 默认旁白音色 |

**同性别角色自动轮换**：比如 3 个男性角色 → 白桦、苏打、白桦。

### style_instruction — 同音色不同个性

每个角色除了音色分配，还有独立的 `style_instruction` 控制语气风格。这让同一个音色演绎不同角色时也能听出区别：

```yaml
characters:
  陈新:
    voice_id: 白桦
    style_instruction: 年轻男性，声音从容淡定，语速平稳，带一点自信
  药农:
    voice_id: 白桦
    style_instruction: 苍老年迈，声音低沉沙哑，语速缓慢，朴实
```

### 旁白

旁白使用固定音色（白桦），风格为"标准播音腔朗读"，不跟随角色。

### voice_hint 自动合并

LLM 分析时会为每个角色生成 `voice_hint`（如"苍老沙哑，语速缓慢"），合成时自动合并到 style_instruction，无需手动配置。

## 预置音色一览

| 音色 | 性别 | 风格 | 推荐用途 |
|------|------|------|----------|
| 白桦 | 男 | 浑厚磁性，成熟稳重 | 旁白、成熟男性主角 |
| 苏打 | 男 | 清澈明亮，年轻活力 | 年轻男性角色 |
| 冰糖 | 女 | 温暖亲和，甜美柔和 | 女性角色 |
| 茉莉 | 女 | 清甜柔和，优雅知性 | 女性角色 |
| Mia | 女 | — | 备选 |
| Chloe | 女 | — | 备选 |
| Milo | 男 | — | 备选 |
| Dean | 男 | — | 备选 |

## 并发控制

LLM 分析和 TTS 合成均支持多线程并发，每个章节独立处理：

```bash
# 默认并发（LLM 5 + TTS 3）
python main.py novel.epub

# 高并发
python main.py novel.epub --llm-workers 10 --tts-workers 5

# 保守模式（避免触发限速）
python main.py novel.epub --llm-workers 2 --tts-workers 1
```

**500 章小说预估耗时：**

| 模式 | LLM 分析 | TTS 合成 | 总计 |
|------|----------|----------|------|
| 串行 | ~34 小时 | ~250 分钟 | ~38 小时 |
| 默认并发 (5+3) | ~7 小时 | ~83 分钟 | ~8.5 小时 |
| 高并发 (10+5) | ~3.5 小时 | ~50 分钟 | ~4.3 小时 |

## Token 消耗（LLM 分析）

单章约 **1.1 万 token**（输入 ~2,800 + 输出 ~8,000，含推理 token）。

577 章全量约 **630 万 token**。

## 输出目录结构

```
output/
├── chapters/              # 按章切割的 txt 文件
│   ├── 第001章_xxx.txt
│   └── ...
├── analysis/              # 每章的 LLM 分析缓存
│   ├── chapter_001.json
│   └── ...
├── audio/                 # 每章的音频文件（只有拼接结果）
│   ├── chapter_001.mp3
│   └── ...
├── characters.json        # 全局角色表
└── voices_auto.yaml       # 自动生成的音色配置
```

## CLI 参数

```
python main.py <novel> [选项]

必选:
  novel                    小说文件 (.txt / .epub)

输出:
  --output, -o DIR         输出目录 (默认: 小说同目录/output/)
  --format, -f FORMAT      mp3/wav/m4b (默认: mp3)
  --voices, -v FILE        角色音色配置 (YAML)

流程控制:
  --use-llm                强制使用 LLM 分析
  --llm-only               只切割 + 分析，不合成
  --skip-llm               跳过 LLM，从缓存加载分析结果
  --resume-from N          从第 N 章恢复
  --start-chapter N        起始章节编号 (默认: 1)
  --end-chapter N          结束章节编号 (默认: 0 表示全部)

并发控制:
  --llm-workers N          LLM 分析并发数 (默认: 5)
  --tts-workers N          TTS 合成并发数 (默认: 3)

LLM:
  --llm-model MODEL        模型名 (默认: mimo-v2.5)
  --llm-base-url URL       API 地址 (默认从 .env 读取)
  --llm-api-key KEY        API Key (默认从 .env 读取)
  --llm-chunk-size N       文本块大小 (默认: 2000)

其他:
  --api-key KEY            TTS API Key (默认从 .env 读取)
  --log-level LEVEL        DEBUG/INFO/WARNING/ERROR
```

## Web UI

项目提供了一个 Web 界面，支持可视化操作：

### 启动

```bash
# 安装 Web 依赖
pip install -r web/requirements.txt

# 启动 Web 服务
python web/api.py
# 或
uvicorn web.api:app --host 0.0.0.0 --port 8080
```

打开浏览器访问 `http://localhost:8080`

### 功能

- 📖 拖拽上传 EPUB/TXT
- 📑 章节列表（搜索、状态标记）
- 🔍 LLM 分析（一键分析、进度显示）
- 🎤 音色配置（旁白 + 角色独立配置）
- 🔊 TTS 合成（单章/批量）
- 🎵 音频播放器（波形可视化）
- 📦 导出音频 ZIP

### API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/upload` | 上传文件 |
| GET | `/api/chapters` | 章节列表 |
| GET | `/api/chapters/:id` | 章节内容+分析 |
| POST | `/api/analyze` | LLM 分析 |
| POST | `/api/synthesize` | TTS 合成 |
| GET/PUT | `/api/voices` | 音色配置 |
| GET | `/api/audio/:id` | 章节音频 |
| GET | `/api/export` | 导出 ZIP |
| GET | `/api/tasks/:id` | 任务状态 |

## 项目结构

```
novel-to-audiobook/
├── main.py              # CLI 入口（三步工作流 + 多线程）
├── epub_parser.py       # EPUB 解析器（按章切割）
├── parser.py            # 规则解析器（正则提取对话）
├── llm_parser.py        # LLM 解析器（大模型分析）
├── voice_manager.py     # 角色-音色映射（自动性别分配 + 轮换）
├── tts_engine.py        # MiMo TTS API 封装（preset/voicedesign/clone）
├── audio_merger.py      # 音频拼接与导出（FFmpeg）
├── config.py            # 配置管理（LLMConfig/TTSConfig）
├── dotenv.py            # .env 加载器
├── web/                 # Web UI
│   ├── api.py           # FastAPI 后端
│   ├── index.html       # 前端页面
│   ├── data/            # 项目数据（上传、分析、音频）
│   └── requirements.txt # Web 依赖
├── .env                 # API Key 配置（不提交）
├── .env.example         # API Key 模板
├── requirements.txt
├── voices.yaml          # 示例音色配置
└── README.md
```

## 技术说明

- **TTS 模型**: `mimo-v2.5-tts`（预置音色 + style_instruction 控制语气）
- **LLM 模型**: `mimo-v2.5`（推理模型，需 streaming 模式，max_tokens ≥ 16000）
- **音频格式**: 段落间静音 0.3-0.5s，段内句子间静音 0.15s
- **分段清理**: 合成完成后自动删除临时 .wav 段文件，只保留最终拼接结果
- **LLM 缓存**: 分析结果缓存到 `analysis/` 目录，`--skip-llm` 时自动加载缓存，无需重新调用 LLM
