"""
AI编导Agent - Skill状态机路由
管理10个Skill之间的跳转逻辑，根据对话上下文动态注入对应Skill Prompt
集成PromptManager实现版本化Prompt加载
"""

import json
from enum import Enum
from typing import Optional
from pathlib import Path
from agent.prompt_manager import get_prompt_manager

PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "v1"


# ========== Skill定义 ==========

class SkillName(str, Enum):
    """10个Skill枚举"""
    NEEDS_UNDERSTANDING = "需求理解"
    STRATEGY_THINKING = "策略思考"
    HOOK_DESIGN = "钩子设计"
    SCRIPT_SKELETON = "脚本骨架"
    COPYWRITING = "文案撰写"
    IMAGE_GEN = "文生图"
    VIDEO_GEN = "图生视频"
    LIVE_ACTION = "真人实拍"
    AB_VARIANT = "AB变体"
    SELF_CHECK = "自检"


# Prompt名称映射（用于从manifest加载）
SKILL_PROMPT_NAMES = {
    SkillName.NEEDS_UNDERSTANDING: "skill_需求理解",
    SkillName.STRATEGY_THINKING: "skill_策略思考",
    SkillName.HOOK_DESIGN: "skill_钩子设计",
    SkillName.SCRIPT_SKELETON: "skill_脚本骨架",
    SkillName.COPYWRITING: "skill_文案撰写",
    SkillName.IMAGE_GEN: "skill_文生图",
    SkillName.VIDEO_GEN: "skill_图生视频",
    SkillName.LIVE_ACTION: "skill_真人实拍",
    SkillName.AB_VARIANT: "skill_ab变体",
    SkillName.SELF_CHECK: "skill_自检",
}


# Skill文件名映射
SKILL_PROMPT_FILES = {
    SkillName.NEEDS_UNDERSTANDING: "skill_需求理解.md",
    SkillName.STRATEGY_THINKING: "skill_策略思考.md",
    SkillName.HOOK_DESIGN: "skill_钩子设计.md",
    SkillName.SCRIPT_SKELETON: "skill_脚本骨架.md",
    SkillName.COPYWRITING: "skill_文案撰写.md",
    SkillName.IMAGE_GEN: "skill_文生图.md",
    SkillName.VIDEO_GEN: "skill_图生视频.md",
    SkillName.LIVE_ACTION: "skill_真人实拍.md",
    SkillName.AB_VARIANT: "skill_ab变体.md",
    SkillName.SELF_CHECK: "skill_自检.md",
}

# Skill对应的工具（该Skill下优先使用哪些工具）
SKILL_TOOLS = {
    SkillName.NEEDS_UNDERSTANDING: ["search_products", "search_dictionary"],
    SkillName.STRATEGY_THINKING: ["search_products", "search_ads", "search_videos", "search_dictionary"],
    SkillName.HOOK_DESIGN: ["search_hooks", "search_videos", "search_experience", "search_dictionary"],
    SkillName.SCRIPT_SKELETON: ["search_templates", "search_experience", "search_dictionary"],
    SkillName.COPYWRITING: ["search_products", "search_hooks", "search_experience", "search_dictionary", "check_compliance"],
    SkillName.IMAGE_GEN: ["generate_image_prompt", "search_dictionary"],
    SkillName.VIDEO_GEN: ["generate_video_prompt", "search_dictionary"],
    SkillName.LIVE_ACTION: ["search_experience", "search_dictionary"],
    SkillName.AB_VARIANT: ["search_hooks", "search_ads", "search_dictionary"],
    SkillName.SELF_CHECK: ["search_experience", "check_compliance", "search_dictionary"],
}

# Skill完成标志关键词（用于检测Skill是否完成）
SKILL_COMPLETION_SIGNALS = {
    SkillName.NEEDS_UNDERSTANDING: [
        "需求确认单", "确认后我将开始", "确认需求",
    ],
    SkillName.STRATEGY_THINKING: [
        "策略方案", "确认策略后", "策略方向",
    ],
    SkillName.HOOK_DESIGN: [
        "钩子锁定", "确认钩子", "钩子设计完成", "钩子已确认",
    ],
    SkillName.SCRIPT_SKELETON: [
        "骨架确认", "脚本骨架已确认", "确认骨架", "进入文案撰写",
    ],
    SkillName.COPYWRITING: [
        "文案完成", "脚本文案已完成", "完整交付物", "进入文生图",
    ],
    SkillName.IMAGE_GEN: [
        "文生图", "文生图Prompt", "image prompt", "Midjourney", "DALL-E",
    ],
    SkillName.VIDEO_GEN: [
        "图生视频", "图生视频Prompt", "video prompt", "Runway", "Kling",
    ],
    SkillName.LIVE_ACTION: [
        "实拍方案", "真人实拍", "实拍", "拍摄指导", "场景方案",
    ],
    SkillName.AB_VARIANT: [
        "A/B", "A版", "B版", "变体", "ab测试",
    ],
    SkillName.SELF_CHECK: [
        "自检报告", "自检", "质检完成", "可投放", "评级",
    ],
}

# 意图关键词 → Skill映射（用于意图检测）
INTENT_KEYWORDS = {
    SkillName.NEEDS_UNDERSTANDING: [
        "做个视频", "帮我写", "做一个", "新脚本", "视频脚本",
        "什么产品", "哪个产品", "目标人群", "给谁看",
    ],
    SkillName.STRATEGY_THINKING: [
        "策略", "方向", "怎么打", "用什么策略", "内容策略",
        "调整策略", "换方向",
    ],
    SkillName.HOOK_DESIGN: [
        "钩子", "开头", "前3秒", "开头怎么写", "hook",
        "抓眼球", "吸引力",
    ],
    SkillName.SCRIPT_SKELETON: [
        "骨架", "结构", "大纲", "框架", "脚本结构",
    ],
    SkillName.COPYWRITING: [
        "文案", "脚本内容", "写文案", "写脚本", "正文",
        "旁白", "台词",
    ],
    SkillName.IMAGE_GEN: [
        "文生图", "图片", "画面", "配图", "image",
        "Midjourney", "DALL-E", "插图",
    ],
    SkillName.VIDEO_GEN: [
        "图生视频", "视频", "动态", "Runway", "Kling",
        "动画效果", "镜头",
    ],
    SkillName.LIVE_ACTION: [
        "真人", "实拍", "拍摄", "演员", "场景",
        "道具", "化妆",
    ],
    SkillName.AB_VARIANT: [
        "A/B", "变体", "ab测试", "另一个版本", "多版本",
        "对照", "变体版本",
    ],
    SkillName.SELF_CHECK: [
        "自检", "检查", "质检", "合规", "审核",
        "有没有问题", "检查一下",
    ],
}


# ========== 状态转移定义 ==========

# 正向流转（编导确认后自动推进）
FORWARD_TRANSITIONS = {
    SkillName.NEEDS_UNDERSTANDING: SkillName.STRATEGY_THINKING,
    SkillName.STRATEGY_THINKING: SkillName.HOOK_DESIGN,
    SkillName.HOOK_DESIGN: SkillName.SCRIPT_SKELETON,
    SkillName.SCRIPT_SKELETON: SkillName.COPYWRITING,
    SkillName.COPYWRITING: SkillName.IMAGE_GEN,
    SkillName.IMAGE_GEN: SkillName.VIDEO_GEN,
    SkillName.VIDEO_GEN: SkillName.LIVE_ACTION,
    SkillName.LIVE_ACTION: SkillName.AB_VARIANT,
    SkillName.AB_VARIANT: SkillName.SELF_CHECK,
    SkillName.SELF_CHECK: None,  # 终态
}

# 回退流转（编导要求修改时）
BACKWARD_TRANSITIONS = {
    SkillName.STRATEGY_THINKING: SkillName.NEEDS_UNDERSTANDING,
    SkillName.HOOK_DESIGN: SkillName.STRATEGY_THINKING,
    SkillName.SCRIPT_SKELETON: SkillName.HOOK_DESIGN,
    SkillName.COPYWRITING: SkillName.SCRIPT_SKELETON,
    SkillName.IMAGE_GEN: SkillName.COPYWRITING,
    SkillName.VIDEO_GEN: SkillName.IMAGE_GEN,
    SkillName.LIVE_ACTION: SkillName.VIDEO_GEN,
    SkillName.AB_VARIANT: SkillName.LIVE_ACTION,
    SkillName.SELF_CHECK: SkillName.AB_VARIANT,
}

