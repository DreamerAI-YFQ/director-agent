import asyncio

from agent.core import DirectorAgent
from agent.skills import SkillName


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeFinalMessage:
    def __init__(self, text):
        self.content = [FakeTextBlock(text)]


class FakeDelta:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    type = "content_block_delta"

    def __init__(self, text):
        self.delta = FakeDelta(text)


class FakeStream:
    def __init__(self, text):
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        self._sent = False
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return FakeEvent(self.text)

    async def get_final_message(self):
        return FakeFinalMessage(self.text)


class FakeMessages:
    def __init__(self, text):
        self.text = text

    def stream(self, **kwargs):
        return FakeStream(self.text)


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


def make_agent_with_response(text):
    agent = DirectorAgent.__new__(DirectorAgent)
    agent.client = FakeClient(text)
    agent.model = "fake-model"
    agent.base_system_prompt = "system"
    agent.conversation_history = []
    from agent.skills import SkillStateMachine

    agent.skill_sm = SkillStateMachine()
    agent._last_assistant_text = ""
    agent.session_id = "test_session"
    return agent


def test_full_delivery_response_syncs_progress_to_complete(monkeypatch):
    text = """
    所有交付物已完成！
    脚本 30s
    文生图Prompt 4张关键帧
    图生视频Prompt 6分镜
    真人实拍方案 场景布置
    A/B变体 B版 + C版
    自检报告 合规通过
    """
    agent = make_agent_with_response(text)

    monkeypatch.setattr("agent.core.storage.save_message", lambda **kwargs: None)

    async def collect():
        events = []
        async for event in agent.chat_stream("生成完整脚本"):
            events.append(event)
        return events

    events = asyncio.run(collect())

    done = [event for event in events if event["type"] == "done"][-1]
    assert done["progress"]["progress"] == "10/10"
    assert done["progress"]["current_skill"] == SkillName.SELF_CHECK.value


def test_hook_candidate_response_does_not_auto_advance(monkeypatch):
    text = """
    进入钩子设计环节。我为你设计3个钩子方案：
    钩子A、钩子B、钩子C。
    请选择一个，或者告诉我调整方向。
    """
    agent = make_agent_with_response(text)
    agent.skill_sm.start()
    agent.skill_sm.mark_completed()
    agent.skill_sm.forward()
    agent.skill_sm.mark_completed()
    agent.skill_sm.forward()

    monkeypatch.setattr("agent.core.storage.save_message", lambda **kwargs: None)

    async def collect():
        events = []
        async for event in agent.chat_stream("给我钩子"):
            events.append(event)
        return events

    events = asyncio.run(collect())

    done = [event for event in events if event["type"] == "done"][-1]
    assert done["progress"]["current_skill"] == SkillName.HOOK_DESIGN.value
    assert done["progress"]["progress"] == "2/10"
