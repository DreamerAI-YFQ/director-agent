"""
AI编导Agent - FastAPI入口
提供REST API和前端聊天界面（流式输出）
"""

import os
import json
import logging
import logging.config
import asyncio
from pathlib import Path
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from agent.core import DirectorAgent
from agent import storage

load_dotenv()

# ========== 日志配置 ==========

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "brief": {
            "format": "[%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "brief",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "default",
            "filename": str(LOG_DIR / "agent.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10MB
            "backupCount": 5,
            "encoding": "utf-8",
        },
        "file_error": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "ERROR",
            "formatter": "default",
            "filename": str(LOG_DIR / "error.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
        },
    },
    "root": {
        "level": "DEBUG",
        "handlers": ["console", "file", "file_error"],
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("ai_director")

app = FastAPI(title="AI编导Agent", version="0.2.0")

# 静态文件
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# 每个WebSocket连接维护一个Agent实例
active_agents: dict = {}


@app.get("/")
async def index():
    """返回聊天界面"""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        from fastapi.responses import HTMLResponse as HR
        return HR(
            content=html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
        )
    return HTMLResponse("<h1>AI编导Agent</h1><p>聊天界面正在构建中...</p>")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket聊天接口（流式输出 + 会话持久化）"""
    await websocket.accept()

    # 支持客户端传session_id恢复会话
    session_id = websocket.query_params.get("session_id")
    agent = DirectorAgent(session_id=session_id)
    agent_id = id(agent)
    active_agents[agent_id] = agent

    try:
        while True:
            # 接收用户消息
            data = await websocket.receive_text()
            msg = json.loads(data)
            user_message = msg.get("content", "")

            if user_message.strip() == "/reset":
                agent.reset()
                await websocket.send_json({
                    "type": "system",
                    "content": "对话已重置"
                })
                continue

            # 流式调用Agent
            try:
                async for event in agent.chat_stream(user_message):
                    try:
                        if event["type"] == "text":
                            await websocket.send_json({
                                "type": "stream",
                                "content": event.get("content", "")
                            })
                        elif event["type"] == "skill_change":
                            await websocket.send_json({
                                "type": "skill_change",
                                "from": event.get("from"),
                                "to": event.get("to"),
                                "progress": event.get("progress", ""),
                                "state": agent.get_progress(),
                            })
                        elif event["type"] == "tool_start":
                            await websocket.send_json({
                                "type": "tool",
                                "name": event.get("name", ""),
                                "content": f"调用工具: {event.get('name', '')}"
                            })
                        elif event["type"] == "tool":
                            await websocket.send_json({
                                "type": "tool",
                                "name": event.get("name", ""),
                                "content": event.get("content", "")
                            })
                        elif event["type"] == "tool_done":
                            await websocket.send_json({
                                "type": "tool_done",
                                "name": event.get("name", ""),
                                "content": event.get("content", "")
                            })
                        elif event["type"] == "tool_result":
                            await websocket.send_json({
                                "type": "tool_done",
                                "name": event.get("name", ""),
                                "content": f"{event.get('name', '')} 完成"
                            })
                        elif event["type"] == "pipeline_progress":
                            await websocket.send_json({
                                "type": "pipeline_progress",
                                "stage": event.get("stage", ""),
                                "status": event.get("status", "")
                            })
                        elif event["type"] == "done":
                            progress = agent.get_progress()
                            await websocket.send_json({
                                "type": "done",
                                "progress": progress,
                            })
                    except Exception as inner_e:
                        logger.warning(f"[WS] 事件处理失败 type={event.get('type','?')}: {inner_e}")
            except Exception as e:
                import traceback
                logger.error(f"[WS] Agent错误: {e}\n{traceback.format_exc()}")
                await websocket.send_json({
                    "type": "error",
                    "content": f"Agent错误: {str(e)}"
                })

    except WebSocketDisconnect:
        if agent_id in active_agents:
            del active_agents[agent_id]


@app.post("/api/chat")
async def chat_api(message: dict):
    """REST API聊天接口（备用）"""
    agent = DirectorAgent()
    try:
        reply = await agent.chat(message.get("content", ""))
        return {"status": "ok", "reply": reply}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/products")
async def list_products():
    """查看产品资料库"""
    results = storage.rag_search("", library="products", top_k=50)
    return [
        {"doc_id": r["doc_id"], "content": r["content"], "metadata": r["metadata"]}
        for r in results
    ]


@app.get("/api/hooks")
async def list_hooks():
    """查看钩子卡片库"""
    results = storage.rag_search("", library="hooks", top_k=50)
    return [
        {"doc_id": r["doc_id"], "content": r["content"], "metadata": r["metadata"]}
        for r in results
    ]


@app.get("/api/templates")
async def list_templates():
    """查看脚本模板库"""
    results = storage.rag_search("", library="scripts", top_k=50)
    return [
        {"doc_id": r["doc_id"], "content": r["content"], "metadata": r["metadata"]}
        for r in results
    ]


@app.get("/api/progress")
async def get_progress():
    """查看当前Skill进度（需要带session，这里返回状态机定义）"""
    from agent.skills import SkillName, FORWARD_TRANSITIONS
    all_skills = list(SkillName)
    return {
        "skills": [s.value for s in all_skills],
        "transitions": {k.value: v.value if v else None for k, v in FORWARD_TRANSITIONS.items()},
    }


@app.get("/api/prompt-versions")
async def get_prompt_versions():
    """查看所有Prompt的版本信息"""
    from agent.prompt_manager import get_prompt_manager
    pm = get_prompt_manager()
    return pm.get_summary()


@app.get("/api/prompt-versions/{prompt_name}")
async def get_prompt_version_detail(prompt_name: str):
    """查看指定Prompt的版本详情和引用关系"""
    from agent.prompt_manager import get_prompt_manager
    pm = get_prompt_manager()
    version_info = pm.get_prompt_version(prompt_name)
    refs = pm.trace_references(prompt_name)
    return {
        "version_info": version_info,
        "references": refs,
    }


@app.post("/api/prompt-versions/{prompt_name}/upgrade")
async def upgrade_prompt_version(prompt_name: str, data: dict):
    """
    升级指定Prompt版本
    body: {"version": "1.1.0", "schema_version": "1.1", "changelog": "...", "breaking_changes": [...]}
    返回升级结果和影响分析（自动标记受影响引用为stale）
    """
    from agent.prompt_manager import get_prompt_manager
    pm = get_prompt_manager()
    result = pm.upgrade_prompt(
        prompt_name=prompt_name,
        new_version=data.get("version", "2.0.0"),
        new_schema_version=data.get("schema_version"),
        changelog_entry=data.get("changelog", ""),
        breaking_changes=data.get("breaking_changes"),
    )
    return result


@app.get("/api/element-trace/{element_id}")
async def trace_element(element_id: str):
    """追溯element_id的来源和引用"""
    from agent.prompt_manager import get_prompt_manager
    pm = get_prompt_manager()
    source = pm.find_element_source(element_id)
    users = pm.who_uses_element(element_id)
    return {
        "element_id": element_id,
        "source": source,
        "referenced_by": users,
    }


# ========== Pipeline API ==========

@app.post("/api/pipeline/run")
async def run_pipeline(video_input: dict):
    """
    启动P0-P5视频分析流水线

    请求体示例:
    {
        "video_id": "tiktok_xxx",
        "url": "https://...",
        "metadata": {...},
        "frames": [...]
    }
    """
    from agent.pipeline import get_orchestrator
    orchestrator = get_orchestrator()
    run = orchestrator.run(video_input)

    stage_summary = {}
    for stage, sr in run.stage_results.items():
        stage_summary[stage.value] = {
            "status": sr.status.value,
            "provider": sr.provider,
            "model": sr.model,
            "latency_ms": sr.latency_ms,
            "error": sr.error if sr.error else None,
        }

    return {
        "run_id": run.run_id,
        "status": run.status,
        "total_cost_usd": run.total_cost_usd,
        "duration_sec": round(run.completed_at - run.started_at, 2) if run.completed_at else 0,
        "stages": stage_summary,
    }


@app.get("/api/pipeline/runs")
async def list_pipeline_runs():
    """列出所有Pipeline运行（从数据库查询）"""
    try:
        conn = storage.get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, video_id, status, total_cost_usd, total_latency_ms,
                       created_at, completed_at
                FROM pipeline_runs
                ORDER BY created_at DESC
                LIMIT 50
            """)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
        return {"runs": [dict(zip(columns, r)) for r in rows]}
    except Exception as e:
        return {"runs": [], "error": str(e)}


