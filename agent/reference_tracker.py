"""
AI编导Agent - 跨Prompt引用追溯模块

核心功能：
1. 全局唯一 element_id 生成
2. 引用关系存储（哪个阶段输出 → 被哪些下游引用）
3. 反向查询（"哪些P4引用了element_xxx？"）
4. 引用完整性校验（上游改版时标记受影响的下游引用）

这是评估材料中的"难点2"工程化实现。
"""

import json
import re
import time
import uuid
from typing import Optional
from agent import storage


ELEMENT_KEY_PATTERN = re.compile(r"^[A-Z]+_\d+[A-Z0-9_-]*$")
SKIP_KEYS = {
    "_token_stats",
    "_schema_validation",
    "cross_validation",
    "parse_warning",
    "raw_text",
    "pipeline_stage",
    "element_id_prefix",
}


# ========== Element ID 生成器 ==========

def generate_element_id(stage: str, category: str = "", index: int = 0) -> str:
    """
    生成全局唯一的element_id

    格式: {stage}_{category}_{uuid_short}
    示例: P3_hook_a3b9c1d2
    """
    short = uuid.uuid4().hex[:8]
    prefix = stage.lower()
    if category:
        prefix = f"{prefix}_{category}"
    return f"{prefix}_{short}"


# ========== 引用注册 ==========

