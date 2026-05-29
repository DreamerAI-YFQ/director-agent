# AI Director Agent

面向海外 DTC 保健品品牌 TikTok 编导团队的 AI 编导 Agent MVP。

项目目标不是做一个单纯聊天机器人，而是搭建一个“知识库驱动创作 + 热点视频学习 + 内容质量反馈”的编导知识资产平台。

## 核心能力

- 对话式内容创作：需求理解、策略、钩子、脚本、文生图 Prompt、图生视频 Prompt、真人实拍、AB 变体、自检。
- RAG 知识库：产品资料、钩子卡片、爆款视频案例、脚本模板、编导经验、投放数据。
- P0-P5 视频学习 Pipeline：元数据、视觉、文案音频、结构叙事、合规效果、结构化入库。
- 词典管理：9 套词典、版本历史、Diff、合规检查。
- Novel 标签闭环：候选标签捕获、Review 队列、决策后进入词典/知识库。
- Prompt 治理：Prompt 版本、输出 schema、element_id 引用追踪。
- 成本与任务后台：Pipeline 阶段、成本记录、引用追踪、审阅、历史会话。
- 可插拔 Agent Runtime：默认 Anthropic Messages SDK；可选 Claude Agent SDK runtime。

## 技术栈

- Backend：Python, FastAPI, WebSocket
- Agent：Anthropic SDK / Claude Agent SDK, 自研 Skills 状态机, 自研 P0-P5 Pipeline
- Database：PostgreSQL + pgvector
- Embedding：DashScope OpenAI-compatible API
- Video/LLM Providers：Claude-compatible, Gemini, QwenVL
- Frontend：原生 HTML/CSS/JavaScript
- DevOps：Docker Compose, Adminer

## 目录结构

```text
agent/                 Agent、Skills、Pipeline、Provider、Storage、Runtime
data/                  本地示例数据
docs/                  项目说明、验收清单、runtime 迁移说明
migrations/            数据库迁移 SQL
prompts/               Prompt 文件与 manifest 注册表
scripts/               初始化数据库、数据接入 demo
static/                前端页面
tests/                 单元测试
main.py                FastAPI 入口
docker-compose.yml     Postgres/pgvector + Adminer
```

## 快速启动

### 1. 准备环境变量

复制示例文件：

```powershell
copy .env.example .env
```

然后在 `.env` 中填写自己的 API Key。不要把 `.env` 提交到仓库。

### 2. 启动数据库和 Adminer

```powershell
docker compose up -d
```

Postgres：

```text
Host: localhost
Port: 5432
Database: ai_director
User: director
Password: director123
```

Adminer：

[http://127.0.0.1:8080](http://127.0.0.1:8080)

Adminer 登录：

```text
System: PostgreSQL
Server: postgres
Username: director
Password: director123
Database: ai_director
```

### 3. 安装 Python 依赖

建议使用 Python 3.13。Claude Agent SDK 的 MCP 依赖在 Python 3.14 环境下可能缺少部分 wheel。

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. 初始化数据库

```powershell
.\venv\Scripts\python.exe scripts\init_db.py
```

查看迁移状态：

```powershell
.\venv\Scripts\python.exe scripts\init_db.py --status
```

### 5. 启动应用

```powershell
.\venv\Scripts\python.exe main.py
```

打开前端：

[http://127.0.0.1:3000](http://127.0.0.1:3000)

## Agent Runtime 切换

默认使用 legacy runtime：

```env
AGENT_RUNTIME=legacy
```

切换 Claude Agent SDK runtime：

```powershell
$env:AGENT_RUNTIME="claude_agent"
.\venv\Scripts\python.exe main.py
```

详细说明见 [docs/agent_runtime_migration.md](docs/agent_runtime_migration.md)。

## 常用验证命令

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m py_compile main.py agent\core.py agent\runtime.py
.\venv\Scripts\python.exe scripts\init_db.py --status
```

## 数据库核心表

- `rag_documents`：RAG 知识库文档与 embedding
- `dictionaries` / `dictionary_versions`：词典与版本历史
- `pipeline_runs` / `pipeline_stages`：P0-P5 Pipeline 运行记录
- `prompt_references`：跨 Prompt element_id 引用追踪
- `novel_tag_candidates`：Novel 标签候选池
- `cost_records`：模型调用成本记录
- `scripts` / `script_versions`：脚本产物与版本
- `sessions` / `messages`：对话会话与消息
- `compliance_checks`：合规检查记录
- `feedback`：编导反馈与效果反馈
- `staged_videos`：第三方视频接入 staging 表

## Prompt 位置

Prompt 文件：

```text
prompts/v1/*.md
```

Prompt 注册与 schema：

```text
prompts/manifest.json
```

目前包含：

- `system.md`
- 10 个 Skills Prompt
- P0-P5 Pipeline Prompt

## MVP 测试建议

1. 打开前端，发送简单问候，确认 WebSocket 流式输出正常。
2. 发送脚本需求，确认对话能保存到 `messages`。
3. 测试合规检查，确认 `compliance_checks` 写入。
4. 运行 P0-P5 Pipeline，确认 `pipeline_runs`、`pipeline_stages`、`prompt_references` 写入。
5. 打开 Adminer 检查数据表。

## 推仓库前注意

- `.env` 已被 `.gitignore` 忽略，不要提交真实 API Key。
- `venv/`、`node_modules/`、`logs/`、`output/` 不要提交。
- 如果真实 Key 曾经泄露到截图或历史提交，请轮换 Key。
- 当前目录不是 Git 仓库时，先执行 `git init` 再提交。

## 项目文档

- [docs/phase1_acceptance_checklist.md](docs/phase1_acceptance_checklist.md)
- [docs/agent_runtime_migration.md](docs/agent_runtime_migration.md)
