"""
难点5：第三方数据源接入 + 脏数据隔离 — 独立演示脚本
模拟"B站抓取 → 字段归一化 → hash去重 → 一致性校验 → staging落库"全流程

用法：
    cd D:\ai-director-agent
    python scripts/data_ingestion_demo.py

前提：
    - PostgreSQL 已启动 (docker-compose up -d)
    - (可选) autocli 已安装，会用真实B站数据；否则用内置mock数据
"""

import json
import hashlib
import time
import subprocess
import os
import sys
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

# 把项目根目录加入路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ========== 配置 ==========

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB", "ai_director"),
    "user": os.getenv("PG_USER", "director"),
    "password": os.getenv("PG_PASSWORD", "director123"),
}

# 统一的视频字段Schema（无论来自B站/TikTok/YouTube都转成这个）
UNIFIED_SCHEMA = {
    "source": "",           # "bilibili" / "tiktok" / "youtube"
    "video_id": "",         # 平台原始ID
    "url": "",              # 视频链接
    "title": "",            # 标题
    "description": "",      # 简介
    "duration_sec": 0,      # 时长(秒)
    "views": 0,             # 播放量
    "likes": 0,             # 点赞
    "comments": 0,          # 评论
    "shares": 0,            # 转发/收藏
    "publish_date": "",     # 发布日期
    "author": "",           # UP主/创作者
    "author_followers": 0,  # 粉丝数
    "tags": [],             # 标签
    "thumbnail_url": "",    # 封面图
    "raw_data": {},         # 原始数据（保留以备追溯）
    "ingested_at": "",      # 入库时间
    "quality_flag": "ok",   # 数据质量标记: ok / warning / dirty
}


@dataclass
class IngestionStats:
    fetched: int = 0
    deduped: int = 0
    validated_ok: int = 0
    validated_warn: int = 0
    validated_dirty: int = 0
    staged: int = 0
    errors: list = field(default_factory=list)


# ========== 第一步：数据抓取 ==========