# 自由跳转（编导可以随时跳到任何Skill）
FREE_JUMP_SKILLS = {
    SkillName.SELF_CHECK,  # 随时可以要求自检
    SkillName.NEEDS_UNDERSTANDING,  # 随时可以改需求
}


# ========== 状态机 ==========

class SkillStateMachine:
    """Skill状态机路由器"""

    def __init__(self):
        self.current_skill: Optional[SkillName] = None
        self.skill_history: list[SkillName] = []
        self.completed_skills: set[SkillName] = set()
        self.context: dict = {}  # 跨Skill共享的上下文数据

    def start(self) -> SkillName:
        """初始化，进入第一个Skill"""
        self.current_skill = SkillName.NEEDS_UNDERSTANDING
        self.skill_history.append(self.current_skill)
        return self.current_skill

    def get_current_skill(self) -> Optional[SkillName]:
        """获取当前Skill"""
        return self.current_skill

    def forward(self) -> Optional[SkillName]:
        """推进到下一个Skill"""
        if self.current_skill is None:
            return self.start()

        if self.current_skill in self.completed_skills:
            # 已完成，推进
            next_skill = FORWARD_TRANSITIONS.get(self.current_skill)
            if next_skill:
                self.current_skill = next_skill
                self.skill_history.append(self.current_skill)
                return self.current_skill
        return None

    def backward(self) -> Optional[SkillName]:
        """回退到上一个Skill"""
        prev_skill = BACKWARD_TRANSITIONS.get(self.current_skill)
        if prev_skill:
            self.current_skill = prev_skill
            self.skill_history.append(self.current_skill)
            return self.current_skill
        return None

    def jump_to(self, skill: SkillName) -> SkillName:
        """自由跳转到指定Skill"""
        self.current_skill = skill
        self.skill_history.append(self.current_skill)
        return self.current_skill

    def mark_completed(self, skill: SkillName = None):
        """标记Skill为已完成"""
        target = skill or self.current_skill
        if target:
            self.completed_skills.add(target)

    def detect_intent(self, user_message: str) -> Optional[SkillName]:
        """
        从用户消息中检测意图，返回应该切换到的Skill
        返回None表示保持当前Skill
        """
        msg_lower = user_message.lower()

        # 1. 检测确认信号（推进到下一个Skill）
        confirm_words = ["确认", "好的", "继续", "可以", "没问题", "下一步", "next", "ok", "go"]
        if any(w in msg_lower for w in confirm_words):
            # 如果当前Skill已产出完成标志，则推进
            return None  # 推进由 mark_completed + forward 处理

        # 2. 检测回退信号
        revise_words = ["改一下", "修改", "换一个", "重新", "不对", "不是", "调整"]
        if any(w in msg_lower for w in revise_words):
            # 检测想修改哪个环节
            for skill, keywords in INTENT_KEYWORDS.items():
                if any(kw in msg_lower for kw in keywords):
                    return skill
            # 没指定环节，回退到上一步
            return BACKWARD_TRANSITIONS.get(self.current_skill)

        # 3. 检测自由跳转意图
        for skill, keywords in INTENT_KEYWORDS.items():
            if any(kw in msg_lower for kw in keywords):
                if skill != self.current_skill:
                    return skill

        return None

    def detect_completion(self, assistant_message: str) -> bool:
        """
        检测助手消息中是否包含当前Skill的完成标志
        """
        if self.current_skill is None:
            return False

        signals = SKILL_COMPLETION_SIGNALS.get(self.current_skill, [])
        return any(signal in assistant_message for signal in signals)

    def detect_final_delivery(self, assistant_message: str) -> bool:
        """检测是否已经输出完整交付物，用于一次性同步最终进度。"""
        if not assistant_message:
            return False

        required_groups = [
            ["脚本", "脚本文案"],
            ["文生图", "文生图Prompt", "image prompt"],
            ["图生视频", "图生视频Prompt", "video prompt"],
            ["真人实拍", "实拍方案"],
            ["A/B", "AB变体", "A/B变体"],
            ["自检", "自检报告", "合规通过"],
        ]
        return all(any(keyword in assistant_message for keyword in group) for group in required_groups)

    def mark_all_completed(self):
        """标记全部Skill完成，并把当前状态停在最后一步。"""
        all_skills = list(SkillName)
        self.completed_skills = set(all_skills)
        self.current_skill = SkillName.SELF_CHECK
        for skill in all_skills:
            if skill not in self.skill_history:
                self.skill_history.append(skill)

    def get_skill_prompt(self, skill: SkillName = None) -> str:
        """加载指定Skill的Prompt文件（通过PromptManager版本化管理）"""
        target = skill or self.current_skill
        if target is None:
            return ""

        prompt_name = SKILL_PROMPT_NAMES.get(target)
        if not prompt_name:
            return ""

        # 优先使用PromptManager加载（带版本印戳）
        try:
            pm = get_prompt_manager()
            return pm.load_prompt(prompt_name)
        except Exception:
            # Fallback: 直接读文件
            filename = SKILL_PROMPT_FILES.get(target)
            if filename:
                filepath = PROMPTS_DIR / filename
                if filepath.exists():
                    return filepath.read_text(encoding="utf-8")
            return f"[Skill Prompt未找到: {prompt_name}]"

    def get_current_tools(self) -> list[str]:
        """获取当前Skill推荐使用的工具列表"""
        if self.current_skill is None:
            return []
        return SKILL_TOOLS.get(self.current_skill, [])

    def set_context(self, key: str, value):
        """设置跨Skill共享的上下文"""
        self.context[key] = value

    def get_context(self, key: str, default=None):
        """获取上下文"""
        return self.context.get(key, default)

    def get_progress(self) -> dict:
        """获取当前进度信息"""
        all_skills = list(SkillName)
        return {
            "current_skill": self.current_skill.value if self.current_skill else None,
            "completed": [s.value for s in self.completed_skills],
            "progress": f"{len(self.completed_skills)}/{len(all_skills)}",
            "next_skill": FORWARD_TRANSITIONS.get(self.current_skill).value if self.current_skill and FORWARD_TRANSITIONS.get(self.current_skill) else None,
            "history": [s.value for s in self.skill_history],
        }

    def get_progress_bar(self) -> str:
        """生成文本进度条"""
        all_skills = list(SkillName)
        current_idx = all_skills.index(self.current_skill) if self.current_skill else -1
        bar = ""
        for i, skill in enumerate(all_skills):
            if skill in self.completed_skills:
                bar += "✅"
            elif i == current_idx:
                bar += "🔹"
            else:
                bar += "⬜"
        return f"{bar} {len(self.completed_skills)}/{len(all_skills)}"

    def build_system_prompt(self, base_system_prompt: str) -> str:
        """
        组装完整的系统Prompt：
        base_system_prompt + 当前Skill Prompt + 进度信息 + 上下文
        """
        parts = [base_system_prompt]

        # 当前Skill Prompt
        if self.current_skill:
            skill_prompt = self.get_skill_prompt()
            if skill_prompt:
                parts.append(f"\n\n---\n\n## 当前激活的Skill: {self.current_skill.value}\n\n{skill_prompt}")

        # 进度条
        progress = self.get_progress_bar()
        parts.append(f"\n\n---\n\n## 当前进度\n{progress}")
        parts.append(f"当前步骤: {self.current_skill.value if self.current_skill else '未开始'}")
        if self.current_skill and FORWARD_TRANSITIONS.get(self.current_skill):
            parts.append(f"下一步骤: {FORWARD_TRANSITIONS[self.current_skill].value}")

        # 上下文数据（如果有）
        if self.context:
            context_str = json.dumps(self.context, ensure_ascii=False, indent=2)
            parts.append(f"\n\n---\n\n## 已确认的上下文数据\n{context_str}")

        return "".join(parts)

    def reset(self):
        """重置状态机"""
        self.current_skill = None
        self.skill_history = []
        self.completed_skills = set()
        self.context = {}
