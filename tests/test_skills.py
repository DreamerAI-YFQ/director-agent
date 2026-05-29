from agent.skills import SkillName, SkillStateMachine


def move_to_hook_design() -> SkillStateMachine:
    sm = SkillStateMachine()
    sm.start()
    sm.mark_completed()
    sm.forward()
    sm.mark_completed()
    sm.forward()
    return sm


def test_hook_candidates_do_not_complete_hook_design():
    sm = move_to_hook_design()
    text = """
    好的，进入钩子设计环节！
    我为你设计3个钩子方案：
    钩子A、钩子B、钩子C。
    请选择一个，或者告诉我调整方向，我再继续往下写完整脚本。
    """

    assert sm.current_skill == SkillName.HOOK_DESIGN
    assert sm.detect_completion(text) is False
    assert sm.current_skill == SkillName.HOOK_DESIGN


def test_hook_confirmation_completes_hook_design():
    sm = move_to_hook_design()
    text = "钩子锁定：选择钩子A。钩子已确认，下一步进入脚本骨架。"

    assert sm.detect_completion(text) is True
    sm.mark_completed()
    next_skill = sm.forward()

    assert next_skill == SkillName.SCRIPT_SKELETON
    assert sm.current_skill == SkillName.SCRIPT_SKELETON


def test_detect_final_delivery_marks_all_skills_complete():
    sm = move_to_hook_design()
    text = """
    所有交付物已完成！
    脚本 30s
    文生图Prompt 4张关键帧
    图生视频Prompt 6分镜
    真人实拍方案 场景布置
    A/B变体 B版 + C版
    自检报告 合规通过
    """

    assert sm.detect_final_delivery(text) is True

    sm.mark_all_completed()
    progress = sm.get_progress()

    assert progress["current_skill"] == SkillName.SELF_CHECK.value
    assert progress["progress"] == "10/10"
    assert set(progress["completed"]) == {skill.value for skill in SkillName}


def test_incomplete_delivery_does_not_trigger_final_completion():
    sm = move_to_hook_design()
    text = """
    先给你脚本和文生图Prompt，
    后面的视频Prompt与自检稍后补。
    """

    assert sm.detect_final_delivery(text) is False
