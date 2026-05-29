"""
AI编导Agent - 核心对话引擎
业务状态机 + 可插拔 Agent runtime（Claude Agent SDK / Legacy Anthropic SDK）
"""

import json
import os
import time
import asyncio
from pathlib import Path
from typing import AsyncGenerator, Optional
from agent.skills import SkillStateMachine, SkillName
from agent.prompt_manager import get_prompt_manager
from agent.runtime import LegacyAnthropicRuntime, create_agent_runtime
from agent import storage

# ========== 常量 ==========

DATA_DIR = Path(__file__).parent.parent / "data"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "v1"

# RAG库名映射（工具名 → storage library名）
RAG_LIBRARY_MAP = {
    "search_products": "products",
    "search_hooks": "hooks",
    "search_templates": "scripts",
    "search_videos": "videos",
    "search_experience": "experience",
    "search_ads": "ads",
}

# 词典中文标签（用于展示）
DICT_LABELS = {
    "banned": "禁用词",
    "compliant": "合规替换",
    "terms": "行业术语",
    "ingredients": "成分",
    "efficacy": "功效描述",
    "audience": "人群标签",
    "hook_words": "钩子词",
    "scenes": "场景词",
    "cta": "CTA行动词",
}


def load_prompt(filename: str) -> str:
    """加载prompts/v1目录下的Prompt文件（通过PromptManager版本化管理）"""
    # 尝试通过PromptManager加载（带版本印戳）
    try:
        pm = get_prompt_manager()
        # 去掉.md后缀作为prompt_name
        prompt_name = filename.replace(".md", "")
        return pm.load_prompt(prompt_name)
    except Exception:
        # Fallback: 直接读文件
        filepath = PROMPTS_DIR / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""


# ========== RAG检索工具 ==========

def _rag_search(library: str, query: str) -> str:
    """语义搜索RAG库（通过storage层）"""
    try:
        results = storage.rag_search(query, library=library, top_k=5)
        if not results:
            # 语义搜索无结果时，回退到关键词搜索
            results = storage.rag_keyword_search(query, library=library, top_k=10)
        # 解析content字段（storage存的是JSON字符串）
        parsed = []
        for r in results:
            content = r.get("content", "{}")
            try:
                item = json.loads(content) if isinstance(content, str) else content
                item["_similarity"] = r.get("similarity", 0)
                parsed.append(item)
            except (json.JSONDecodeError, TypeError):
                parsed.append({"raw": content, "_similarity": r.get("similarity", 0)})
        if not parsed:
            return json.dumps({"message": f"在{library}库中未找到匹配结果"}, ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"搜索失败: {str(e)}"}, ensure_ascii=False)


# 保留兼容的函数签名（供execute_tool调用）
def search_products(query: str) -> str:
    return _rag_search("products", query)

def search_hooks(query: str) -> str:
    return _rag_search("hooks", query)

def search_templates(query: str) -> str:
    return _rag_search("scripts", query)

def search_videos(query: str) -> str:
    return _rag_search("videos", query)

def search_experience(query: str) -> str:
    return _rag_search("experience", query)

def search_ads(query: str) -> str:
    return _rag_search("ads", query)


# ========== 词典检索工具 ==========

def search_dictionary(dict_type: str, query: str) -> str:
    """
    检索指定词典（通过storage层语义搜索）
    dict_type: 词典类型（banned/compliant/terms/ingredients/efficacy/audience/hook_words/scenes/cta）
    query: 搜索关键词
    """
    if dict_type not in DICT_LABELS:
        available = ", ".join(f"{k}({v})" for k, v in DICT_LABELS.items())
        return json.dumps({"error": f"未知词典类型: {dict_type}", "available_types": available}, ensure_ascii=False)

    try:
        results = storage.dict_search(dict_type, query, top_k=10)
        if not results:
            return json.dumps({
                "dict_type": dict_type,
                "dict_label": DICT_LABELS.get(dict_type, dict_type),
                "query": query,
                "result_count": 0,
                "message": f"在{DICT_LABELS.get(dict_type, dict_type)}词典中未找到匹配项，请尝试其他关键词",
            }, ensure_ascii=False)

        return json.dumps({
            "dict_type": dict_type,
            "dict_label": DICT_LABELS.get(dict_type, dict_type),
            "query": query,
            "result_count": len(results),
            "results": results,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"词典搜索失败: {str(e)}"}, ensure_ascii=False)