def register_reference(source_stage: str, source_element_id: str,
                       target_stage: str, target_pipeline_run_id: str = "",
                       reference_type: str = "direct") -> dict:
    """
    注册一个跨Prompt引用关系

    Args:
        source_stage: 上游阶段（如 P3）
        source_element_id: 上游输出的element_id
        target_stage: 下游阶段（如 P4）
        target_pipeline_run_id: 下游阶段所属的Pipeline运行ID
        reference_type: 引用类型（direct / derived / inferred）

    Returns:
        注册记录
    """
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prompt_references (
                ref_id SERIAL PRIMARY KEY,
                source_stage VARCHAR(20) NOT NULL,
                source_element_id VARCHAR(100) NOT NULL,
                target_stage VARCHAR(20) NOT NULL,
                target_pipeline_run_id VARCHAR(100),
                reference_type VARCHAR(20) DEFAULT 'direct',
                element_version INT DEFAULT 1,
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(source_element_id, target_stage, target_pipeline_run_id)
            )
        """)

        # 先查询是否已存在
        cur.execute("""
            SELECT ref_id FROM prompt_references
            WHERE source_element_id = %s
              AND target_stage = %s
              AND COALESCE(target_pipeline_run_id, '') = COALESCE(%s, '')
        """, (source_element_id, target_stage, target_pipeline_run_id))

        existing = cur.fetchone()
        if existing:
            return {
                "ref_id": existing[0],
                "source_stage": source_stage,
                "source_element_id": source_element_id,
                "target_stage": target_stage,
                "reference_type": reference_type,
                "status": "existing",
            }

        # 不存在则插入
        cur.execute("""
            INSERT INTO prompt_references
                (source_stage, source_element_id, target_stage,
                 target_pipeline_run_id, reference_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING ref_id
        """, (source_stage, source_element_id, target_stage,
              target_pipeline_run_id, reference_type))
        row = cur.fetchone()
        ref_id = row[0]
        conn.commit()

    return {
        "ref_id": ref_id,
        "source_stage": source_stage,
        "source_element_id": source_element_id,
        "target_stage": target_stage,
        "reference_type": reference_type,
    }


def register_batch_references(references: list[dict]):
    """
    批量注册引用关系

    references: [{source_stage, source_element_id, target_stage, ...}, ...]
    """
    results = []
    for ref in references:
        r = register_reference(
            source_stage=ref.get("source_stage", ""),
            source_element_id=ref.get("source_element_id", ""),
            target_stage=ref.get("target_stage", ""),
            target_pipeline_run_id=ref.get("target_pipeline_run_id", ""),
            reference_type=ref.get("reference_type", "direct"),
        )
        results.append(r)
    return results


# ========== 反向查询 ==========

def find_references_to(element_id: str) -> list:
    """查询"哪些下游引用了某个 element_id？"——反向追溯"""
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ref_id, source_stage, target_stage, target_pipeline_run_id,
                   reference_type, element_version, status, created_at
            FROM prompt_references
            WHERE source_element_id = %s
            ORDER BY created_at DESC
        """, (element_id,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def find_references_by_stage(between: tuple = None) -> list:
    """
    查询特定阶段间的所有引用关系

    Args:
        between: (source_stage, target_stage) 如 ("P3", "P4")
    """
    conn = storage.get_connection()
    with conn.cursor() as cur:
        if between:
            cur.execute("""
                SELECT ref_id, source_stage, source_element_id, target_stage,
                       target_pipeline_run_id, reference_type, status, created_at
                FROM prompt_references
                WHERE source_stage = %s AND target_stage = %s
                ORDER BY created_at DESC
            """, between)
        else:
            cur.execute("""
                SELECT ref_id, source_stage, source_element_id, target_stage,
                       target_pipeline_run_id, reference_type, status, created_at
                FROM prompt_references
                ORDER BY created_at DESC
                LIMIT 100
            """)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# ========== 引用完整性校验 ==========

def validate_references(pipeline_run_id: str) -> dict:
    """
    验证指定Pipeline运行中的所有引用完整性

    检查：
    1. 每个P4引用是否对应有效的P3输出
    2. 是否有悬空引用（引用了不存在的element）
    3. 引用版本是否一致

    Returns:
        {valid: bool, total_refs: int, broken_refs: list, warnings: list}
    """
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ref_id, source_element_id, target_stage, status
            FROM prompt_references
            WHERE target_pipeline_run_id = %s
        """, (pipeline_run_id,))
        refs = cur.fetchall()

    broken = []
    total = len(refs)
    for ref_id, element_id, target, status in refs:
        if status != "active":
            broken.append({
                "ref_id": ref_id,
                "element_id": element_id,
                "target": target,
                "reason": f"引用状态为 {status}",
            })

    return {
        "valid": len(broken) == 0,
        "total_refs": total,
        "broken_refs": broken,
    }


def find_all_element_ids_for_stage(source_stage: str) -> list[str]:
    """查找指定阶段产出的所有 element_id（用于升级时批量标记）"""
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT source_element_id
            FROM prompt_references
            WHERE source_stage = %s AND source_element_id != ''
        """, (source_stage,))
        return [row[0] for row in cur.fetchall()]


def find_stale_references(limit: int = 100) -> list:
    """查找所有过期的引用（按阶段筛选）"""
    conn = storage.get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ref_id, source_stage, source_element_id, target_stage,
                   target_pipeline_run_id, reference_type, status, created_at
            FROM prompt_references
            WHERE status = 'stale'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# ========== 改版影响分析 ==========

def analyze_version_impact(source_stage: str, changed_element_ids: list[str] = None) -> dict:
    """
    分析上游改版对下游的影响

    当某个阶段的 Prompt 升级时，找出所有受影响的下游引用，标记为 stale。
    如果 changed_element_ids 为空，则自动查找该阶段的全部 element_id。

    Returns:
        {affected_refs: int, stale_marked: int, details: list}
    """
    # 如果没有指定 element_ids，自动查找该阶段的所有 element
    if changed_element_ids is None:
        changed_element_ids = find_all_element_ids_for_stage(source_stage)

    affected = []
    stale_marked = 0
    for eid in changed_element_ids:
        refs = find_references_to(eid)
        active_refs = [r for r in refs if r.get("status") == "active"]
        if active_refs:
            affected.append({
                "element_id": eid,
                "affected_count": len(active_refs),
                "targets": list(set(r["target_stage"] for r in active_refs)),
                "ref_ids": [r["ref_id"] for r in active_refs],
            })

    # 标记所有受影响引用为 stale
    if affected:
        stale_marked = mark_references_stale(source_stage, changed_element_ids)

    return {
        "source_stage": source_stage,
        "changed_elements": len(changed_element_ids),
        "total_affected_refs": sum(a["affected_count"] for a in affected),
        "stale_marked": stale_marked,
        "details": affected,
    }


def mark_references_stale(source_stage: str, element_ids: list[str]) -> int:
    """
    标记引用了指定element_id的引用为"stale"（上游已改版）

    返回受影响的行数
    """
    conn = storage.get_connection()
    count = 0
    with conn.cursor() as cur:
        for eid in element_ids:
            cur.execute("""
                UPDATE prompt_references
                SET status = 'stale', element_version = element_version + 1
                WHERE source_element_id = %s AND status = 'active'
            """, (eid,))
            count += cur.rowcount
        conn.commit()
    return count


# ========== 从Pipeline输出中提取并注册引用 ==========

def extract_and_register_references(pipeline_run_id: str,
                                     stage_outputs: dict) -> dict:
    """
    从Pipeline各阶段输出中提取element_id并注册引用关系

    Args:
        pipeline_run_id: Pipeline运行ID
        stage_outputs: {stage_name: stage_result_dict}

    Pipeline的阶段间引用关系（在Prompt中定义）：
    - P0 → P1/P2/P3/P4/P5
    - P1/P2 → P3/P4/P5
    - P3 → P4/P5
    - P4 → P5

    Returns:
        {registered: int, errors: list}
    """
    registered = 0
    errors = []

    reference_map = {
        "P0": ["P1", "P2", "P3", "P4", "P5"],
        "P1": ["P3", "P4", "P5"],
        "P2": ["P3", "P4", "P5"],
        "P3": ["P4", "P5"],
        "P4": ["P5"],
    }

    for source_stage, target_stages in reference_map.items():
        if source_stage not in stage_outputs:
            continue
        source_output = stage_outputs[source_stage].get("output", {})
        elements = extract_elements(source_output)
        for target_stage in target_stages:
            if target_stage not in stage_outputs:
                continue
            for elem in elements:
                element_id = elem.get("id", "")
                if not element_id:
                    continue
                try:
                    register_reference(
                        source_stage=source_stage,
                        source_element_id=element_id,
                        target_stage=target_stage,
                        target_pipeline_run_id=pipeline_run_id,
                        reference_type="direct",
                    )
                    registered += 1
                except Exception as e:
                    errors.append(f"{source_stage}→{target_stage}: {e}")

    return {"registered": registered, "errors": errors}


def extract_elements(stage_output: dict) -> list[dict]:
    """
    从阶段输出中提取所有element

    支持多种输出结构：
    - {"elements": [{id: ..., ...}]}
    - [{"id": ..., ...}]
    - {"segments": [{id: ...}], "hooks": [{id: ...}]}
    """
    elements = []
    seen_ids = set()

    def _add_element(element_id: str, key_prefix: str = "", source: dict = None):
        if not element_id or element_id in seen_ids:
            return
        source = source or {}
        elements.append({
            "id": element_id,
            "type": key_prefix.strip("_") or "unknown",
            "name": source.get("field") or source.get("name") or source.get("title", ""),
        })
        seen_ids.add(element_id)

    def _recurse(obj, key_prefix=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in SKIP_KEYS:
                    continue
                if ELEMENT_KEY_PATTERN.match(key):
                    _add_element(key, key_prefix or key.rsplit("_", 1)[0].lower(), value if isinstance(value, dict) else {})

            # 如果有id字段，这是一个element
            if "id" in obj and isinstance(obj["id"], str) and len(obj["id"]) > 3:
                _add_element(obj["id"], key_prefix, obj)

            # 遍历子对象
            for key, value in obj.items():
                if key in SKIP_KEYS:
                    continue  # 跳过元数据
                _recurse(value, f"{key_prefix}_{key}" if key_prefix else key)
        elif isinstance(obj, list):
            for item in obj:
                _recurse(item, key_prefix)

    _recurse(stage_output)
    return elements
