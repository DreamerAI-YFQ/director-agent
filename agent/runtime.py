"""
Agent runtime adapters.

DirectorAgent owns the business workflow: skill state, progress, storage, and
validation. Runtime adapters own the model execution loop. This lets the project
move from the legacy Anthropic Messages SDK loop to Claude Agent SDK without
letting the SDK take over business state.
"""

from __future__ import annotations

import json
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent

ToolExecutor = Callable[[str, dict], str]
PipelineStreamer = Callable[[dict], AsyncGenerator[dict, None]]


class AgentRuntime:
    """Base runtime interface used by DirectorAgent."""

    name = "base"

    async def stream_turn(
        self,
        *,
        system_prompt: str,
        conversation_history: list[dict],
        tools: list[dict],
        tool_executor: ToolExecutor,
        pipeline_streamer: Optional[PipelineStreamer] = None,
        max_rounds: int = 10,
    ) -> AsyncGenerator[dict, None]:
        raise NotImplementedError


class LegacyAnthropicRuntime(AgentRuntime):
    """Existing Anthropic Messages SDK tool loop, kept as a fallback runtime."""

    name = "legacy_anthropic"

    def __init__(
        self,
        *,
        client: Any = None,
        model: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model = model or os.getenv("MODEL", "claude-haiku-4-5")
        if client is not None:
            self.client = client
            return

        from anthropic import AsyncAnthropic

        kwargs = {"api_key": api_key or os.getenv("ANTHROPIC_API_KEY")}
        effective_base_url = base_url or os.getenv("ANTHROPIC_BASE_URL") or None
        if effective_base_url:
            kwargs["base_url"] = effective_base_url
        self.client = AsyncAnthropic(**kwargs)

    async def stream_turn(
        self,
        *,
        system_prompt: str,
        conversation_history: list[dict],
        tools: list[dict],
        tool_executor: ToolExecutor,
        pipeline_streamer: Optional[PipelineStreamer] = None,
        max_rounds: int = 10,
    ) -> AsyncGenerator[dict, None]:
        last_assistant_text = ""
        turn_full_text = ""

        for _ in range(max_rounds):
            tool_use_blocks = []
            current_tool = None

            async with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=conversation_history,
                tools=tools,
            ) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if hasattr(delta, "text"):
                            last_assistant_text += delta.text
                            turn_full_text += delta.text
                            yield {"type": "text", "content": delta.text}
                    elif event_type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", None) == "tool_use":
                            current_tool = {
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            }
                    elif event_type == "content_block_stop" and current_tool:
                        tool_use_blocks.append(current_tool)
                        yield {
                            "type": "tool_start",
                            "name": current_tool["name"],
                            "input": current_tool.get("input", {}),
                        }
                        current_tool = None

                final_response = await stream.get_final_message()

            assistant_content = getattr(final_response, "content", [])
            conversation_history.append({
                "role": "assistant",
                "content": assistant_content,
            })

            if not tool_use_blocks:
                for block in assistant_content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_use_blocks.append({
                            "id": block.id,
                            "name": block.name,
                            "input": getattr(block, "input", {}),
                        })

            if not tool_use_blocks:
                yield {
                    "type": "runtime_done",
                    "assistant_text": last_assistant_text,
                    "turn_full_text": turn_full_text or last_assistant_text,
                }
                return

            tool_results = []
            for tool_block in tool_use_blocks:
                for block in assistant_content:
                    if (
                        getattr(block, "type", None) == "tool_use"
                        and getattr(block, "id", None) == tool_block["id"]
                    ):
                        tool_input = getattr(block, "input", {}) or {}
                        tool_name = getattr(block, "name", tool_block["name"])

                        if tool_name == "run_video_pipeline" and pipeline_streamer:
                            yield {
                                "type": "tool",
                                "name": tool_name,
                                "content": "🔧 启动Pipeline分析...",
                            }
                            tool_output = ""
                            async for pipeline_event in pipeline_streamer(tool_input):
                                if pipeline_event["type"] == "pipeline_progress":
                                    yield {
                                        "type": "pipeline_progress",
                                        "stage": pipeline_event["stage"],
                                        "status": pipeline_event["status"],
                                    }
                                    tool_output += (
                                        f"[{pipeline_event['stage']}:"
                                        f"{pipeline_event['status']}] "
                                    )
                                elif pipeline_event["type"] == "pipeline_result":
                                    tool_output = json.dumps(
                                        pipeline_event["result"],
                                        ensure_ascii=False,
                                        indent=2,
                                    )[:4000]
                                    yield {
                                        "type": "tool_done",
                                        "name": tool_name,
                                        "content": (
                                            "✅ Pipeline完成: "
                                            f"{pipeline_event['result']['run_id']}"
                                        ),
                                        "result": tool_output,
                                    }
                            result = tool_output
                        else:
                            result = tool_executor(tool_name, tool_input)
                            yield {
                                "type": "tool_result",
                                "name": tool_name,
                                "input": tool_input,
                                "result": result[:200],
                            }

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                        break

            conversation_history.append({
                "role": "user",
                "content": tool_results,
            })

        yield {
            "type": "runtime_error",
            "content": "\n\n⚠️ Agent执行轮次超限，请简化你的需求后重试。",
            "assistant_text": last_assistant_text,
            "turn_full_text": turn_full_text or last_assistant_text,
        }


