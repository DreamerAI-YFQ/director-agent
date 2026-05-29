# Agent Runtime Migration

本项目的业务编排由 `DirectorAgent` 持有，模型执行由可插拔 Runtime 持有。

## 当前 Runtime

### Legacy Anthropic Runtime

作为 fallback 保留。

- 位置：`agent/runtime.py::LegacyAnthropicRuntime`
- SDK：`anthropic.AsyncAnthropic`
- 职责：保留原有 `messages.stream` + tool use 循环。
- 适合：SDK 或认证环境不可用时兜底，以及 Anthropic-compatible 代理演示。

### Claude Agent SDK Runtime

默认启用。

- 位置：`agent/runtime.py::ClaudeAgentSDKRuntime`
- SDK：`claude-agent-sdk`
- 职责：使用 Claude Agent SDK 执行 agent loop，自定义业务工具通过 SDK MCP server 暴露。
- 业务边界：Skills 状态机、进度、会话持久化、Pipeline 结果校验仍由 `DirectorAgent` 和项目代码控制。

默认配置：

```env
AGENT_RUNTIME=claude_agent
```

手动切换到 legacy：

```powershell
$env:AGENT_RUNTIME="legacy"
.\venv\Scripts\python.exe main.py
```

## 重要环境说明

当前系统 Python 是 3.14，`mcp` 依赖在该环境下可能缺少 `rpds-py` wheel。项目 `venv` 是 Python 3.13，并已安装 `claude-agent-sdk` / `mcp`，所以运行 Claude Agent SDK runtime 时优先使用：

```powershell
.\venv\Scripts\python.exe main.py
```

## 工具迁移状态

已完成：

- RAG/词典/合规/反馈等普通工具可通过 SDK MCP server 暴露。
- `run_video_pipeline` 通过 runtime 内部事件队列把 P0-P5 进度转成前端事件。
- SDK 不可用时自动 fallback 到 Legacy Anthropic Runtime。

待验证：

- 真实 Claude Agent SDK 调用需要有效 Claude Code/Anthropic 认证环境。
- SDK runtime 下的长会话 session 恢复还未做成持久 resume。
- SDK runtime 下的工具事件名称仍需前端进一步美化。

## 架构原则

不要让 Claude Agent SDK 接管业务状态。

正确边界：

- 项目状态机决定当前环节：需求理解、钩子设计、文案撰写、自检等。
- Claude Agent SDK 负责在当前上下文中调用工具、组织回答、执行 agent loop。
- 项目代码负责保存状态、更新前端进度、校验输出、落库和引用追踪。
