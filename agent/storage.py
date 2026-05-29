"""
AI编导Agent - 统一数据访问层
PostgreSQL + pgvector 向量搜索 + 阿里云百炼 Embedding API
"""

import json
import os
import uuid
import logging
from datetime import datetime
from typing import Optional

import psycopg2
from psycopg2.extras import Json, execute_values
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ========== 配置 ==========

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB", "ai_director"),
    "user": os.getenv("PG_USER", "director"),
    "password": os.getenv("PG_PASSWORD", "director123"),
}

EMBEDDING_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("DASHSCOPE_BASE_URL",
                                "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

# ========== 连接管理 ==========

_connection = None


def get_connection():
    """获取PostgreSQL连接（单例）"""
    global _connection
    if _connection is None or _connection.closed:
        _connection = psycopg2.connect(**DB_CONFIG)
        _connection.autocommit = True
    return _connection


def close_connection():
    """关闭连接"""
    global _connection
    if _connection and not _connection.closed:
        _connection.close()
        _connection = None


# ========== Embedding ==========

_embedding_client = None


def _get_embedding_client():
    """获取百炼 Embedding 客户端"""
    global _embedding_client
    if _embedding_client is None:
        _embedding_client = OpenAI(
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
        )
    return _embedding_client


def embed(text: str) -> list:
    """
    调用阿里云百炼API生成文本embedding
    返回1024维向量
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    client = _get_embedding_client()
    try:
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],  # 截断过长文本
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"[Embedding] 调用失败: {e}")
        return [0.0] * EMBEDDING_DIM


def embed_batch(texts: list) -> list:
    """
    批量生成embedding
    """
    if not texts:
        return []

    client = _get_embedding_client()
    try:
        # 过滤空文本
        valid_texts = [t[:8000] if t else " " for t in texts]
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=valid_texts,
        )
        # 按 index 排序确保顺序正确
        result = [None] * len(texts)
        for item in response.data:
            result[item.index] = item.embedding
        return result
    except Exception as e:
        logger.error(f"[Embedding] 批量调用失败: {e}")
        return [[0.0] * EMBEDDING_DIM for _ in texts]


# ========== RAG 语义搜索 ==========

def rag_upsert(library: str, doc_id: str, content: str, metadata: dict = None):
    """
    插入或更新RAG文档（带embedding）
    """
    vec = embed(content)
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rag_documents (library, doc_id, content, metadata, embedding, updated_at)
            VALUES (%s, %s, %s, %s, %s::vector, NOW())
            ON CONFLICT (library, doc_id) DO UPDATE SET
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
        """, (library, doc_id, content, Json(metadata or {}), str(vec)))
    logger.info(f"[RAG] upsert: {library}/{doc_id}")


def rag_search(query: str, library: str = None, top_k: int = 5,
               metadata_filter: dict = None) -> list:
    """
    语义搜索RAG库
    返回 [{doc_id, content, metadata, similarity}, ...]
    """
    vec = embed(query)
    conn = get_connection()

    # 构建WHERE条件
    conditions = []
    params = []

    if library:
        conditions.append("library = %s")
        params.append(library)

    if metadata_filter:
        for key, value in metadata_filter.items():
            conditions.append("metadata->>%s = %s")
            params.extend([key, str(value)])

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # 参数顺序：SELECT的向量 → WHERE参数 → ORDER BY的向量 → LIMIT
    sql = f"""
        SELECT doc_id, content, metadata,
               1 - (embedding <=> %s::vector) AS similarity
        FROM rag_documents
        {where_clause}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """

    params_final = [str(vec)] + params + [str(vec), top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params_final)
        rows = cur.fetchall()

    return [
        {
            "doc_id": row[0],
            "content": row[1],
            "metadata": row[2],
            "similarity": round(float(row[3]), 4) if row[3] else 0.0,
        }
        for row in rows
    ]


def rag_keyword_search(query: str, library: str = None, top_k: int = 10) -> list:
    """
    关键词搜索（兼容旧接口，作为语义搜索的补充）
    """
    conn = get_connection()
    conditions = ["content ILIKE %s"]
    params = [f"%{query}%"]

    if library:
        conditions.append("library = %s")
        params.append(library)

    params.append(top_k)

    sql = f"""
        SELECT doc_id, content, metadata
        FROM rag_documents
        WHERE {' AND '.join(conditions)}
        LIMIT %s
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {
            "doc_id": row[0],
            "content": row[1],
            "metadata": row[2],
        }
        for row in rows
    ]


# ========== 词典 ==========

def dict_upsert(dict_type: str, key: str, value: dict):
    """插入或更新词典条目（含版本记录）"""
    vec = embed(f"{key} {json.dumps(value, ensure_ascii=False)}")
    conn = get_connection()
    with conn.cursor() as cur:
        # 查询当前版本
        cur.execute("""
            SELECT value, version FROM dictionaries
            WHERE dict_type = %s AND key = %s
        """, (dict_type, key))
        existing = cur.fetchone()

        if existing:
            old_value = existing[0]
            new_version = existing[1] + 1
            # 保存版本历史
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dictionary_versions (
                    version_id SERIAL PRIMARY KEY,
                    dict_type VARCHAR(50) NOT NULL,
                    key VARCHAR(200) NOT NULL,
                    old_value JSONB,
                    new_value JSONB NOT NULL,
                    version INT NOT NULL,
                    change_type VARCHAR(20) DEFAULT 'update',
                    changed_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                INSERT INTO dictionary_versions
                    (dict_type, key, old_value, new_value, version, change_type)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (dict_type, key, Json(old_value), Json(value), new_version, "update"))
        else:
            new_version = 1
            # 创建版本记录
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dictionary_versions (
                    version_id SERIAL PRIMARY KEY,
                    dict_type VARCHAR(50) NOT NULL,
                    key VARCHAR(200) NOT NULL,
                    old_value JSONB,
                    new_value JSONB NOT NULL,
                    version INT NOT NULL,
                    change_type VARCHAR(20) DEFAULT 'update',
                    changed_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                INSERT INTO dictionary_versions
                    (dict_type, key, old_value, new_value, version, change_type)
                VALUES (%s, %s, NULL, %s, %s, 'create')
            """, (dict_type, key, Json(value), new_version))

        # 更新词典表（带版本号）
        cur.execute("""
            INSERT INTO dictionaries (dict_type, key, value, embedding, version)
            VALUES (%s, %s, %s, %s::vector, %s)
            ON CONFLICT (dict_type, key) DO UPDATE SET
                value = EXCLUDED.value,
                embedding = EXCLUDED.embedding,
                version = EXCLUDED.version
        """, (dict_type, key, Json(value), str(vec), new_version))


def dict_search(dict_type: str, query: str, top_k: int = 5) -> list:
    """语义搜索词典"""
    vec = embed(query)
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT key, value, 1 - (embedding <=> %s::vector) AS similarity
            FROM dictionaries
            WHERE dict_type = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (str(vec), dict_type, str(vec), top_k))
        rows = cur.fetchall()

    return [
        {
            "key": row[0],
            "value": row[1],
            "similarity": round(float(row[2]), 4) if row[2] else 0.0,
        }
        for row in rows
    ]


def dict_get_all(dict_type: str) -> list:
    """获取词典全部条目（含版本号）"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT key, value, version FROM dictionaries WHERE dict_type = %s", (dict_type,))
        rows = cur.fetchall()
    return [{"key": row[0], "value": row[1], "version": row[2]} for row in rows]


def dict_get_version_history(dict_type: str, key: str = None) -> list:
    """获取词典版本变更历史"""
    conn = get_connection()
    with conn.cursor() as cur:
        if key:
            cur.execute("""
                SELECT version_id, dict_type, key, old_value, new_value,
                       version, change_type, changed_at
                FROM dictionary_versions
                WHERE dict_type = %s AND key = %s
                ORDER BY version DESC
                LIMIT 50
            """, (dict_type, key))
        else:
            cur.execute("""
                SELECT version_id, dict_type, key, old_value, new_value,
                       version, change_type, changed_at
                FROM dictionary_versions
                WHERE dict_type = %s
                ORDER BY changed_at DESC
                LIMIT 100
            """, (dict_type,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def dict_diff_versions(dict_type: str, key: str, v1: int, v2: int) -> dict:
    """
    对比词典条目的两个版本差异

    Returns:
        {key, old, new, added_fields, removed_fields, changed_fields, change_type}
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT old_value, new_value, change_type
            FROM dictionary_versions
            WHERE dict_type = %s AND key = %s AND version IN (%s, %s)
            ORDER BY version
        """, (dict_type, key, v1, v2))
        rows = cur.fetchall()

    if len(rows) < 2:
        return {"key": key, "error": "版本不存在或数量不足"}

    old_data = rows[1][0] or {}  # v2的old_value即v1时的值
    new_data = rows[1][1] or {}

    # 逐字段对比
    old_keys = set(old_data.keys()) if isinstance(old_data, dict) else set()
    new_keys = set(new_data.keys()) if isinstance(new_data, dict) else set()

    added = {k: new_data[k] for k in new_keys - old_keys}
    removed = {k: old_data[k] for k in old_keys - new_keys}
    changed = {}
    for k in old_keys & new_keys:
        if old_data[k] != new_data[k]:
            changed[k] = {"old": old_data[k], "new": new_data[k]}

    return {
        "key": key,
        "dict_type": dict_type,
        "v1": v1,
        "v2": v2,
        "change_type": rows[1][2] or "unknown",
        "has_changes": bool(added or removed or changed),
        "added_fields": added,
        "removed_fields": removed,
        "changed_fields": changed,
    }


def dict_get_current_version(dict_type: str) -> int:
    """获取词典当前最大版本号"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(MAX(version), 0) FROM dictionary_versions
            WHERE dict_type = %s
        """, (dict_type,))
        return cur.fetchone()[0]


# ========== Pipeline 持久化 ==========

def save_pipeline_run(run_id: str, video_id: str, stages: list,
                      total_cost: float = 0, total_latency: int = 0):
    """保存Pipeline运行结果"""
    conn = get_connection()
    with conn.cursor() as cur:
        # 更新 run
        cur.execute("""
            INSERT INTO pipeline_runs (run_id, video_id, status, completed_at, total_cost_usd, total_latency_ms)
            VALUES (%s, %s, 'completed', NOW(), %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'completed',
                completed_at = NOW(),
                total_cost_usd = %s,
                total_latency_ms = %s
        """, (run_id, video_id, total_cost, total_latency, total_cost, total_latency))

        # 插入各阶段
        for s in stages:
            cur.execute("""
                INSERT INTO pipeline_stages
                    (run_id, stage, provider, model, status, result, cost_usd, latency_ms,
                     cross_validation_result, cross_validation_provider, cross_validation_model,
                     started_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """, (
                run_id,
                s.get("stage", ""),
                s.get("provider", ""),
                s.get("model", ""),
                s.get("status", ""),
                Json(s.get("result", {})),
                s.get("cost_usd", 0),
                s.get("latency_ms", 0),
                Json(s.get("cross_validation_result", {})),
                s.get("cross_validation_provider", ""),
                s.get("cross_validation_model", ""),
            ))


def get_pipeline_run(run_id: str) -> Optional[dict]:
    """查询Pipeline运行结果"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        run = dict(zip(columns, row))

        cur.execute("SELECT * FROM pipeline_stages WHERE run_id = %s ORDER BY stage", (run_id,))
        stage_rows = cur.fetchall()
        stage_columns = [desc[0] for desc in cur.description]
        run["stages"] = [dict(zip(stage_columns, r)) for r in stage_rows]

    return run


# ========== 脚本持久化 ==========

def save_script(script_id: str, content: dict, video_id: str = None,
                product_id: str = None) -> str:
    """保存脚本（自动版本递增）"""
    conn = get_connection()
    with conn.cursor() as cur:
        # 检查是否已存在
        cur.execute("SELECT version FROM scripts WHERE script_id = %s", (script_id,))
        row = cur.fetchone()
        if row:
            old_version = row[0]
            new_version = old_version + 1
            # 保存旧版本到 script_versions
            cur.execute("""
                INSERT INTO script_versions (script_id, version, diff)
                VALUES (%s, %s, %s)
            """, (script_id, old_version, Json({"from_version": old_version})))
            # 更新主记录
            cur.execute("""
                UPDATE scripts SET content = %s, version = %s, updated_at = NOW()
                WHERE script_id = %s
            """, (Json(content), new_version, script_id))
        else:
            cur.execute("""
                INSERT INTO scripts (script_id, video_id, product_id, content, version)
                VALUES (%s, %s, %s, %s, 1)
            """, (script_id, video_id, product_id, Json(content)))

    return script_id


def get_script(script_id: str) -> Optional[dict]:
    """获取脚本"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM scripts WHERE script_id = %s", (script_id,))
        row = cur.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cur.description]
        return dict(zip(columns, row))


# ========== 对话持久化 ==========

def save_session(session_id: str):
    """创建/更新会话"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sessions (session_id, last_active)
            VALUES (%s, NOW())
            ON CONFLICT (session_id) DO UPDATE SET last_active = NOW()
        """, (session_id,))


def save_message(session_id: str, role: str, content: str,
                 skill_name: str = None, tool_calls: list = None):
    """保存消息"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO messages (session_id, role, content, skill_name, tool_calls)
            VALUES (%s, %s, %s, %s, %s)
        """, (session_id, role, content, skill_name, Json(tool_calls) if tool_calls else None))
        # 更新会话活跃时间
        cur.execute("""
            UPDATE sessions SET last_active = NOW() WHERE session_id = %s
        """, (session_id,))


def get_session_history(session_id: str, limit: int = 50) -> list:
    """获取会话历史"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT role, content, skill_name, tool_calls, created_at
            FROM messages
            WHERE session_id = %s
            ORDER BY created_at ASC
            LIMIT %s
        """, (session_id, limit))
        rows = cur.fetchall()
    return [
        {
            "role": row[0],
            "content": row[1],
            "skill_name": row[2],
            "tool_calls": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
        }
        for row in rows
    ]


# ========== 成本持久化 ==========

def save_cost_record(provider: str, model: str, stage: str,
                     input_tokens: int, output_tokens: int,
                     cost_usd: float, latency_ms: int,
                     success: bool = True, error: str = None):
    """保存成本记录"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cost_records
                (provider, model, stage, input_tokens, output_tokens,
                 cost_usd, latency_ms, success, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (provider, model, stage, input_tokens, output_tokens,
              cost_usd, latency_ms, success, error))


def get_cost_summary(hours: int = 24) -> dict:
    """获取成本汇总"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                provider,
                model,
                COUNT(*) as call_count,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(cost_usd) as total_cost,
                AVG(latency_ms) as avg_latency
            FROM cost_records
            WHERE created_at >= NOW() - INTERVAL '%s hours'
            GROUP BY provider, model
            ORDER BY total_cost DESC
        """, (hours,))
        rows = cur.fetchall()

    return [
        {
            "provider": row[0],
            "model": row[1],
            "call_count": row[2],
            "total_input_tokens": row[3],
            "total_output_tokens": row[4],
            "total_cost": float(row[5]) if row[5] else 0,
            "avg_latency": int(row[6]) if row[6] else 0,
        }
        for row in rows
    ]


# ========== 合规持久化 ==========

def save_compliance_check(script_id: str, risk_level: str,
                          original_text: str, replacement: str = None,
                          regulation: str = None, dict_versions: dict = None):
    """保存合规检查记录（含词典版本号）"""
    conn = get_connection()
    with conn.cursor() as cur:
        # 确保dict_versions列存在
        try:
            cur.execute("""
                ALTER TABLE compliance_checks ADD COLUMN IF NOT EXISTS
                dict_versions JSONB
            """)
        except Exception:
            conn.rollback()

        cur.execute("""
            INSERT INTO compliance_checks
                (script_id, risk_level, original_text, replacement, regulation, dict_versions)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (script_id, risk_level, original_text, replacement, regulation,
              Json(dict_versions) if dict_versions else None))


def find_affected_compliance_checks(keyword: str, dict_type: str = "banned") -> list:
    """
    词典新增词条后，扫描历史合规检查中受影响的记录。
    用于 Novel 批准后自动标记老数据为过期。

    Args:
        keyword: 新增的词条文本
        dict_type: 词典类型（banned/compliant）

    Returns:
        受影响的 compliance_check 列表
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT check_id, script_id, original_text, dict_versions
            FROM compliance_checks
            WHERE original_text ILIKE %s
            ORDER BY created_at DESC
            LIMIT 200
        """, (f"%{keyword}%",))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_compliance_stale(check_ids: list[int]) -> int:
    """标记合规检查记录为过期（词典变更导致）"""
    if not check_ids:
        return 0
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE compliance_checks
            SET status = 'stale_recheck', resolved_at = NULL
            WHERE check_id = ANY(%s) AND status != 'resolved'
        """, (check_ids,))
        count = cur.rowcount
        conn.commit()
    return count


def get_compliance_history(script_id: str = None, limit: int = 50) -> list:
    """
    获取合规检查历史（按时间排序，显示词典版本变化）
    如果指定 script_id，只返回该脚本的历史
    """
    conn = get_connection()
    with conn.cursor() as cur:
        if script_id:
            cur.execute("""
                SELECT check_id, script_id, risk_level, original_text, replacement,
                       regulation, status, dict_versions, created_at, resolved_at
                FROM compliance_checks
                WHERE script_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (script_id, limit))
        else:
            cur.execute("""
                SELECT check_id, script_id, risk_level, original_text, replacement,
                       regulation, status, dict_versions, created_at, resolved_at
                FROM compliance_checks
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_script_ids_with_compliance(limit: int = 20) -> list:
    """获取有合规检查记录的脚本ID列表"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT script_id, COUNT(*) as check_count,
                   MAX(created_at) as last_checked
            FROM compliance_checks
            WHERE script_id IS NOT NULL AND script_id != ''
            GROUP BY script_id
            ORDER BY last_checked DESC
            LIMIT %s
        """, (limit,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def compare_compliance_versions(script_id: str) -> dict:
    """
    对比同一脚本在不同时间点的合规检查结果。
    按 created_at 分组（同一批次同一 dict_versions），返回每组的差异。
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT check_id, risk_level, original_text, replacement,
                   regulation, dict_versions, created_at
            FROM compliance_checks
            WHERE script_id = %s
            ORDER BY created_at ASC
        """, (script_id,))
        rows = cur.fetchall()

    if len(rows) < 2:
        return {
            "script_id": script_id,
            "total_checks": len(rows),
            "message": "仅有一次合规检查记录，无法对比",
            "versions": []
        }

    # 按 dict_versions 分组
    groups = {}
    for r in rows:
        dv = r[5]  # dict_versions
        key = json.dumps(dv, sort_keys=True) if dv else "unknown"
        if key not in groups:
            groups[key] = {
                "dict_versions": dv,
                "count": 0,
                "risks": {"pass": 0, "warning": 0, "error": 0},
                "words": [],
                "first_check": r[6].isoformat() if r[6] else None,
                "last_check": r[6].isoformat() if r[6] else None,
            }
        g = groups[key]
        g["count"] += 1
        g["risks"][r[1] or "pass"] = g["risks"].get(r[1], 0) + 1
        if r[2]:
            g["words"].append(r[2])
        g["last_check"] = r[6].isoformat() if r[6] else g["first_check"]

    versions = list(groups.values())
    return {
        "script_id": script_id,
        "total_checks": len(rows),
        "version_count": len(versions),
        "versions": versions,
    }


def get_dict_monitor_stats() -> dict:
    """获取词典监控统计数据"""
    conn = get_connection()
    with conn.cursor() as cur:
        # 词典体积
        cur.execute("""
            SELECT dict_type, COUNT(*) as count, MAX(version) as max_version
            FROM dictionaries
            GROUP BY dict_type
            ORDER BY dict_type
        """)
        dict_volumes = [
            {"dict_type": r[0], "count": r[1], "latest_version": r[2]}
            for r in cur.fetchall()
        ]

        # 最近变更（版本历史）
        cur.execute("""
            SELECT dict_type, key, version, change_type, changed_at
            FROM dictionary_versions
            ORDER BY changed_at DESC
            LIMIT 20
        """)
        recent_changes = [
            {"dict_type": r[0], "key": r[1], "version": r[2],
             "change_type": r[3], "changed_at": r[4].isoformat() if r[4] else None}
            for r in cur.fetchall()
        ]

        # Novel统计
        cur.execute("""
            SELECT status, COUNT(*) FROM novel_tag_candidates GROUP BY status
        """)
        novel_stats = {r[0]: r[1] for r in cur.fetchall()}

        # 过期合规检查数
        cur.execute("SELECT COUNT(*) FROM compliance_checks WHERE status = 'stale_recheck'")
        stale_count = cur.fetchone()[0]

    return {
        "dict_volumes": dict_volumes,
        "recent_changes": recent_changes,
        "novel_stats": novel_stats,
        "stale_compliance_checks": stale_count,
    }


# ========== Feedback 持久化 ==========

def save_feedback(script_id: str, feedback_type: str, content: dict,
                  metric_name: str = None, metric_value: float = None):
    """保存反馈数据"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO feedback (script_id, type, content, metric_name, metric_value)
            VALUES (%s, %s, %s, %s, %s)
        """, (script_id, feedback_type, Json(content), metric_name, metric_value))


def get_feedback(script_id: str) -> list:
    """获取脚本的反馈数据"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT type, content, metric_name, metric_value, created_at
            FROM feedback
            WHERE script_id = %s
            ORDER BY created_at DESC
        """, (script_id,))
        rows = cur.fetchall()
    return [
        {
            "type": row[0],
            "content": row[1],
            "metric_name": row[2],
            "metric_value": float(row[3]) if row[3] else None,
            "created_at": row[4].isoformat() if row[4] else None,
        }
        for row in rows
    ]


# ========== 初始化 ==========

def init_db():
    """初始化数据库：运行迁移并检查pgvector扩展。"""
    from agent.migrations import run_migrations

    applied = run_migrations()
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        version = row[0] if row else "unknown"
    logger.info(f"[DB] 初始化完成, pgvector版本: {version}, applied_migrations={applied}")
    return {"ok": True, "pgvector_version": version, "applied_migrations": applied}


if __name__ == "__main__":
    # 测试连接
    print("测试数据库连接...")
    init_db()
    print("测试Embedding API...")
    vec = embed("保健品TikTok视频钩子设计")
    print(f"  向量维度: {len(vec)}, 前5维: {vec[:5]}")
    print("数据库和Embedding均正常！")
