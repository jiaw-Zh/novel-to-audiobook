# 小说转有声书 — 基于小米 MiMo TTS 的多角色配音

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

# Step 3: 用调整后的配置合成
python main.py novel.epub --skip-llm --voices output/voices_auto.yaml
```

### 模式 3: 断点续传

```bash
# 从第 50 章恢复合成
python main.py novel.epub --skip-llm --resume-from 50
```

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

## LLM 分析结果

### characters.json

```json
[
  {"name": "陈新", "gender": "male", "age": "unknown", "voice_hint": "年轻男性，声音从容淡定"},
  {"name": "刘民有", "gender": "male", "age": "unknown", "voice_hint": "年轻男性，声音紧张焦虑"}
]
```

### voices_auto.yaml（自动生成，可手动编辑）

```yaml
default:
  mode: preset
  voice_id: 白桦
  style_instruction: 用标准播音腔朗读

characters:
  陈新:
    mode: voicedesign
    voice_prompt: 男性，年轻，声音从容淡定
  刘民有:
    mode: voicedesign
    voice_prompt: 男性，年轻，声音紧张焦虑
```

## 角色配置说明

三种音色模式：

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `voicedesign` | 文本描述设计音色 | 主要角色（LLM 自动生成） |
| `preset` | 预置音色 | 旁白、次要角色 |
| `clone` | 音频克隆 | 需要特定声音的角色 |

预置音色：冰糖(女)、茉莉(女)、苏打(男)、白桦(男)、Mia(女)、Chloe(女)、Milo(男)、Dean(男)

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
  --skip-llm               跳过 LLM，用规则解析
  --resume-from N          从第 N 章恢复

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

## 项目结构

```
novel-to-audiobook/
├── main.py              # CLI 入口（三步工作流 + 多线程）
├── epub_parser.py       # EPUB 解析器（按章切割）
├── parser.py            # 规则解析器（正则提取对话）
├── llm_parser.py        # LLM 解析器（大模型分析）
├── voice_manager.py     # 角色-音色映射
├── tts_engine.py        # MiMo TTS API 封装
├── audio_merger.py      # 音频拼接与导出
├── config.py            # 配置管理
├── dotenv.py            # .env 加载器
├── .env                 # API Key 配置（不提交）
├── .env.example         # API Key 模板
├── requirements.txt
├── voices.yaml          # 示例音色配置
└── README.md
```
