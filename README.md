# 归位 · guiwei

**丢进一个文件夹，AI 就把每个文件读懂、改名、归类、建索引。**
*Drop a file into one folder — an LLM reads it, renames it, files it, and indexes it.*

归位是一个**本地**的文件自动整理服务。它盯着一个「收件」文件夹，你往里丢任何文件（下载的、别人发的、AI 产出的），它就用大模型读取内容，按 `日期_类型_主体_主题` 改名，归类到对应文件夹，并维护一套可全文/标签检索、且**每一步都可撤销**的索引。文档和图片走 OpenAI 兼容接口，音频和视频走 Gemini。

*guiwei is a **local** file-organizing service. It watches an inbox folder; whenever a file lands (a download, something a colleague sent, an AI-generated artifact), it reads the content with an LLM, renames it to `date_type_subject_topic`, sorts it into a category folder, and maintains a searchable, **fully undoable** index. Documents & images go to an OpenAI-compatible endpoint; audio & video go to Gemini.*

> 这不是一个 Claude Code skill，而是一个独立后台服务——所以你可以 fork 走、按自己的整理习惯二次开发。
> *This is not a Claude Code skill; it's a standalone background service — fork it and adapt it to how **you** organize.*

---

## 为什么 · Why

AI 时代，你每天产出的文件从几个变成几十上百个：网页、方案、录音、视频，名字奇奇怪怪、类型五花八门。手动命名归档的老办法扛不住了。而且——**对人友好的形式（网页、多媒体）对智能体往往不友好，反之亦然**。归位的答案是：文件夹只做笨存储（按模态/体裁分桶），**检索价值全部沉淀在索引里**（标签 + ≤120 字梗概），既给人一份 `目录.md` 浏览，也给 Agent 一份可调用的目录清单。

*In the AI era you generate dozens-to-hundreds of files a day. The old manual-naming approach breaks down — and formats that are friendly for humans (web pages, media) are unfriendly for agents, and vice-versa. guiwei's answer: folders are dumb storage (bucketed by modality/genre); all retrieval value lives in the index (tags + a ≤120-char synopsis) — a `目录.md` for humans to browse and an agent-callable catalog.*

## 工作原理 · How it works

```
收件夹/                      watch.ps1 (FileSystemWatcher, 去抖 + 周期兜底)
  └─ 你丢进来的文件  ──►  organize.py scan --apply
                            ├─ 判类型/日期(文件名>元数据>文件时间)
                            ├─ 敏感件闸门：不读内容、不发大模型，只按名归档 + 打码
                            ├─ 文档/图片 → OpenAI 兼容大脑；音频/视频 → Gemini
                            ├─ 起名 日期_类型_主体_主题，归类到 文档/图片/视频/音频/…
                            └─ 写三层索引：台账.jsonl(权威源+撤销日志) / 索引.db(SQLite) / 目录.md(人读)
```

顶级夹 = 模态（图片/视频/音频/程序/代码）+ 文档体裁（会议/口播/健康/财务/方案/知识，长尾留「文档」）。软件文件夹会被整体压成 zip 归档（校验完整后才删原夹）。

*Top-level folders = modality (image/video/audio/program/code) + document genres (meeting/voiceover/health/finance/proposal/knowledge, with a catch-all). A whole software folder gets zipped and archived (original removed only after integrity check).*

## 快速开始 · Quick start

**依赖 · Requirements**: Python 3.11+，`pip install pymupdf pillow openpyxl`（docx/pptx/html 走标准库，零额外依赖）。音视频功能需要 `ffmpeg` 在 PATH。实时监控（`watch.ps1`）是 Windows/PowerShell；`organize.py` 引擎本身跨平台。

**1. 配置大模型 key · Configure keys** — 两种方式任选：

