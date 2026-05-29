-- Initial reproducible schema for AI Director Agent.
-- Idempotent on existing development databases; safe to run more than once.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_documents (
    id SERIAL PRIMARY KEY,
    library VARCHAR NOT NULL,
    doc_id VARCHAR,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding vector(1024),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_rag_lib_doc UNIQUE (library, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_rag_library ON rag_documents (library);
CREATE INDEX IF NOT EXISTS idx_rag_embedding ON rag_documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS dictionaries (
    id SERIAL PRIMARY KEY,
    dict_type VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    value JSONB NOT NULL,
    embedding vector(1024),
    created_at TIMESTAMP DEFAULT NOW(),
    version INTEGER DEFAULT 1,
    CONSTRAINT uq_dict_type_key UNIQUE (dict_type, key)
);
CREATE INDEX IF NOT EXISTS idx_dict_type ON dictionaries (dict_type);

CREATE TABLE IF NOT EXISTS dictionary_versions (
    version_id SERIAL PRIMARY KEY,
    dict_type VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    old_value JSONB,
    new_value JSONB NOT NULL,
    version INTEGER NOT NULL,
    change_type VARCHAR DEFAULT 'update',
    changed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id VARCHAR PRIMARY KEY,
    video_id VARCHAR,
    status VARCHAR DEFAULT 'running',
    started_at TIMESTAMP DEFAULT NOW(),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    total_cost_usd NUMERIC DEFAULT 0,
    total_latency_ms INTEGER DEFAULT 0,
    config JSONB DEFAULT '{}'::jsonb
);
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR REFERENCES pipeline_runs(run_id),
    stage VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'pending',
    result JSONB DEFAULT '{}'::jsonb,
    cost_usd NUMERIC DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    cross_validation_result JSONB DEFAULT '{}'::jsonb,
    cross_validation_provider VARCHAR DEFAULT '',
    cross_validation_model VARCHAR DEFAULT '',
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pstages_run ON pipeline_stages (run_id);

CREATE TABLE IF NOT EXISTS cost_records (
    id SERIAL PRIMARY KEY,
    provider VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    stage VARCHAR,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    success BOOLEAN DEFAULT TRUE,
    error TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cost_time ON cost_records (created_at);

CREATE TABLE IF NOT EXISTS scripts (
    script_id VARCHAR PRIMARY KEY,
    video_id VARCHAR,
    product_id VARCHAR,
    content JSONB NOT NULL,
    version INTEGER DEFAULT 1,
    status VARCHAR DEFAULT 'draft',
    compliance_status VARCHAR DEFAULT 'unchecked',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS script_versions (
    id SERIAL PRIMARY KEY,
    script_id VARCHAR REFERENCES scripts(script_id),
    version INTEGER NOT NULL,
    diff JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    last_active TIMESTAMP DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR REFERENCES sessions(session_id),
    role VARCHAR NOT NULL,
    content TEXT,
    skill_name VARCHAR,
    tool_calls JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_msgs_session ON messages (session_id);

CREATE TABLE IF NOT EXISTS compliance_checks (
    check_id SERIAL PRIMARY KEY,
    script_id VARCHAR,
    risk_level VARCHAR NOT NULL,
    original_text TEXT NOT NULL,
    replacement TEXT,
    regulation VARCHAR,
    status VARCHAR DEFAULT 'pending',
    reviewer VARCHAR,
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    dict_versions JSONB
);
CREATE INDEX IF NOT EXISTS idx_comp_script ON compliance_checks (script_id);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id SERIAL PRIMARY KEY,
    script_id VARCHAR,
    type VARCHAR NOT NULL,
    content JSONB DEFAULT '{}'::jsonb,
    metric_name VARCHAR,
    metric_value NUMERIC,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feedback_script ON feedback (script_id);

CREATE TABLE IF NOT EXISTS prompt_references (
    ref_id SERIAL PRIMARY KEY,
    source_stage VARCHAR NOT NULL,
    source_element_id VARCHAR(100) NOT NULL,
    target_stage VARCHAR NOT NULL,
    target_pipeline_run_id VARCHAR,
    reference_type VARCHAR DEFAULT 'direct',
    element_version INTEGER DEFAULT 1,
    status VARCHAR DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source_element_id, target_stage, target_pipeline_run_id)
);

CREATE TABLE IF NOT EXISTS novel_tag_candidates (
    candidate_id SERIAL PRIMARY KEY,
    tag_text VARCHAR NOT NULL UNIQUE,
    frequency INTEGER DEFAULT 1,
    sources JSONB DEFAULT '[]'::jsonb,
    confidence_score DOUBLE PRECISION DEFAULT 0.0,
    status VARCHAR DEFAULT 'pending',
    decision VARCHAR,
    decided_by VARCHAR,
    decided_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS staged_videos (
    id SERIAL PRIMARY KEY,
    source VARCHAR NOT NULL,
    video_id VARCHAR NOT NULL,
    video_hash VARCHAR NOT NULL,
    url VARCHAR,
    title TEXT,
    description TEXT,
    duration_sec INTEGER DEFAULT 0,
    views BIGINT DEFAULT 0,
    likes BIGINT DEFAULT 0,
    comments BIGINT DEFAULT 0,
    shares BIGINT DEFAULT 0,
    publish_date VARCHAR,
    author VARCHAR,
    author_followers BIGINT DEFAULT 0,
    tags JSONB DEFAULT '[]'::jsonb,
    thumbnail_url VARCHAR,
    raw_data JSONB,
    quality_flag VARCHAR DEFAULT 'ok',
    validation_issues JSONB DEFAULT '[]'::jsonb,
    ingested_at TIMESTAMP DEFAULT NOW(),
    pipeline_run_id VARCHAR,
    pipeline_status VARCHAR DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_staged_videos_hash ON staged_videos (video_hash);
CREATE INDEX IF NOT EXISTS idx_staged_videos_quality ON staged_videos (quality_flag) WHERE quality_flag = 'ok';