def check_autocli_available() -> bool:
    """检查 autocli 是否已安装"""
    try:
        subprocess.run(["autocli", "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def fetch_bilibili_hot_real(limit: int = 10) -> list[dict]:
    """通过 autocli 抓取 B站 热门视频（真实数据）"""
    try:
        result = subprocess.run(
            ["autocli", "bilibili", "hot", "--limit", str(limit), "--format", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"autocli failed: {result.stderr}")
        return json.loads(result.stdout)
    except Exception as e:
        print(f"  ⚠️ autocli 抓取失败: {e}，降级为模拟数据")
        return []


def fetch_bilibili_search_real(keyword: str, limit: int = 10) -> list[dict]:
    """通过 autocli 搜索 B站 视频（真实数据）"""
    try:
        result = subprocess.run(
            ["autocli", "bilibili", "search", keyword, "--limit", str(limit), "--format", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"autocli failed: {result.stderr}")
        return json.loads(result.stdout)
    except Exception as e:
        print(f"  ⚠️ autocli 搜索失败: {e}，降级为模拟数据")
        return []


def generate_mock_videos(count: int = 15) -> list[dict]:
    """生成模拟B站/TikTok视频数据（含脏数据）"""
    mock_data = [
        {"video_id": "BV1xx411c7mD", "source": "bilibili", "title": "保健品真的有用吗？深度测评",
         "views": 523000, "likes": 45100, "comments": 3200, "shares": 8900, "duration_sec": 487,
         "author": "测评大师", "author_followers": 250000, "publish_date": "2026-05-20",
         "tags": ["保健品", "测评", "科普"], "url": "https://bilibili.com/video/BV1xx411c7mD"},

        {"video_id": "BV1xx411c7mD", "source": "bilibili", "title": "保健品真的有用吗？深度测评",
         "views": 523500, "likes": 45200, "comments": 3210, "shares": 8950, "duration_sec": 487,
         "author": "测评大师", "author_followers": 251000, "publish_date": "2026-05-20",
         "tags": ["保健品", "测评", "科普"], "url": "https://bilibili.com/video/BV1xx411c7mD"},

        {"video_id": "BV2yy822e8nE", "source": "bilibili", "title": "这瓶鱼油我吃了30天 | 真实体验",
         "views": 1280000, "likes": 89000, "comments": 12400, "shares": 23400, "duration_sec": 521,
         "author": "健康生活家", "author_followers": 520000, "publish_date": "2026-05-18",
         "tags": ["鱼油", "体验", "保健品"], "url": "https://bilibili.com/video/BV2yy822e8nE"},

        {"video_id": "BV3zz933f9oF", "source": "bilibili", "title": "益生菌空腹吃还是饭后吃？90%的人都错了",
         "views": 456000, "likes": 32100, "comments": 5600, "shares": 7800, "duration_sec": 345,
         "author": "营养师小王", "author_followers": 180000, "publish_date": "2026-05-15",
         "tags": ["益生菌", "科普", "营养"], "url": "https://bilibili.com/video/BV3zz933f9oF"},

        {"video_id": "BV4aa044h0pG", "source": "bilibili", "title": "我妈吃了三年褪黑素，结果...",
         "views": 3100000, "likes": 245000, "comments": 35000, "shares": 67000, "duration_sec": 612,
         "author": "真相挖掘机", "author_followers": 890000, "publish_date": "2026-05-10",
         "tags": ["褪黑素", "体验", "真相"], "url": "https://bilibili.com/video/BV4aa044h0pG"},

        {"video_id": "BV5bb155i1qH", "source": "bilibili", "title": "健身补剂智商税大盘点",
         "views": 890000, "likes": 76000, "comments": 8900, "shares": 12300, "duration_sec": 756,
         "author": "健身老炮", "author_followers": 420000, "publish_date": "2026-05-08",
         "tags": ["健身", "补剂", "智商税"], "url": "https://bilibili.com/video/BV5bb155i1qH"},

        # ---- 脏数据 ----
        {"video_id": "", "source": "bilibili", "title": "", "views": -1, "likes": 0,
         "comments": 0, "shares": 0, "duration_sec": 0, "author": "", "author_followers": 0,
         "publish_date": "", "tags": [], "url": ""},

        {"video_id": "BV_dirty_001", "source": "tiktok", "title": None, "views": None,
         "likes": None, "comments": 0, "shares": 0, "duration_sec": -5, "author": None,
         "author_followers": 0, "publish_date": "2099-99-99", "tags": [], "url": ""},

        {"video_id": "BV_dup_003", "source": "bilibili", "title": "重复视频去重测试",
         "views": 1000, "likes": 50, "comments": 10, "shares": 5, "duration_sec": 120,
         "author": "测试账号", "author_followers": 100, "publish_date": "2026-01-01",
         "tags": ["测试"], "url": "https://bilibili.com/video/BV_dup_003"},

        {"video_id": "BV_dup_003", "source": "bilibili", "title": "重复视频去重测试",
         "views": 1050, "likes": 55, "comments": 12, "shares": 6, "duration_sec": 120,
         "author": "测试账号", "author_followers": 100, "publish_date": "2026-01-01",
         "tags": ["测试"], "url": "https://bilibili.com/video/BV_dup_003"},
    ]
    # 补足到 count 个
    return mock_data[:count]


# ========== 第二步：字段归一化 ==========

def normalize_to_unified(raw: dict, source: str) -> dict:
    """
    将不同平台的原始数据统一为 UNIFIED_SCHEMA 格式。
    处理字段缺失、类型转换、格式差异。
    """
    unified = dict(UNIFIED_SCHEMA)
    unified["source"] = source
    unified["raw_data"] = raw
    unified["ingested_at"] = datetime.now().isoformat()

    # 字段映射 + 安全类型转换
    field_map = {
        "video_id": str(raw.get("video_id") or raw.get("bvid") or raw.get("id") or ""),
        "url": str(raw.get("url") or raw.get("short_link") or f"https://bilibili.com/video/{raw.get('bvid', '')}" or ""),
        "title": str(raw.get("title") or ""),
        "description": str(raw.get("description") or raw.get("desc") or ""),
        "duration_sec": _safe_int(raw.get("duration_sec") or raw.get("duration") or raw.get("length"), 0),
        "views": _safe_int(raw.get("views") or raw.get("view") or raw.get("play"), 0),
        "likes": _safe_int(raw.get("likes") or raw.get("like") or raw.get("stat", {}).get("like", 0), 0),
        "comments": _safe_int(raw.get("comments") or raw.get("comment") or raw.get("reply"), 0),
        "shares": _safe_int(raw.get("shares") or raw.get("share") or raw.get("favorite"), 0),
        "publish_date": _safe_date(raw.get("publish_date") or raw.get("pubdate") or raw.get("created")),
        "author": str(raw.get("author") or raw.get("owner", {}).get("name", "")),
        "author_followers": _safe_int(raw.get("author_followers") or raw.get("follower") or raw.get("owner", {}).get("follower", 0), 0),
        "tags": raw.get("tags") if isinstance(raw.get("tags"), list) else [],
        "thumbnail_url": str(raw.get("thumbnail_url") or raw.get("pic") or raw.get("cover") or ""),
    }

    for key, default in field_map.items():
        if isinstance(default, list):
            unified[key] = default
        elif isinstance(default, int):
            unified[key] = default if default >= 0 else 0
        elif isinstance(default, str):
            unified[key] = default if default else ""

    return unified


def _safe_int(val, default=0):
    try:
        v = int(val)
        return v if v >= 0 else 0
    except (TypeError, ValueError):
        return default


def _safe_date(val):
    if not val:
        return ""
    try:
        # 尝试解析常见格式
        from datetime import datetime as dt
        for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y%m%d"]:
            try:
                d = dt.strptime(str(val)[:10], fmt[:10])
                return d.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return str(val)[:10]
    except Exception:
        return ""


# ========== 第三步：Hash 去重 ==========

def compute_video_hash(video: dict) -> str:
    """计算视频内容哈希（基于 video_id + 来源，不依赖播放量等可变字段）"""
    key = f"{video['source']}:{video['video_id']}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def deduplicate(videos: list[dict]) -> list[dict]:
    """Hash 去重，保留第一次出现的版本"""
    seen = set()
    unique = []
    for v in videos:
        h = compute_video_hash(v)
        if h not in seen:
            seen.add(h)
            unique.append(v)
    return unique


# ========== 第四步：数据质量校验 ==========

def validate_video(video: dict) -> str:
    """
    跨字段一致性校验，返回质量标记。

    规则:
    - 必要字段缺失 → dirty
    - 数值异常（views<0, likes>views） → warning
    - 日期异常 → warning
    - 全部正常 → ok
    """
    issues = []

    # 脏数据：核心字段为空
    if not video["video_id"] or not video["title"]:
        issues.append("必要字段缺失(video_id/title)")
        video["quality_flag"] = "dirty"
        return "dirty"

    # 警告：数值异常
    if video["views"] < 0:
        issues.append(f"播放量异常({video['views']})")
    if video["likes"] > video["views"] and video["views"] > 0:
        issues.append(f"点赞({video['likes']}) > 播放({video['views']})")
    if video["comments"] > video["views"] * 0.5 and video["views"] > 0:
        issues.append(f"评论占比异常(comments/views={video['comments']/video['views']:.2f})")

    # 警告：日期异常
    if video["publish_date"]:
        try:
            pub = datetime.strptime(video["publish_date"], "%Y-%m-%d")
            if pub > datetime.now():
                issues.append(f"发布日期在未来({video['publish_date']})")
        except ValueError:
            issues.append(f"日期格式非法({video['publish_date']})")

    if issues:
        video["quality_flag"] = "warning"
        video["_validation_issues"] = issues
        return "warning"

    video["quality_flag"] = "ok"
    return "ok"


# ========== 第五步：Staging 落库 ==========

def init_staging_tables():
    """创建 staging 表（和主表隔离，脏数据不进主库）"""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staged_videos (
                id SERIAL PRIMARY KEY,
                source VARCHAR(20) NOT NULL,
                video_id VARCHAR(100) NOT NULL,
                video_hash VARCHAR(32) NOT NULL,
                url VARCHAR(500),
                title TEXT,
                description TEXT,
                duration_sec INT DEFAULT 0,
                views BIGINT DEFAULT 0,
                likes BIGINT DEFAULT 0,
                comments BIGINT DEFAULT 0,
                shares BIGINT DEFAULT 0,
                publish_date VARCHAR(20),
                author VARCHAR(200),
                author_followers BIGINT DEFAULT 0,
                tags JSONB DEFAULT '[]',
                thumbnail_url VARCHAR(500),
                raw_data JSONB,
                quality_flag VARCHAR(20) DEFAULT 'ok',
                validation_issues JSONB DEFAULT '[]',
                ingested_at TIMESTAMP DEFAULT NOW(),
                pipeline_run_id VARCHAR(50),
                pipeline_status VARCHAR(20) DEFAULT 'pending'
            )
        """)
        # 索引：加速去重查询
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_staged_videos_hash
            ON staged_videos(video_hash)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_staged_videos_quality
            ON staged_videos(quality_flag) WHERE quality_flag = 'ok'
        """)
    conn.close()
    print("  ✅ staging表就绪")


def stage_video(video: dict, conn) -> bool:
    """单条视频写入 staging 表（on conflict 跳过）"""
    try:
        h = compute_video_hash(video)
        with conn.cursor() as cur:
            # 检查是否已存在（hash 去重）
            cur.execute("SELECT id FROM staged_videos WHERE video_hash = %s", (h,))
            if cur.fetchone():
                return False  # skip

            cur.execute("""
                INSERT INTO staged_videos
                    (source, video_id, video_hash, url, title, description,
                     duration_sec, views, likes, comments, shares, publish_date,
                     author, author_followers, tags, thumbnail_url,
                     raw_data, quality_flag, validation_issues)
                VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s)
            """, (
                video["source"], video["video_id"], h, video["url"],
                video["title"][:500], video["description"][:2000],
                video["duration_sec"], video["views"], video["likes"],
                video["comments"], video["shares"],
                video["publish_date"][:20] if video["publish_date"] else "",
                video["author"][:200], video["author_followers"],
                json.dumps(video["tags"]),
                video["thumbnail_url"][:500] if video["thumbnail_url"] else "",
                json.dumps(video["raw_data"], ensure_ascii=False),
                video["quality_flag"],
                json.dumps(video.get("_validation_issues", []), ensure_ascii=False),
            ))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        return False


# ========== 主流程 ==========

def run():
    print("\n" + "=" * 60)
    print("  难点5：第三方数据源接入 + 脏数据隔离 — 演示")
    print("=" * 60)

    stats = IngestionStats()

    # 0. 初始化
    print("\n[0/5] 初始化 staging 表...")
    try:
        init_staging_tables()
    except Exception as e:
        print(f"  ❌ 数据库连接失败: {e}")
        print("  请先启动 PostgreSQL: docker-compose up -d")
        return

    # 1. 抓取
    print("\n[1/5] 数据抓取...")
    if check_autocli_available():
        print("  ✅ autocli 可用，尝试抓取B站热门...")
        raw_videos = fetch_bilibili_hot_real(10)
        if not raw_videos:
            raw_videos = generate_mock_videos(15)
            print(f"  ⚠️ 抓取失败，使用模拟数据: {len(raw_videos)}条")
        else:
            print(f"  ✅ 真实数据: {len(raw_videos)}条")
    else:
        print("  ⚠️ autocli 未安装，使用模拟数据")
        print("  安装 autocli: https://github.com/nashsu/AutoCLI")
        raw_videos = generate_mock_videos(15)
        print(f"  模拟数据: {len(raw_videos)}条 (含3条脏数据)")

    stats.fetched = len(raw_videos)

    # 2. 归一化
    print("\n[2/5] 字段归一化...")
    unified = [normalize_to_unified(v, v.get("source", "bilibili")) for v in raw_videos]
    print(f"  ✅ 归一化完成: {len(unified)}条 → 统一Schema")

    # 3. 去重
    print("\n[3/5] Hash 去重...")
    before_dedup = len(unified)
    unique = deduplicate(unified)
    stats.deduped = before_dedup - len(unique)
    print(f"  去重前: {before_dedup}条")
    print(f"  去重后: {len(unique)}条")
    if stats.deduped > 0:
        print(f"  🔄 已去除 {stats.deduped}条重复视频")
        for v in unified:
            if v not in unique:
                print(f"     - {v['video_id']}: {v['title'][:30]}")

    # 4. 校验
    print("\n[4/5] 数据质量校验...")
    for v in unique:
        flag = validate_video(v)
        if flag == "ok":
            stats.validated_ok += 1
        elif flag == "warning":
            stats.validated_warn += 1
        else:
            stats.validated_dirty += 1

    print(f"  ✅ OK:      {stats.validated_ok}条 → 直接入库")
    print(f"  ⚠️  WARNING:  {stats.validated_warn}条 → 标记入库")
    print(f"  ❌ DIRTY:    {stats.validated_dirty}条 → 隔离不入库")

    # 5. 落库
    print("\n[5/5] Staging 落库...")
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    for v in unique:
        if v["quality_flag"] == "dirty":
            print(f"  ⛔ 跳过脏数据: {v['video_id'] or '(无ID)'} — {v.get('_validation_issues', [])}")
            continue
        if stage_video(v, conn):
            stats.staged += 1
    conn.close()
    print(f"  ✅ 成功入库: {stats.staged}条")

    # 6. 摘要
    print("\n" + "=" * 60)
    print("  入库摘要")
    print("=" * 60)
    print(f"  抓取总数:   {stats.fetched}")
    print(f"  去重移除:   {stats.deduped}")
    print(f"  质量OK:    {stats.validated_ok}")
    print(f"  质量警告:   {stats.validated_warn}")
    print(f"  质量脏数据: {stats.validated_dirty}")
    print(f"  实际入库:   {stats.staged}")
    if stats.errors:
        print(f"  错误:      {len(stats.errors)}")

    # 查询验证
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT quality_flag, COUNT(*) FROM staged_videos GROUP BY quality_flag")
        print("\n  Staging 表内容:")
        for row in cur.fetchall():
            print(f"    {row[0]:10s}: {row[1]}条")
    conn.close()

    print("\n  ✅ 演示完成！")
    print(f"  查看数据: SELECT * FROM staged_videos ORDER BY ingested_at DESC;\n")


if __name__ == "__main__":
    run()