@app.get("/api/pipeline/runs/{run_id}")
async def get_pipeline_run(run_id: str):
    """获取指定Pipeline运行的详细结果（从数据库查询）"""
    try:
        run = storage.get_pipeline_run(run_id)
        if not run:
            return {"error": f"运行不存在: {run_id}"}
        return run
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/pipeline/cost")
async def get_pipeline_cost():
    """获取Pipeline成本汇总（从数据库查询）"""
    try:
        return {"summary": storage.get_cost_summary()}
    except Exception as e:
        return {"summary": [], "error": str(e)}


@app.get("/api/pipeline/budget")
async def get_budget_status():
    """获取当前预算状态"""
    from agent.provider import get_cost_tracker
    tracker = get_cost_tracker()
    return {"budget": tracker.get_budget_status(), "alerts": tracker.get_alerts(clear=False)}


@app.get("/api/pipeline/alerts")
async def get_pipeline_alerts():
    """获取成本告警"""
    from agent.provider import get_cost_tracker
    tracker = get_cost_tracker()
    return {"alerts": tracker.get_alerts()}


# ========== 引用追溯 API ==========

@app.get("/api/references/{element_id}")
async def get_element_references(element_id: str):
    """查询某个element_id被哪些下游引用"""
    from agent.reference_tracker import find_references_to
    refs = find_references_to(element_id)
    return {"element_id": element_id, "references": refs, "count": len(refs)}


@app.get("/api/references")
async def list_references(source_stage: str = None, target_stage: str = None):
    """查询阶段间引用关系"""
    from agent.reference_tracker import find_references_by_stage
    if source_stage and target_stage:
        refs = find_references_by_stage((source_stage, target_stage))
    else:
        refs = find_references_by_stage()
    return {"references": refs, "count": len(refs)}


@app.get("/api/references/validate/{pipeline_run_id}")
async def validate_references(pipeline_run_id: str):
    """验证Pipeline运行的引用完整性"""
    from agent.reference_tracker import validate_references
    return validate_references(pipeline_run_id)


@app.get("/api/references/stale")
async def list_stale_references(limit: int = 100):
    """列出所有过期引用（Prompt升级后标记为stale的）"""
    from agent.reference_tracker import find_stale_references
    refs = find_stale_references(limit)
    return {"stale_refs": refs, "count": len(refs)}


@app.post("/api/references/rerun-stale")
async def rerun_stale_references(data: dict):
    """
    重跑过期引用：对标记为stale的引用重新运行Pipeline

    body: {"source_stage": "P3"} or {"ref_ids": [1, 2, 3]}
    """
    from agent.reference_tracker import find_stale_references, mark_references_stale
    from agent.pipeline import get_orchestrator

    source_stage = data.get("source_stage", "")
    ref_ids = data.get("ref_ids", [])

    if ref_ids:
        # 按指定 ref_ids 重跑
        rerun_ids = ref_ids
    elif source_stage:
        # 找到该阶段的所有stale引用
        stale_refs = find_stale_references(limit=500)
        rerun_ids = [r["ref_id"] for r in stale_refs if r["source_stage"] == source_stage]
    else:
        return {"error": "请指定 source_stage 或 ref_ids"}

    if not rerun_ids:
        return {"status": "ok", "reran": 0, "message": "未找到需要重跑的过期引用"}

    # 收集所有需要重跑的 run_id
    conn = storage.get_connection()
    run_ids = set()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT target_pipeline_run_id
            FROM prompt_references
            WHERE ref_id = ANY(%s) AND target_pipeline_run_id IS NOT NULL
        """, (rerun_ids,))
        for row in cur.fetchall():
            if row[0]:
                run_ids.add(row[0])

    # 重新标记为 active（准备重跑）
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE prompt_references SET status = 'active'
            WHERE ref_id = ANY(%s) AND status = 'stale'
        """, (rerun_ids,))
        conn.commit()

    return {
        "status": "ok",
        "reran": len(run_ids),
        "run_ids": list(run_ids)[:20],
        "message": f"已解除 {len(rerun_ids)} 条引用的过期状态，共计 {len(run_ids)} 个Pipeline。请重新运行这些Pipeline以获取最新结果。",
    }


# ========== Novel Review 队列 API ==========

@app.get("/api/review/candidates")
async def list_review_candidates(status: str = "pending", min_confidence: float = 0,
                                  limit: int = 50):
    """获取待审阅的候选标签"""
    from agent.novel_capture import get_review_candidates
    candidates = get_review_candidates(status=status, min_confidence=min_confidence, limit=limit)
    return {"candidates": candidates, "count": len(candidates)}


@app.post("/api/review/decide")
async def batch_decide(decisions: list[dict]):
    """
    批量决策候选标签

    decisions: [
        {"candidate_id": 1, "decision": "approved", "target_dict": "hook_words"},
        {"candidate_id": 2, "decision": "rejected"},
    ]
    """
    from agent.novel_capture import batch_decide
    result = batch_decide(decisions)
    return result


@app.get("/api/review/stats")
async def get_review_stats():
    """获取Review队列统计"""
    from agent.novel_capture import get_review_stats
    return get_review_stats()


# ========== Feedback API ==========

@app.post("/api/feedback")
async def submit_feedback(data: dict):
    """提交编导反馈"""
    from agent import storage
    script_id = data.get("script_id", "")
    rating = int(data.get("rating", 0))
    comment = data.get("comment", "")
    metrics = data.get("metrics", {})

    try:
        content = {"rating": rating, "comment": comment}
        for metric_name, metric_value in metrics.items():
            storage.save_feedback(
                script_id=script_id, feedback_type="rating",
                content=content, metric_name=metric_name,
                metric_value=float(metric_value),
            )
        storage.save_feedback(
            script_id=script_id, feedback_type="rating",
            content=content, metric_name="overall",
            metric_value=float(rating),
        )
        return {"status": "saved", "script_id": script_id, "rating": rating}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/feedback/{script_id}")
async def get_feedback(script_id: str):
    """获取脚本的反馈数据"""
    from agent import storage
    feedback = storage.get_feedback(script_id)
    return {"script_id": script_id, "feedback": feedback}


# ========== RAG & 词典 API ==========

@app.get("/api/rag/{library}")
async def search_rag(library: str, q: str = "", top_k: int = 5):
    """语义搜索RAG库"""
    try:
        results = storage.rag_search(q, library=library, top_k=top_k)
        return {"library": library, "results": results}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/dict/monitor")
async def get_dict_monitor():
    """获取词典监控数据：体积/版本/Novel统计/过期合规检查"""
    try:
        return storage.get_dict_monitor_stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/dict/{dict_type}")
async def search_dict(dict_type: str, q: str = "", top_k: int = 10):
    """语义搜索词典"""
    try:
        if q:
            results = storage.dict_search(dict_type, q, top_k=top_k)
        else:
            results = storage.dict_get_all(dict_type)
        return {"dict_type": dict_type, "results": results}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/dict/{dict_type}/versions")
async def get_dict_versions(dict_type: str, key: str = None):
    """获取词典版本历史"""
    from agent import storage
    history = storage.dict_get_version_history(dict_type, key)
    current = storage.dict_get_current_version(dict_type)
    return {"dict_type": dict_type, "current_version": current, "history": history}


@app.get("/api/dict/{dict_type}/diff/{key}")
async def diff_dict_versions(dict_type: str, key: str, v1: int, v2: int):
    """对比词典两个版本差异"""
    from agent import storage
    diff = storage.dict_diff_versions(dict_type, key, v1, v2)
    return diff


