import asyncio
import json

from agent.runtime import (
    ClaudeAgentSDKRuntime,
    ClaudeAgentSDKRuntime as SDKRuntime,
    LegacyAnthropicRuntime,
    create_agent_runtime,
)


class FakeClient:
    pass


def test_runtime_defaults_to_claude_agent_when_available(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    monkeypatch.setattr(ClaudeAgentSDKRuntime, "ensure_available", staticmethod(lambda: None))

    runtime = create_agent_runtime(
        model="fake-model",
        session_id="sess_test",
        client=FakeClient(),
    )

    assert isinstance(runtime, ClaudeAgentSDKRuntime)


def test_runtime_uses_legacy_when_explicitly_selected(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "legacy")

    runtime = create_agent_runtime(
        model="fake-model",
        session_id="sess_test",
        client=FakeClient(),
    )

    assert isinstance(runtime, LegacyAnthropicRuntime)


def test_claude_agent_runtime_falls_back_when_sdk_unavailable(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "claude_agent")

    def raise_unavailable():
        raise ImportError("missing sdk")

    monkeypatch.setattr(SDKRuntime, "ensure_available", staticmethod(raise_unavailable))

    runtime = create_agent_runtime(
        model="fake-model",
        session_id="sess_test",
        client=FakeClient(),
    )

    assert isinstance(runtime, LegacyAnthropicRuntime)


def test_claude_agent_runtime_selected_when_available(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "claude_agent")
    monkeypatch.setattr(ClaudeAgentSDKRuntime, "ensure_available", staticmethod(lambda: None))

    runtime = create_agent_runtime(
        model="fake-model",
        session_id="sess_test",
        client=FakeClient(),
    )

    assert isinstance(runtime, ClaudeAgentSDKRuntime)


def test_claude_agent_sdk_tool_bridge_emits_pipeline_events():
    runtime = ClaudeAgentSDKRuntime(model="fake-model", session_id="sess_test")
    event_queue = asyncio.Queue()

    async def fake_pipeline_streamer(args):
        yield {"type": "pipeline_progress", "stage": "P0", "status": "running"}
        yield {"type": "pipeline_progress", "stage": "P0", "status": "completed"}
        yield {
            "type": "pipeline_result",
            "result": {
                "run_id": "run_test",
                "status": "completed",
            },
        }

    def fake_tool_executor(name, args):
        raise AssertionError("pipeline should use pipeline_streamer, not tool_executor")

    async def collect():
        result = await runtime._execute_sdk_tool(
            "run_video_pipeline",
            {"video_input": "{}"},
            fake_tool_executor,
            fake_pipeline_streamer,
            event_queue,
        )
        events = []
        while not event_queue.empty():
            events.append(await event_queue.get())
        return result, events

    result, events = asyncio.run(collect())

    assert json.loads(result)["run_id"] == "run_test"
    assert events[0]["type"] == "tool"
    assert events[1] == {"type": "pipeline_progress", "stage": "P0", "status": "running"}
    assert events[2] == {"type": "pipeline_progress", "stage": "P0", "status": "completed"}
    assert events[3]["type"] == "tool_done"
    assert events[3]["result"] == result


def test_claude_agent_sdk_tool_bridge_emits_normal_tool_result():
    runtime = ClaudeAgentSDKRuntime(model="fake-model", session_id="sess_test")
    event_queue = asyncio.Queue()

    def fake_tool_executor(name, args):
        return f"{name}:{args['query']}"

    async def collect():
        result = await runtime._execute_sdk_tool(
            "search_products",
            {"query": "sleep"},
            fake_tool_executor,
            None,
            event_queue,
        )
        events = []
        while not event_queue.empty():
            events.append(await event_queue.get())
        return result, events

    result, events = asyncio.run(collect())

    assert result == "search_products:sleep"
    assert events == [
        {
            "type": "tool_result",
            "name": "search_products",
            "input": {"query": "sleep"},
            "result": "search_products:sleep",
        }
    ]