环境变量 · Environment variables:
```bash
export LLM_API_KEY=sk-...            # OpenAI 兼容 key（文档/图片）
export LLM_BASE_URL=https://api.openai.com/v1
export GEMINI_API_KEY=...            # 音频/视频
```
或复制 `settings.example.json` 为 `settings.local.json`（放脚本同目录，或用 `GUIWEI_SETTINGS` 指定路径），填进 `credentials`。想用 Claude/其它模型：把 `LLM_BASE_URL` 指到你的 OpenAI 兼容中转，并在 `规则.json` 改 `brain.model`。

**2. 设定收件夹并初始化 · Point it at your inbox**:
```bash
python organize.py init --root ~/Inbox        # 建 索引/ 系统目录 + 默认 规则.json
```

**3. 先 dry-run，再实跑 · Preview, then apply**:
```bash
python organize.py scan  --root ~/Inbox        # dry-run：只写 索引/_预览.md，不动文件
python organize.py scan --apply --root ~/Inbox # 实跑：改名 + 归类 + 写索引（可 undo）
python organize.py undo  --root ~/Inbox        # 不满意？一键全撤回根目录
```

**4. 挂实时监控（Windows）· Run the watcher**:
```powershell
powershell -ExecutionPolicy Bypass -File watch.ps1 -Root "$env:USERPROFILE\Inbox"
powershell -ExecutionPolicy Bypass -File watch.ps1 -DryRun   # 只预览不动文件
```

## 全本地运行 · Fully local

`brain.backend="llm_relay"` 只是个「OpenAI 兼容协议」的名字——官方 API、第三方中转、本机的 llama.cpp / ollama / vLLM 都走它。要让**正文一个字节都不出这台机器**，把 `brain.local_only` 打开：它在 `load_config()` 里当场校验，配错就退出，不给你"以为不出机、其实全发中转"的状态。

| 配置 | 结果 |
|---|---|
| `backend="claude_cli"`（本机 Claude 订阅） | ✅ 放行 |
| `backend="llm_relay"` + base_url 是本机 loopback（`127.0.0.0/8` / `localhost` / `::1`） | ✅ 放行 |
| 其它 | ❌ 启动即退出，报错里打印当前 `backend` 和**生效的** `base_url` |

判的是**生效值**（环境变量优先于 `settings.local.json`），所以"规则.json 写本机、环境变量偷偷指远端"这种半吊子状态拦得住。`local_only` 下音频/视频依然一律不读——那条路只有 Gemini，本机没有对应实现，少一个能力好过把录音发出去。

**接本机模型三步**（llama.cpp 为例，ollama / vLLM 同理）：

1. 起服务：`llama-server -m Ornith-1.5-9B-Q4_K_M.gguf --port 8686`（ollama 默认 `http://127.0.0.1:11434/v1`）
2. 写 `settings.local.json`（放脚本同目录，或用 `GUIWEI_SETTINGS` 指到别处；环境变量 `LLM_API_KEY` / `LLM_BASE_URL` 优先级更高）。它已在 `.gitignore` 里，不会被提交：
   ```json
   {"credentials": {"llm_relay": {"API_KEY": "local", "API_BASE_URL": "http://127.0.0.1:8686/v1"}}}
   ```
3. 改 `<收件夹>/索引/规则.json`：`brain.model` 填本地模型 id（llama.cpp 常填 gguf 路径），`brain.local_only` 设 `true`；本地小模型建议同时把 `brain.timeout` 调到 600–900（默认 180 容不下思考链）、`brain.max_concurrent` 压到 1–2、`brain.batch_size` 设 3 左右。

实测（2026-11-20）：Ornith-1.5-9B-Q4_K_M @ `127.0.0.1:8686`，5 个文件实跑 1 分 43 秒、约 20 秒/件，`reconcile` 悬空 0 / 孤儿 0。9B 也能读出"华远科技智造平台三期""张伟、李娜、王强""EG-200 8 台 × 12000"这类细节，摘要可直接检索。

*`brain.local_only` keeps the brain on this machine: either `claude_cli` (local Claude subscription) or an `llm_relay` whose **effective** base_url is loopback (`127.0.0.0/8`, `localhost`, `::1`) — the shape llama.cpp / ollama / vLLM take. It is checked at load time, so a config that only looks local fails fast with the actual `base_url` in the message. Audio/video stay unread under `local_only` because that path only exists via Gemini. Local models want a bigger `brain.timeout` and lower concurrency: a 9B Q4 on CPU costs ~20 s per file.*

## 命令 · Commands

| 命令 | 作用 |
|---|---|
| `scan` | dry-run，出 `索引/_预览.md`，不动文件 |
| `scan --apply` | 实跑：改名 + 归档 + 写索引（台账可 undo） |
| `undo [--last N]` | 撤销最近 N 条（默认全部本批），文件移回根目录、清索引 |
| `search "词" [--客户 X] [--项目 Y] [--类型 Z]` | 全文 + 标签检索 |
| `folders` | 分析根目录下的文件夹，出裁决清单（只识别不动手） |
| `reindex` | 从台账重建 SQLite + 目录.md |
| `reconcile [--dry-run]` | 索引↔磁盘对账：摘除悬空、列出孤儿（不碰文件） |
| `migrate` | 一次性把已归档文档按体裁规则回迁到顶级夹（可 undo） |
| `init` | 建系统目录骨架 + 默认 `规则.json` |

## 配置 · Configuration

改 `<收件夹>/索引/规则.json`（支持整行 `//` 注释），无需动代码：

- `types` — 扩展名 → 类型桶
- `doc_genre_folders` — 文档按类型标签/文件名路由到体裁顶级夹
- `sensitive.keywords` / `content_keywords` — 命中即敏感（不读、不外发、打码）
- `vocab.{客户,项目,人名}` — 别名归一表，防标签裂变
- `brain.backend` / `brain.model` / `brain.gemini` — 模型与代理
- `brain.local_only` — 只许本机大脑（`llm_relay` 指向 `127.0.0.1` 也算），配错启动即报错，见「全本地运行」
- `general.stable_seconds` — 文件「下完」判定（**必须 < watcher 去抖 6s**）

## 安全 · Safety

- **dry-run 默认**：`scan` 不动任何文件，先看提案。
- **全程可撤销**：每次移动写进 `台账.jsonl`，`undo` 原路移回。
- **敏感件闸门**：文件名或内容命中隐私词（健康/财务/凭据/密钥…，中英文词库）→ **不读内容、不发大模型**，只按文件名归档并给索引打码。
- **不覆盖**：目标重名自动加序号；同盘 `os.replace` 原子移动；`sha256` 去重。
- **单飞锁**：防并发重复处理。
- **可强制全本地**：`brain.local_only=true` 时大脑必须在本机（本机 Claude 订阅，或 loopback 上的 llama.cpp/ollama），配错启动即退出并打印生效的 `base_url`。
- **对账熔断**：批量「文件消失」超阈值疑似存储故障时，一条都不摘，避免误清空索引。

*Dry-run by default · every move is logged and fully undoable · a sensitive-file gate never reads or uploads private files · no overwrite (auto-suffix, atomic same-volume move, sha256 dedup) · single-flight lock · reconcile has a circuit-breaker against mass-deletion false positives · `brain.local_only` refuses to start unless the brain is actually on this machine.*

## 说明 · Notes

按你自己的整理习惯改 `规则.json` 的体裁/词表最好用——每个人整理文件的思路都不一样。大模型只负责起「主题 / 标签 / 梗概」，二进制、大文件、敏感件都不读不外发。**发出去的内容收不回**：请自行确认敏感闸门词库覆盖了你的隐私类型再开实时监控。

*Tune the genres/vocab in `规则.json` to your own habits. The LLM only produces topic/tags/synopsis; binaries, large files and sensitive files are never read or sent. Content sent to a provider can't be recalled — make sure the sensitive-gate keywords cover your privacy types before enabling the live watcher.*

## License

MIT © 2026 张拼拼 (Max Pin)