class ClaudeAgentSDKRuntime(AgentRuntime):
    """
    Claude Agent SDK runtime.

    Custom business tools are exposed as an in-process MCP server. This runtime
    intentionally receives system_prompt from DirectorAgent every turn, so the
    project's skill state machine remains the source of truth.
    """

    name = "claude_agent_sdk"

    def __init__(
        self,
        *,
        model: str = "",
        session_id: str = "",
        cwd: str | Path | None = None,
    ):
        self.model = model or os.getenv("CLAUDE_AGENT_MODEL") or os.getenv("MODEL")
        self.session_id = session_id
        self.cwd = Path(cwd or PROJECT_ROOT)

    @staticmethod
    def ensure_available() -> None:
        import claude_agent_sdk  # noqa: F401

    def _build_history_prompt(self, conversation_history: list[dict]) -> str:
        parts = []
        for message in conversation_history:
            role = message.get("role", "user")
            content = message.get("content", "")
            text = self._content_to_text(content)
            if text:
                parts.append(f"{role.upper()}:\n{text}")
        return "\n\n".join(parts)

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        chunks.append(str(item.get("text", "")))
                    elif item.get("type") == "tool_result":
                        chunks.append(f"[tool_result] {item.get('content', '')}")
                elif hasattr(item, "text"):
                    chunks.append(str(item.text))
                elif hasattr(item, "name") and hasattr(item, "input"):
                    chunks.append(f"[tool_use:{item.name}] {item.input}")
            return "\n".join(c for c in chunks if c)
        return ""

    def _sdk_env(self) -> dict[str, str]:
        env = {}
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        ):
            value = os.getenv(key)
            if value:
                env[key] = value
        return env

    async def _execute_sdk_tool(
        self,
        tool_name: str,
        args: dict,
        tool_executor: ToolExecutor,
        pipeline_streamer: Optional[PipelineStreamer],
        event_queue: "asyncio.Queue[dict]",
    ) -> str:
        args = args or {}

        if tool_name == "run_video_pipeline" and pipeline_streamer:
            await event_queue.put({
                "type": "tool",
                "name": tool_name,
                "input": args,
                "content": "🔧 启动Pipeline分析...",
            })
            tool_output = ""
            async for pipeline_event in pipeline_streamer(args):
                if pipeline_event["type"] == "pipeline_progress":
                    await event_queue.put({
                        "type": "pipeline_progress",
                        "stage": pipeline_event["stage"],
                        "status": pipeline_event["status"],
                    })
                    tool_output += (
                        f"[{pipeline_event['stage']}:"
                        f"{pipeline_event['status']}] "
                    )
                elif pipeline_event["type"] == "pipeline_result":
                    tool_output = json.dumps(
                        pipeline_event["result"],
                        ensure_ascii=False,
                        indent=2,
                    )[:4000]
                    await event_queue.put({
                        "type": "tool_done",
                        "name": tool_name,
                        "input": args,
                        "content": f"✅ Pipeline完成: {pipeline_event['result']['run_id']}",
                        "result": tool_output,
                    })
            return tool_output

        result = tool_executor(tool_name, args)
        await event_queue.put({
            "type": "tool_result",
            "name": tool_name,
            "input": args,
            "result": result[:200],
        })
        return result

    def _build_mcp_server(
        self,
        tools: list[dict],
        tool_executor: ToolExecutor,
        pipeline_streamer: Optional[PipelineStreamer],
        event_queue: "asyncio.Queue[dict]",
    ):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        sdk_tools = []
        for spec in tools:
            name = spec["name"]
            description = spec.get("description", "")
            input_schema = spec.get("input_schema", {"type": "object"})

            @tool(name, description, input_schema)
            async def handler(args, tool_name=name):
                try:
                    result = await self._execute_sdk_tool(
                        tool_name,
                        args or {},
                        tool_executor,
                        pipeline_streamer,
                        event_queue,
                    )
                    return {"content": [{"type": "text", "text": result}]}
                except Exception as exc:
                    return {
                        "content": [{"type": "text", "text": str(exc)}],
                        "is_error": True,
                    }

            sdk_tools.append(handler)

        return create_sdk_mcp_server(
            name="ai_director_tools",
            version="1.0.0",
            tools=sdk_tools,
        )

    async def stream_turn(
        self,
        *,
        system_prompt: str,
        conversation_history: list[dict],
        tools: list[dict],
        tool_executor: ToolExecutor,
        pipeline_streamer: Optional[PipelineStreamer] = None,
        max_rounds: int = 10,
    ) -> AsyncGenerator[dict, None]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ToolUseBlock,
            query,
        )

        prompt = self._build_history_prompt(conversation_history)
        event_queue: asyncio.Queue[dict] = asyncio.Queue()
        mcp_server = self._build_mcp_server(
            tools,
            tool_executor,
            pipeline_streamer,
            event_queue,
        )
        allowed_tools = [spec["name"] for spec in tools]

        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            mcp_servers={"ai_director": mcp_server},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            model=self.model,
            max_turns=max_rounds,
            include_partial_messages=True,
            cwd=self.cwd,
            env=self._sdk_env(),
        )

        assistant_text = ""
        streamed_text = False
        message_queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        async def pump_messages():
            try:
                async for message in query(prompt=prompt, options=options):
                    await message_queue.put(message)
            except Exception as exc:
                await message_queue.put(exc)
            finally:
                await message_queue.put(sentinel)

        pump_task = asyncio.create_task(pump_messages())

        try:
            while True:
                try:
                    while True:
                        yield event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                try:
                    message = await asyncio.wait_for(message_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue

                if message is sentinel:
                    break

                if isinstance(message, Exception):
                    yield {
                        "type": "runtime_error",
                        "content": str(message),
                        "assistant_text": assistant_text,
                        "turn_full_text": assistant_text,
                    }
                    return

                async for event in self._handle_sdk_message(
                    message,
                    AssistantMessage=AssistantMessage,
                    ResultMessage=ResultMessage,
                    StreamEvent=StreamEvent,
                    TextBlock=TextBlock,
                    ToolUseBlock=ToolUseBlock,
                    assistant_text_ref={"text": assistant_text, "streamed": streamed_text},
                    conversation_history=conversation_history,
                ):
                    if event["type"] == "_assistant_state":
                        assistant_text = event["assistant_text"]
                        streamed_text = event["streamed_text"]
                        continue
                    yield event
                    if event["type"] in ("runtime_done", "runtime_error"):
                        return
        finally:
            if not pump_task.done():
                pump_task.cancel()

        try:
            while True:
                yield event_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        conversation_history.append({
            "role": "assistant",
            "content": assistant_text,
        })
        yield {
            "type": "runtime_done",
            "assistant_text": assistant_text,
            "turn_full_text": assistant_text,
        }

    async def _handle_sdk_message(
        self,
        message: Any,
        *,
        AssistantMessage: Any,
        ResultMessage: Any,
        StreamEvent: Any,
        TextBlock: Any,
        ToolUseBlock: Any,
        assistant_text_ref: dict,
        conversation_history: list[dict],
    ) -> AsyncGenerator[dict, None]:
        assistant_text = assistant_text_ref["text"]
        streamed_text = assistant_text_ref["streamed"]

        def state_event():
            return {
                "type": "_assistant_state",
                "assistant_text": assistant_text,
                "streamed_text": streamed_text,
            }

        if isinstance(message, StreamEvent):
            event = message.event
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                text = delta.get("text")
                if text:
                    streamed_text = True
                    assistant_text += text
                    yield {"type": "text", "content": text}
                    yield state_event()
            return

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    if not streamed_text:
                        assistant_text += block.text
                        yield {"type": "text", "content": block.text}
                elif isinstance(block, ToolUseBlock):
                    yield {
                        "type": "tool_start",
                        "name": block.name,
                        "input": block.input,
                    }
            yield state_event()
            return

        if isinstance(message, ResultMessage):
            if message.result and not assistant_text:
                assistant_text = message.result
                yield {"type": "text", "content": message.result}

            if message.is_error:
                errors = message.errors or [message.stop_reason or "unknown error"]
                yield {
                    "type": "runtime_error",
                    "content": "\n".join(str(e) for e in errors),
                    "assistant_text": assistant_text,
                    "turn_full_text": assistant_text,
                }
                return

            conversation_history.append({
                "role": "assistant",
                "content": assistant_text,
            })
            yield {
                "type": "runtime_done",
                "assistant_text": assistant_text,
                "turn_full_text": assistant_text,
            }
            return

        yield state_event()


def create_agent_runtime(
    *,
    model: str,
    session_id: str,
    api_key: str | None = None,
    base_url: str | None = None,
    client: Any = None,
) -> AgentRuntime:
    mode = os.getenv("AGENT_RUNTIME", "legacy").strip().lower()

    if mode in {"claude_agent", "claude-agent", "claude_code", "claude-code"}:
        try:
            ClaudeAgentSDKRuntime.ensure_available()
            return ClaudeAgentSDKRuntime(model=model, session_id=session_id)
        except Exception as exc:
            logger.warning(
                "Claude Agent SDK runtime unavailable; falling back to legacy "
                "Anthropic runtime: %s",
                exc,
            )

    return LegacyAnthropicRuntime(
        client=client,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )
