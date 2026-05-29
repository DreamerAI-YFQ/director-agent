"""
AI编导Agent - Novel标签捕获 & 候选Review队列

工程难点 #3 的核心模块：
- 从Pipeline分析结果中捕获新颖标签（不在现有词典中的词/概念）
- 跨视频去重（同novel_xxx不重复review）
- 频次置信度排序
- 待审阅池管理
- 批量决策 → 词典更新流程
"""

import json
import time
from datetime import datetime
from agent import storage

logger = __import__("logging").getLogger(__name__)


# ========== 标签捕获 ==========

def capture_novel_tags(pipeline_run_id: str, stage_outputs: dict,
                       existing_dictionaries: list[str] = None) -> dict:
    """
    从Pipeline各阶段输出中捕获可能的新颖标签

    Args:
        pipeline_run_id: Pipeline运行ID
        stage_outputs: {stage_name: result_dict}
        existing_dictionaries: 需要比对去重的词典类型列表

    Returns:
        {captured: int, candidates: list, dedup_stats: dict}
    """
    if existing_dictionaries is None:
        existing_dictionaries = ["banned", "compliant", "terms", "ingredients",
                                 "efficacy", "audience", "hook_words", "scenes", "cta"]

    # 收集所有已知词典词条
    known_keys = set()
    for dt in existing_dictionaries:
        try:
            items = storage.dict_get_all(dt)
            for item in items:
                key = item.get("key", "").strip().lower()
                if key:
                    known_keys.add(key)
                val = item.get("value", {})
                if isinstance(val, dict):
                    for vk in val.values():
                        if isinstance(vk, str):
                            known_keys.add(vk.strip().lower())
        except Exception:
            pass

    # 从阶段输出中提取候选词
    candidates = []
    seen_texts = set()

    for stage_name, output in stage_outputs.items():
        stage_data = output.get("output", {}) if isinstance(output, dict) else {}
        # 提取文本中的关键词
        words = _extract_keywords(stage_data)
        for word in words:
            word_lower = word.strip().lower()
            if word_lower in seen_texts:
                continue
            if word_lower in known_keys:
                continue
            seen_texts.add(word_lower)

            candidates.append({
                "text": word,
                "source_stage": stage_name,
                "pipeline_run_id": pipeline_run_id,
                "detected_at": datetime.now().isoformat(),
            })

    # 跨已有novel记录去重
    deduped = _deduplicate_candidates(candidates)
    saved = _save_candidates(deduped)

    return {
        "captured": len(saved),
        "candidates": saved,
        "dedup_stats": {
            "total_extracted": len(candidates),
            "after_dedup": len(deduped),
            "saved": len(saved),
        },
    }


def _extract_keywords(data: dict) -> list:
    """从结构化数据中提取关键词"""
    keywords = []

    def _walk(obj):
        if isinstance(obj, str) and 2 <= len(obj) <= 50 and not obj.startswith("#"):
            # 简单分词（按常见分隔符）
            for sep in [",", "、", ";", "\n"]:
                if sep in obj:
                    for part in obj.split(sep):
                        part = part.strip()
                        if 2 <= len(part) <= 50:
                            keywords.append(part)
                    return
            keywords.append(obj)
        elif isinstance(obj, dict):
            for key in obj:
                if key in ("id", "element_id", "risk_level", "cost_usd",
                           "_similarity", "_version_stamps", "parse_warning"):
                    continue
                _walk(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return keywords


def _deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """跨候选词去重并置信度排序"""
    grouped = {}

    for c in candidates:
        key = c["text"].strip().lower()
        if key not in grouped:
            grouped[key] = {
                "text": c["text"],
                "frequency": 1,
                "sources": [c["source_stage"]],
                "first_seen": c["detected_at"],
                "pipeline_runs": [c["pipeline_run_id"]],
            }
        else:
            grouped[key]["frequency"] += 1
            if c["source_stage"] not in grouped[key]["sources"]:
                grouped[key]["sources"].append(c["source_stage"])
            if c["pipeline_run_id"] not in grouped[key]["pipeline_runs"]:
                grouped[key]["pipeline_runs"].append(c["pipeline_run_id"])

    # 按频次排序
    result = sorted(grouped.values(), key=lambda x: -x["frequency"])
    return result


def _save_candidates(candidates: list[dict]) -> list[dict]:
    """保存候选标签到review队列"""
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS novel_tag_candidates (
                candidate_id SERIAL PRIMARY KEY,
                tag_text VARCHAR(200) NOT NULL UNIQUE,
                frequency INT DEFAULT 1,
                sources JSONB DEFAULT '[]',
                confidence_score FLOAT DEFAULT 0.0,
                status VARCHAR(20) DEFAULT 'pending',
                decision VARCHAR(50),
                decided_by VARCHAR(50),
                decided_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        saved = []
        for c in candidates:
            # 计算置信度: frequency * source_diversity
            freq = c["frequency"]
            sources = len(c["sources"])
            confidence = min(freq * 0.1 + sources * 0.2, 1.0)

            try:
                cur.execute("""
                    INSERT INTO novel_tag_candidates
                        (tag_text, frequency, sources, confidence_score)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tag_text) DO UPDATE SET
                        frequency = novel_tag_candidates.frequency + %s,
                        sources = novel_tag_candidates.sources || %s::jsonb,
                        confidence_score = GREATEST(novel_tag_candidates.confidence_score, %s),
                        updated_at = NOW()
                    RETURNING candidate_id, status
                """, (c["text"], freq, json.dumps(c["sources"]), confidence,
                      freq, json.dumps(c["sources"]), confidence))
                row = cur.fetchone()
                if row:
                    c["candidate_id"] = row[0]
                    c["status"] = row[1]
                    c["confidence_score"] = round(confidence, 3)
                saved.append(c)
            except Exception as e:
                storage.logger.warning(f"[Novel] 候选标签保存失败 '{c['text']}': {e}")

        conn.commit()
    return saved


# ========== Review 队列管理 ==========

def get_review_candidates(status: str = None, min_confidence: float = 0.0,
                          limit: int = 50) -> list[dict]:
    """
    获取待审阅的候选标签

    Args:
        status: 过滤状态（pending / approved / rejected / merged）
        min_confidence: 最低置信度
        limit: 返回数量上限
    """
    conn = storage.get_connection()
    with conn.cursor() as cur:
        where = []
        params = []

        if status:
            where.append("status = %s")
            params.append(status)
        if min_confidence > 0:
            where.append("confidence_score >= %s")
            params.append(min_confidence)

        where_clause = ""
        if where:
            where_clause = "WHERE " + " AND ".join(where)

        cur.execute(f"""
            SELECT candidate_id, tag_text, frequency, sources, confidence_score,
                   status, decision, decided_at, created_at
            FROM novel_tag_candidates
            {where_clause}
            ORDER BY confidence_score DESC
            LIMIT %s
        """, params + [limit])

        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def batch_decide(decisions: list[dict], decided_by: str = "admin") -> dict:
    """
    批量决策候选标签

    decisions: [
        {candidate_id: 1, decision: "approved|rejected|merged", target_dict: "hook_words"},
        ...
    ]

    Returns:
        {decided: int, errors: list}
    """
    results = {"decided": 0, "errors": []}
    conn = storage.get_connection()

    for d in decisions:
        cid = d.get("candidate_id")
        decision = d.get("decision", "")
        target_dict = d.get("target_dict", "")

        if decision not in ("approved", "rejected", "merged"):
            results["errors"].append(f"candidate_{cid}: 无效决策 '{decision}'")
            continue

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE novel_tag_candidates
                    SET status = %s, decision = %s, decided_by = %s,
                        decided_at = NOW(), updated_at = NOW()
                    WHERE candidate_id = %s
                """, (decision, decision, decided_by, cid))
                conn.commit()

                # 如果批准，添加到目标词典
                if decision == "approved" and target_dict:
                    cur.execute("""
                        SELECT tag_text FROM novel_tag_candidates
                        WHERE candidate_id = %s
                    """, (cid,))
                    row = cur.fetchone()
                    if row:
                        tag_text = row[0]
                        storage.dict_upsert(target_dict, tag_text, {
                            "source": "novel_capture",
                            "pipeline_confidence": d.get("confidence_score", 0),
                            "approved_by": decided_by,
                            "approved_at": datetime.now().isoformat(),
                        })
                        # 如果是禁用词或合规词，扫描受影响的旧合规检查
                        if target_dict in ("banned", "compliant"):
                            affected = storage.find_affected_compliance_checks(tag_text, target_dict)
                            if affected:
                                ids = [a["check_id"] for a in affected]
                                stale_count = storage.mark_compliance_stale(ids)
                                logger.info(f"[Novel] '{tag_text}' 批准到 {target_dict}: "
                                            f"标记 {stale_count} 条旧合规检查为过期")

                results["decided"] += 1
        except Exception as e:
            results["errors"].append(f"candidate_{cid}: {e}")

    return results


def get_review_stats() -> dict:
    """获取Review队列统计"""
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, COUNT(*) as cnt
            FROM novel_tag_candidates
            GROUP BY status
        """)
        by_status = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT AVG(confidence_score), MAX(confidence_score) FROM novel_tag_candidates")
        row = cur.fetchone()

        return {
            "total_candidates": sum(by_status.values()),
            "pending": by_status.get("pending", 0),
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
            "merged": by_status.get("merged", 0),
            "avg_confidence": round(float(row[0]), 3) if row[0] else 0,
            "max_confidence": round(float(row[1]), 3) if row[1] else 0,
        }


# ========== Pipeline 集成钩子 ==========

def on_pipeline_complete(run_id: str, stages: dict) -> dict:
    """
    Pipeline完成后自动执行Novel标签捕获
    建议在storage.save_pipeline_run之后调用
    """
    return capture_novel_tags(run_id, stages)