def check_compliance(text: str) -> str:
    """
    合规检查：扫描文本中是否包含禁用词，并返回合规替换建议
    （通过storage层读取词典数据）
    """
    try:
        banned_items = storage.dict_get_all("banned")
        compliant_items = storage.dict_get_all("compliant")
    except Exception as e:
        return json.dumps({"error": f"词典读取失败: {str(e)}"}, ensure_ascii=False)

    alerts = []
    text_lower = text.lower()

    for item in banned_items:
        entry = item.get("value", {})
        word = item.get("key", "") or entry.get("word", "")  # 兼容两种结构
        english = entry.get("english", "")
        # 拆分 english 字段中的 / 分隔词（如 "treat/treatment"）
        english_variants = [e.strip() for e in english.split("/") if e.strip()] if english else []
        # 匹配中文或任一英文变体
        match = word in text_lower
        if not match:
            for ev in english_variants:
                if ev.lower() in text_lower:
                    match = True
                    break
        if match:
            # 查找替换建议
            replacement = None
            for c in compliant_items:
                c_key = c.get("key", "")
                c_entry = c.get("value", {})
                if c_key == word or c_entry.get("banned_word") == word or c_entry.get("banned_english") == english:
                    replacement = c_entry
                    break

            alert = {
                "banned_word": word,
                "english": english,
                "risk_level": entry.get("risk_level", ""),
                "reason": entry.get("reason", ""),
                "regulation": entry.get("regulation", ""),
                "replacements": replacement.get("replacements", []) if replacement else [],
            }
            alerts.append(alert)

    if not alerts:
        try:
            dict_versions = {
                "banned": storage.dict_get_current_version("banned"),
                "compliant": storage.dict_get_current_version("compliant"),
            }
            storage.save_compliance_check(
                script_id=f"compliance_{int(time.time())}",
                risk_level="pass",
                original_text=text[:500],
                dict_versions=dict_versions,
            )
        except Exception:
            pass
        return json.dumps({"status": "pass", "message": "文本通过合规检查，未发现禁用词"}, ensure_ascii=False)

    # 获取当前词典版本号
    try:
        check_dict_versions = {
            "banned": storage.dict_get_current_version("banned"),
            "compliant": storage.dict_get_current_version("compliant"),
        }
    except Exception:
        check_dict_versions = {}

    # 持久化每条合规风险
    check_ts = int(time.time())
    for i, alert in enumerate(alerts):
        try:
            storage.save_compliance_check(
                script_id=f"compliance_{check_ts}_{i}",
                risk_level=alert.get("risk_level", "warning"),
                original_text=alert["banned_word"],
                replacement="|".join(alert.get("replacements", [])) or None,
                regulation=alert.get("regulation"),
                dict_versions=check_dict_versions,
            )
        except Exception:
            pass

    return json.dumps({
        "status": "warning",
        "alert_count": len(alerts),
        "alerts": alerts,
        "message": f"发现{len(alerts)}个合规风险，请替换后重新检查",
    }, ensure_ascii=False, indent=2)


# ========== Function Calling 工具定义 ==========