# ========== 时间旅行查询 API ==========

@app.get("/api/compliance/history")
async def get_compliance_history(script_id: str = None, limit: int = 50):
    """获取合规检查历史（含词典版本信息）"""
    return storage.get_compliance_history(script_id, limit)


@app.get("/api/compliance/scripts")
async def get_scripts_with_compliance(limit: int = 20):
    """获取有合规检查记录的脚本列表"""
    return {"scripts": storage.get_script_ids_with_compliance(limit)}


@app.get("/api/compliance/compare/{script_id}")
async def compare_compliance(script_id: str):
    """对比同一脚本在不同词典版本下的合规结果变化"""
    result = storage.compare_compliance_versions(script_id)
    return result


@app.post("/api/compliance/reevaluate/{script_id}")
async def reevaluate_compliance(script_id: str):
    """
    用当前最新词典重新评估脚本合规性（时间旅行重评估）
    返回新旧结果对比
    """
    from agent.core import check_compliance

    # 获取脚本内容
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT content FROM scripts WHERE script_id = %s
        """, (script_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"未找到脚本: {script_id}"}

    content = row[0]
    # 提取所有文本内容用于合规检查
    text_parts = []
    if isinstance(content, dict):
        for key in ("script_text", "copywriting", "narration", "transcript", "text"):
            val = content.get(key, "")
            if val:
                text_parts.append(str(val))
        # 也检查嵌套的 variants
        for variant_name in ("variants", "ab_variants"):
            variants = content.get(variant_name, [])
            if isinstance(variants, list):
                for v in variants:
                    if isinstance(v, dict):
                        for k, val in v.items():
                            if isinstance(val, str) and len(val) > 20:
                                text_parts.append(val)
    text_to_check = " ".join(text_parts) if text_parts else json.dumps(content, ensure_ascii=False)

    # 用当前最新词典重评估
    try:
        new_result_raw = check_compliance(text_to_check)
        new_result = json.loads(new_result_raw)
    except Exception as e:
        return {"error": f"重评估失败: {e}"}

    # 获取旧的合规检查结果用于对比
    old_checks = storage.get_compliance_history(script_id, limit=100)
    old_dict_versions = None
    if old_checks:
        for c in old_checks:
            if c.get("dict_versions"):
                old_dict_versions = c["dict_versions"]
                break

    current_versions = {
        "banned": storage.dict_get_current_version("banned"),
        "compliant": storage.dict_get_current_version("compliant"),
    }

    return {
        "script_id": script_id,
        "reevaluated": True,
        "new_result": new_result,
        "old_dict_versions": old_dict_versions,
        "current_dict_versions": current_versions,
        "previous_check_count": len(old_checks),
        "diff_summary": {
            "old_alert_count": sum(1 for c in old_checks if c.get("risk_level") in ("warning", "error")),
            "new_alert_count": new_result.get("alert_count", 0),
            "message": f"旧版({len(old_checks)}条检查) → 新版({new_result.get('alert_count', 0)}条告警)"
        }
    }


# ========== 会话 API ==========

@app.get("/api/sessions")
async def list_sessions(limit: int = 20):
    """列出所有会话（含消息数、最后活跃时间）"""
    try:
        conn = storage.get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.session_id, s.created_at, s.last_active,
                       COUNT(m.id) as msg_count
                FROM sessions s
                INNER JOIN messages m ON s.session_id = m.session_id
                GROUP BY s.session_id, s.created_at, s.last_active
                HAVING COUNT(m.id) > 0
                ORDER BY s.last_active DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return {"sessions": [
            {
                "session_id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "last_active": r[2].isoformat() if r[2] else None,
                "msg_count": r[3],
            }
            for r in rows
        ]}
    except Exception as e:
        return {"sessions": [], "error": str(e)}


@app.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str, limit: int = 50):
    """获取会话历史"""
    try:
        return {"messages": storage.get_session_history(session_id, limit)}
    except Exception as e:
        return {"messages": [], "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