TOOLS = [
    {
        "name": "search_products",
        "description": "搜索产品资料库。当编导提到某个产品、品类、症状或需求时使用。返回产品名称、目标人群、核心成分、卖点、价格等完整信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如产品名、品类、症状、目标人群等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_hooks",
        "description": "搜索钩子卡片库。当需要设计视频开头钩子时使用。返回不同类型的钩子模板和示例。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如钩子类型、品类、场景等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_templates",
        "description": "搜索脚本结构模板库。当需要选择脚本结构时使用。返回不同类型的脚本模板和结构说明。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如脚本类型、适用场景等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "generate_image_prompt",
        "description": "根据脚本内容生成文生图提示词。输出适用于Midjourney/DALL-E的英文提示词。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_description": {
                    "type": "string",
                    "description": "场景描述，如'一个疲惫的女性在办公室揉太阳穴'"
                },
                "style": {
                    "type": "string",
                    "description": "风格要求，如'真实感'、'动画风'、'扁平化'",
                    "enum": ["photorealistic", "animated", "flat", "cinematic"]
                }
            },
            "required": ["scene_description"]
        }
    },
    {
        "name": "generate_video_prompt",
        "description": "根据脚本内容生成图生视频提示词。输出适用于Runway/Kling的英文提示词。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_description": {
                    "type": "string",
                    "description": "场景描述，如'女性从疲惫变为精神焕发'"
                },
                "camera_movement": {
                    "type": "string",
                    "description": "镜头运动，如'push-in'、'pan-right'、'zoom-out'",
                    "enum": ["push-in", "pull-out", "pan-left", "pan-right", "static", "orbit"]
                },
                "duration": {
                    "type": "string",
                    "description": "时长，如'3s'、'5s'",
                    "default": "3s"
                }
            },
            "required": ["scene_description"]
        }
    },
    {
        "name": "search_videos",
        "description": "搜索爆款视频案例库。当需要参考竞品爆款视频的分析数据时使用。返回视频的五维度分析（钩子/留存曲线/关键时刻/视觉风格/音乐）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如品类、钩子类型、脚本类型等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_experience",
        "description": "搜索编导经验库。当需要参考编导团队的实战经验时使用。返回钩子设计、脚本节奏、实拍合规、A/B测试等经验总结。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如钩子、合规、节奏、A/B测试等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_ads",
        "description": "搜索投放数据库。当需要参考历史投放效果数据时使用。返回CTR、CVR、CPA、ROAS等关键指标和优化洞察。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如产品名、投放月份等"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_script",
        "description": "将生成的完整脚本保存到文件。包含文案、文生图prompt、图生视频prompt、真人实拍方案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "脚本标题"
                },
                "content": {
                    "type": "string",
                    "description": "完整的脚本内容（JSON格式），包含script_text, image_prompts, video_prompts, live_action_plan, ab_variants, self_check"
                }
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "search_dictionary",
        "description": "检索9个内容词典之一。用于查找禁用词、合规替换词、行业术语、成分信息、功效描述、人群标签、钩子词、场景词、CTA行动词。在写文案和自检环节必须使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "dict_type": {
                    "type": "string",
                    "description": "词典类型：banned(禁用词)、compliant(合规替换)、terms(行业术语)、ingredients(成分)、efficacy(功效描述)、audience(人群标签)、hook_words(钩子词)、scenes(场景词)、cta(CTA行动词)",
                    "enum": ["banned", "compliant", "terms", "ingredients", "efficacy", "audience", "hook_words", "scenes", "cta"]
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                }
            },
            "required": ["dict_type", "query"]
        }
    },
    {
        "name": "check_compliance",
        "description": "合规检查：扫描文案中是否包含FDA/FTC禁用词，并给出合规替换建议。在文案撰写完成后和自检环节必须调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "需要检查的文案文本"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "run_video_pipeline",
        "description": "启动P0-P5视频分析流水线。输入一个视频（URL或模拟数据），自动执行元数据识别→视觉分析→文案分析→结构叙事→合规评估→汇总入库的完整流水线。返回结构化分析报告。",
        "input_schema": {
            "type": "object",
            "properties": {
                "video_input": {
                    "type": "string",
                    "description": "视频输入数据（JSON格式），包含video_id、url、metadata等字段。也可直接输入视频URL。"
                }
            },
            "required": ["video_input"]
        }
    },
    {
        "name": "submit_feedback",
        "description": "编导对已生成脚本进行反馈评价。包含打分（1-5星）、文本评论、具体指标的满意度。用于持续优化Agent输出质量。",
        "input_schema": {
            "type": "object",
            "properties": {
                "script_id": {
                    "type": "string",
                    "description": "要评价的脚本ID"
                },
                "rating": {
                    "type": "integer",
                    "description": "整体评分（1-5星）",
                    "enum": [1, 2, 3, 4, 5]
                },
                "comment": {
                    "type": "string",
                    "description": "文字反馈（可选）"
                },
                "metrics": {
                    "type": "object",
                    "description": "分维度评分（可选），如 {hook_effectiveness: 4, compliance: 5, creativity: 3}"
                }
            },
            "required": ["script_id", "rating"]
        }
    }
]


# ========== 工具执行器 ==========

def execute_tool(name: str, input_data: dict) -> str:
    """根据工具名执行对应的函数"""
    tool_map = {
        "search_products": lambda d: search_products(d["query"]),
        "search_hooks": lambda d: search_hooks(d["query"]),
        "search_templates": lambda d: search_templates(d["query"]),
        "search_videos": lambda d: search_videos(d["query"]),
        "search_experience": lambda d: search_experience(d["query"]),
        "search_ads": lambda d: search_ads(d["query"]),
        "generate_image_prompt": lambda d: _generate_image_prompt(d),
        "generate_video_prompt": lambda d: _generate_video_prompt(d),
        "save_script": lambda d: _save_script(d),
        "search_dictionary": lambda d: search_dictionary(d["dict_type"], d["query"]),
        "check_compliance": lambda d: check_compliance(d["text"]),
        "run_video_pipeline": lambda d: _run_video_pipeline(d),
        "submit_feedback": lambda d: _submit_feedback(d),
    }
    handler = tool_map.get(name)
    if handler:
        return handler(input_data)
    return f"未知工具: {name}"


def _generate_image_prompt(params: dict) -> str:
    """生成文生图提示词"""
    scene = params["scene_description"]
    style_map = {
        "photorealistic": "photorealistic, 8k, detailed, natural lighting",
        "animated": "animated style, vibrant colors, clean lines",
        "flat": "flat design, minimal, bold colors, vector style",
        "cinematic": "cinematic, dramatic lighting, 35mm film look",
    }
    style = style_map.get(params.get("style", "photorealistic"), style_map["photorealistic"])
    prompt = f"{scene}, {style}, TikTok vertical format 9:16, health and wellness product advertisement"
    return json.dumps({"prompt": prompt, "format": "Midjourney/DALL-E compatible"}, ensure_ascii=False)


def _generate_video_prompt(params: dict) -> str:
    """生成图生视频提示词"""
    scene = params["scene_description"]
    camera = params.get("camera_movement", "push-in")
    duration = params.get("duration", "3s")
    prompt = f"{scene}, {camera} camera movement, {duration}, smooth motion, TikTok vertical 9:16, professional quality"
    return json.dumps({"prompt": prompt, "camera": camera, "duration": duration, "format": "Runway/Kling compatible"}, ensure_ascii=False)


def _save_script(params: dict) -> str:
    """保存脚本到文件+数据库（含版本印戳）"""
    title = params.get("title", "untitled")
    content = params.get("content", "")
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    safe_title = "".join(c for c in title if c.isalnum() or c in "_ -").strip()
    filepath = output_dir / f"{safe_title}.json"

    # 注入版本印戳到输出数据
    try:
        pm = get_prompt_manager()
        content_data = json.loads(content) if isinstance(content, str) else content
        # 收集所有Skill的版本印戳
        version_stamps = {}
        for skill_name in SkillName:
            prompt_name = skill_name.value
            version_stamps[prompt_name] = pm.get_prompt_version(
                f"skill_{prompt_name}" if prompt_name != "AB变体" else "skill_ab变体"
            )
        content_data["_version_stamps"] = {
            "manifest_version": pm._manifest.get("version", "unknown"),
            "active_version_dir": pm.active_version,
            "saved_at": __import__("datetime").datetime.now().isoformat(),
            "prompt_versions": version_stamps,
        }
        content = json.dumps(content_data, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 版本印戳注入失败不影响保存

    filepath.write_text(content, encoding="utf-8")

    # 同时保存到数据库
    try:
        script_id = f"script_{safe_title}_{int(time.time())}"
        content_dict = json.loads(content) if isinstance(content, str) else content
        storage.save_script(script_id, content_dict)
        return json.dumps({"status": "saved", "path": str(filepath), "script_id": script_id}, ensure_ascii=False)
    except Exception as e:
        # 数据库保存失败不影响文件保存
        return json.dumps({"status": "saved", "path": str(filepath), "db_error": str(e)}, ensure_ascii=False)


def _submit_feedback(params: dict) -> str:
    """编导反馈提交（持久化到数据库）"""
    script_id = params.get("script_id", "")
    rating = int(params.get("rating", 3))
    comment = params.get("comment", "")
    metrics = params.get("metrics", {})

    try:
        content = {"rating": rating, "comment": comment}
        # 逐指标保存
        for metric_name, metric_value in metrics.items():
            storage.save_feedback(
                script_id=script_id,
                feedback_type="rating",
                content=content,
                metric_name=metric_name,
                metric_value=float(metric_value),
            )
        # 保存整体反馈
        storage.save_feedback(
            script_id=script_id,
            feedback_type="rating",
            content=content,
            metric_name="overall",
            metric_value=float(rating),
        )
        return json.dumps({
            "status": "saved",
            "script_id": script_id,
            "rating": rating,
            "metrics_count": len(metrics),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"反馈保存失败: {str(e)}"}, ensure_ascii=False)


def _run_video_pipeline(params: dict) -> str:
    """启动P0-P5视频分析流水线"""
    from agent.pipeline import get_orchestrator

    video_input_str = params.get("video_input", "{}")
    try:
        video_input = json.loads(video_input_str) if isinstance(video_input_str, str) else video_input_str
    except json.JSONDecodeError:
        video_input = {"raw_input": video_input_str}

    orchestrator = get_orchestrator()
    run = orchestrator.run(video_input)

    # 构建结果摘要
    stage_summary = {}
    stages_for_db = []
    for stage, sr in run.stage_results.items():
        stage_summary[stage.value] = {
            "status": sr.status.value,
            "provider": sr.provider,
            "latency_ms": sr.latency_ms,
            "error": sr.error if sr.error else None,
        }
        # 构建数据库保存格式的阶段数据
        stages_for_db.append({
            "stage": stage.value,
            "provider": sr.provider,
            "model": sr.model,
            "status": sr.status.value,
            "result": sr.result,
            "cost_usd": 0,  # 成本在provider层追踪
            "latency_ms": sr.latency_ms,
            "cross_validation_result": sr.cross_validation_result,
            "cross_validation_provider": sr.cross_validation_provider,
            "cross_validation_model": sr.cross_validation_model,
        })

    result = {
        "run_id": run.run_id,
        "status": run.status,
        "total_cost_usd": run.total_cost_usd,
        "duration_sec": round(run.completed_at - run.started_at, 2) if run.completed_at else 0,
        "stages": stage_summary,
    }

    # 持久化到数据库
    try:
        total_latency = int((run.completed_at - run.started_at) * 1000) if run.completed_at else 0
        video_id = run.video_input.get("video_id", run.video_input.get("raw_input", "unknown"))
        storage.save_pipeline_run(
            run_id=run.run_id,
            video_id=str(video_id)[:100],
            stages=stages_for_db,
            total_cost=run.total_cost_usd,
            total_latency=total_latency,
        )
        result["db_saved"] = True
    except Exception as e:
        result["db_saved"] = False
        result["db_error"] = str(e)

    # 如果P5完成了，附加最终报告
    p5_result = run.stage_results.get(
        __import__("agent.pipeline", fromlist=["PipelineStage"]).PipelineStage.P5
    )
    if p5_result and p5_result.status.value == "completed" and p5_result.result:
        result["final_report_available"] = True
        # 保存报告到output目录
        output_dir = Path(__file__).parent.parent / "output"
        output_dir.mkdir(exist_ok=True)
        report_path = output_dir / f"pipeline_{run.run_id}.json"
        report_path.write_text(
            json.dumps(p5_result.result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["report_path"] = str(report_path)

    return json.dumps(result, ensure_ascii=False, indent=2)


async def _run_video_pipeline_stream(params: dict):
    """
    异步启动Pipeline，通过线程安全Queue实时推送阶段进度。
    用于 WebSocket 流式对话中，让前端看到P0-P5每阶段的执行状态。
    """
    from agent.pipeline import get_orchestrator
    import queue as sync_queue

    video_input_str = params.get("video_input", "{}")
    try:
        video_input = json.loads(video_input_str) if isinstance(video_input_str, str) else video_input_str
    except json.JSONDecodeError:
        video_input = {"raw_input": video_input_str}

    q = sync_queue.Queue()

    def on_progress(stage: str, status: str):
        try:
            q.put_nowait({"type": "pipeline_progress", "stage": stage, "status": status})
        except Exception:
            pass

    orchestrator = get_orchestrator()

    def run_sync():
        try:
            run = orchestrator.run(video_input, progress_callback=on_progress)
            q.put_nowait({"type": "pipeline_done", "run": run})
        except Exception as e:
            q.put_nowait({"type": "pipeline_error", "error": str(e)})

    task = asyncio.create_task(asyncio.to_thread(run_sync))

    run = None
    while True:
        try:
            msg = q.get_nowait()
            if msg["type"] == "pipeline_progress":
                yield msg
            elif msg["type"] == "pipeline_done":
                run = msg["run"]
                break
            elif msg["type"] == "pipeline_error":
                yield {"type": "error", "content": f"Pipeline执行失败: {msg['error']}"}
                break
        except sync_queue.Empty:
            if task.done():
                break
            await asyncio.sleep(0.1)
            continue

    if run is None:
        return

    # 构建结果
    stage_summary = {}
    stages_for_db = []
    for stage, sr in run.stage_results.items():
        stage_summary[stage.value] = {
            "status": sr.status.value,
            "provider": sr.provider,
            "latency_ms": sr.latency_ms,
            "error": sr.error if sr.error else None,
        }
        stages_for_db.append({
            "stage": stage.value,
            "provider": sr.provider,
            "model": sr.model,
            "status": sr.status.value,
            "result": sr.result,
            "cost_usd": 0,
            "latency_ms": sr.latency_ms,
            "cross_validation_result": sr.cross_validation_result,
            "cross_validation_provider": sr.cross_validation_provider,
            "cross_validation_model": sr.cross_validation_model,
        })

    result = {
        "run_id": run.run_id,
        "status": run.status,
        "total_cost_usd": run.total_cost_usd,
        "duration_sec": round(run.completed_at - run.started_at, 2) if run.completed_at else 0,
        "stages": stage_summary,
    }

    try:
        total_latency = int((run.completed_at - run.started_at) * 1000) if run.completed_at else 0
        video_id = run.video_input.get("video_id", run.video_input.get("raw_input", "unknown"))
        storage.save_pipeline_run(
            run_id=run.run_id,
            video_id=str(video_id)[:100],
            stages=stages_for_db,
            total_cost=run.total_cost_usd,
            total_latency=total_latency,
        )
        result["db_saved"] = True
    except Exception as e:
        result["db_saved"] = False
        result["db_error"] = str(e)

    p5_result = run.stage_results.get(
        __import__("agent.pipeline", fromlist=["PipelineStage"]).PipelineStage.P5
    )
    if p5_result and p5_result.status.value == "completed" and p5_result.result:
        result["final_report_available"] = True
        output_dir = Path(__file__).parent.parent / "output"
        output_dir.mkdir(exist_ok=True)
        report_path = output_dir / f"pipeline_{run.run_id}.json"
        report_path.write_text(
            json.dumps(p5_result.result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["report_path"] = str(report_path)

    yield {"type": "pipeline_result", "result": result}


# ========== Agent主循环（流式输出） ==========

class DirectorAgent:
    """AI编导Agent - 核心对话引擎（流式输出 + Skill状态机 + 会话持久化）"""

    def __init__(self, api_key: str = None, model: str = None, base_url: str = None,
                 session_id: str = None):
        self.model = model or os.getenv("MODEL", "claude-haiku-4-5")
        self.base_system_prompt = load_prompt("system.md") or self._default_system_prompt()
        self.conversation_history = []
        self.skill_sm = SkillStateMachine()
        self._last_assistant_text = ""  # 用于检测Skill完成

        # 会话持久化
        self.session_id = session_id or f"sess_{int(time.time())}"
        self.runtime = create_agent_runtime(
            model=self.model,
            session_id=self.session_id,
            api_key=api_key,
            base_url=base_url,
        )
        try:
            storage.save_session(self.session_id)
        except Exception:
            pass  # 持久化失败不影响运行

    def _ensure_runtime(self):
        """Compatibility for tests or older callers that construct via __new__."""
        if not hasattr(self, "runtime") or self.runtime is None:
            self.runtime = LegacyAnthropicRuntime(
                client=getattr(self, "client", None),
                model=getattr(self, "model", os.getenv("MODEL", "claude-haiku-4-5")),
            )
        return self.runtime

    def _get_system_prompt(self) -> str:
        """动态组装系统Prompt（含当前Skill Prompt + 进度）"""
        return self.skill_sm.build_system_prompt(self.base_system_prompt)

    def _default_system_prompt(self) -> str:
        """默认系统Prompt"""
        return """你是AI编导助手，隶属于一个海外DTC保健品品牌的内容编导团队。

你的职责是辅助编导团队产出TikTok短视频脚本，每个任务交付完整的制作包：
1. 完整脚本文案
2. 文生图Prompt（Midjourney/DALL-E格式）
3. 图生视频Prompt（Runway/Kling格式）
4. 真人实拍方案
5. A/B变体（至少2个）
6. 自检报告

工作流程：
1. 先理解编导的需求（产品、目标人群、视频类型）
2. 搜索产品资料库了解产品信息
3. 搜索钩子卡片库设计开头
4. 搜索脚本模板库选择结构
5. 生成完整脚本
6. 生成配套的文生图/图生视频Prompt
7. 生成真人实拍方案
8. 生成A/B变体
9. 自检并输出报告

重要原则：
- 使用中文与编导对话
- 文生图和图生视频Prompt用英文输出
- 每个环节都要调用工具查数据，不要凭空编造
- 遵循客户已有的Prompt体系和词典，不替换内容

词典使用规则（必须遵守）：
- 写文案时必须查阅禁用词词典(banned)，确保不使用FDA/FTC禁止的医疗声明用语
- 遇到禁用词必须查阅合规替换词典(compliant)，使用合规替代词
- 文案完成后必须调用check_compliance工具做合规检查
- 描述功效时必须查阅功效描述词典(efficacy)，使用合规的功效表达
- 设计钩子时可查阅钩子词词典(hook_words)和场景词词典(scenes)
- 涉及产品成分必须查阅成分词典(ingredients)确认标准描述
- 涉及目标人群必须查阅人群标签词典(audience)匹配画像
- 自检环节必须调用check_compliance做最终合规审查
"""

    async def chat_stream(self, user_message: str) -> AsyncGenerator[dict, None]:
        """
        流式对话，逐块yield事件字典
        事件类型：
          {"type": "text", "content": "文字片段"}
          {"type": "skill_change", "from": "旧Skill", "to": "新Skill", "progress": "进度条"}
          {"type": "tool_start", "name": "工具名", "input": {...}}
          {"type": "tool_result", "name": "工具名", "result": "结果"}
          {"type": "done"}
        """
        # 保存上一轮助手文本，用于检测完成标志
        assistant_text_before = self._last_assistant_text
        self._last_assistant_text = ""

        # ===== 状态机：处理Skill切换 =====
        if not self.skill_sm.current_skill:
            # 首次对话，启动状态机
            self.skill_sm.start()
            yield {
                "type": "skill_change",
                "from": None,
                "to": self.skill_sm.current_skill.value,
                "progress": self.skill_sm.get_progress_bar(),
            }
        else:
            # 检测用户意图，看是否需要切换Skill
            intent_skill = self.skill_sm.detect_intent(user_message)

            # 检测确认/推进信号
            confirm_words = ["确认", "好的继续", "继续", "没问题", "下一步", "可以继续"]
            is_confirm = any(w in user_message for w in confirm_words)

            if is_confirm and assistant_text_before:
                # 用户明确说"继续"，无条件标记当前skill为完成并推进（防止卡住）
                detected = self.skill_sm.detect_completion(assistant_text_before)
                if self.skill_sm.current_skill:
                    self.skill_sm.mark_completed()
                next_skill = self.skill_sm.forward()
                if next_skill:
                    self._update_context_from_last_response()
                    yield {
                        "type": "skill_change",
                        "from": self.skill_sm.skill_history[-2].value if len(self.skill_sm.skill_history) >= 2 else None,
                        "to": next_skill.value,
                        "progress": self.skill_sm.get_progress_bar(),
                    }
                elif self.skill_sm.current_skill:
                    # 已经是终态
                    yield {
                        "type": "system",
                        "content": f"✅ 所有步骤已完成（{self.skill_sm.get_progress_bar()}）。如需修改，请说明具体需求。"
                    }
            elif intent_skill and intent_skill != self.skill_sm.current_skill:
                # 跳转到意图指向的Skill
                old_skill = self.skill_sm.current_skill
                self.skill_sm.jump_to(intent_skill)
                yield {
                    "type": "skill_change",
                    "from": old_skill.value,
                    "to": intent_skill.value,
                    "progress": self.skill_sm.get_progress_bar(),
                }

        # ===== 记录用户消息 =====
        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })
        # 持久化用户消息
        try:
            storage.save_message(
                session_id=self.session_id,
                role="user",
                content=user_message,
                skill_name=self.skill_sm.current_skill.value if self.skill_sm.current_skill else None,
            )
        except Exception:
            pass

        # ===== 动态组装系统Prompt =====
        system_prompt = self._get_system_prompt()

        runtime_done = None
        async for event in self._ensure_runtime().stream_turn(
            system_prompt=system_prompt,
            conversation_history=self.conversation_history,
            tools=TOOLS,
            tool_executor=execute_tool,
            pipeline_streamer=_run_video_pipeline_stream,
            max_rounds=10,
        ):
            if event["type"] == "runtime_done":
                runtime_done = event
                break
            if event["type"] == "runtime_error":
                self._last_assistant_text = event.get("assistant_text", "")
                yield {"type": "text", "content": event.get("content", "")}
                yield {"type": "done", "progress": self.skill_sm.get_progress()}
                return
            if event["type"] == "tool_result":
                self._update_context_from_tool(
                    event.get("name", ""),
                    event.get("input", {}),
                    event.get("result", ""),
                )
            yield event

        if runtime_done is None:
            runtime_done = {"assistant_text": "", "turn_full_text": ""}

        self._last_assistant_text = runtime_done.get("assistant_text", "")
        full_text = runtime_done.get("turn_full_text") or self._last_assistant_text

        if self.skill_sm.detect_final_delivery(full_text):
            self.skill_sm.mark_all_completed()
        # 每轮最多自动推进一个Skill，避免一次回复里出现多个关键词时状态栏跳过中间环节。
        elif (
            self.skill_sm.detect_completion(full_text)
            and self.skill_sm.current_skill
            and self.skill_sm.current_skill not in self.skill_sm.completed_skills
        ):
            previous_skill = self.skill_sm.current_skill
            self.skill_sm.mark_completed()
            next_skill = self.skill_sm.forward()
            if next_skill:
                yield {
                    "type": "skill_change",
                    "from": previous_skill.value,
                    "to": next_skill.value,
                    "progress": self.skill_sm.get_progress_bar(),
                }

        try:
            storage.save_message(
                session_id=self.session_id,
                role="assistant",
                content=self._last_assistant_text[:4000],
                skill_name=self.skill_sm.current_skill.value if self.skill_sm.current_skill else None,
            )
        except Exception:
            pass

        progress = self.skill_sm.get_progress()
        yield {"type": "done", "progress": progress}
        return

    def _update_context_from_last_response(self):
        """从上一轮回复中提取上下文数据（简单实现）"""
        # 如果当前Skill已完成，把关键信息存入上下文
        skill = self.skill_sm.current_skill
        if skill == SkillName.NEEDS_UNDERSTANDING:
            # 尝试从需求确认单中提取
            self.skill_sm.set_context("needs_confirmed", True)
        elif skill == SkillName.STRATEGY_THINKING:
            self.skill_sm.set_context("strategy_confirmed", True)
        elif skill == SkillName.HOOK_DESIGN:
            self.skill_sm.set_context("hooks_confirmed", True)

    def _update_context_from_tool(self, tool_name: str, tool_input: dict, result: str):
        """从工具调用结果中更新上下文"""
        if tool_name == "search_products":
            self.skill_sm.set_context("last_product_search", result[:500])
        elif tool_name == "search_hooks":
            self.skill_sm.set_context("last_hook_search", result[:500])
        elif tool_name == "search_ads":
            self.skill_sm.set_context("last_ads_search", result[:500])

    async def chat(self, user_message: str) -> str:
        """非流式对话（兼容旧接口）"""
        full_text = ""
        async for event in self.chat_stream(user_message):
            if event["type"] == "text":
                full_text += event.get("content", "")
        return full_text

    def reset(self):
        """重置对话历史和状态机"""
        self.conversation_history = []
        self.skill_sm.reset()
        self._last_assistant_text = ""

    def get_progress(self) -> dict:
        """获取当前Skill进度"""
        return self.skill_sm.get_progress()
