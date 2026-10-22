-- Initial schema for the AI Pull-Request Review Agent.
-- This migration creates tables and indexes; it does not drop data.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- Repository code chunks and their embeddings.
CREATE TABLE IF NOT EXISTS code_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    installation_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    symbol TEXT,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    token_count INT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', content)
    ) STORED,
    UNIQUE (installation_id, repo, path, chunk_index)
);

CREATE INDEX IF NOT EXISTS code_chunks_fts_idx
    ON code_chunks USING GIN (content_tsv);

CREATE INDEX IF NOT EXISTS code_chunks_symbol_idx
    ON code_chunks (installation_id, repo, symbol);

-- Per-file indexing records, used to skip unchanged files.
CREATE TABLE IF NOT EXISTS repo_file_index (
    installation_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, repo, path)
);

CREATE TABLE IF NOT EXISTS repo_index_state (
    installation_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    last_indexed_sha TEXT,
    embedding_model TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, repo)
);

-- One review record per installation, repository, PR and head SHA.
CREATE TABLE IF NOT EXISTS pr_review_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    installation_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INT NOT NULL,
    head_sha TEXT NOT NULL,
    delivery_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    overall_confidence NUMERIC(4,3),
    github_review_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (installation_id, repo, pr_number, head_sha)
);

CREATE INDEX IF NOT EXISTS pr_review_records_repo_idx
    ON pr_review_records (installation_id, repo, pr_number, created_at DESC);

-- Validated findings produced by the specialist agents.
CREATE TABLE IF NOT EXISTS finding_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id UUID NOT NULL
        REFERENCES pr_review_records(id) ON DELETE CASCADE,
    agent_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT,
    summary TEXT NOT NULL,
    file_path TEXT,
    line_start INT,
    line_end INT,
    suggestion TEXT,
    confidence NUMERIC(4,3),
    rationale TEXT,
    agreed_by TEXT[] NOT NULL DEFAULT '{}',
    disputed BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS finding_records_review_idx
    ON finding_records (review_id);

-- Human-in-the-loop review queue and feedback.
CREATE TABLE IF NOT EXISTS hitl_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id UUID NOT NULL
        REFERENCES pr_review_records(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_reviews_queue_idx
    ON hitl_reviews (status, created_at);

CREATE TABLE IF NOT EXISTS hitl_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID REFERENCES finding_records(id),
    verdict TEXT NOT NULL,
    comment TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Durable receipt of incoming GitHub webhook deliveries.
CREATE TABLE IF NOT EXISTS webhook_inbox (
    delivery_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    action TEXT,
    installation_id BIGINT,
    repo TEXT,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS webhook_inbox_pending_idx
    ON webhook_inbox (status, created_at);

-- Atomic daily spend accounting for paid model calls.
CREATE TABLE IF NOT EXISTS daily_budget_usage (
    budget_day DATE PRIMARY KEY,
    reserved_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    actual_usd NUMERIC(12,6) NOT NULL DEFAULT 0
);

-- Append-only event and cost history.
CREATE TABLE IF NOT EXISTS agent_events (
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    installation_id BIGINT NOT NULL,
    review_id UUID NOT NULL,
    agent TEXT NOT NULL,
    span_id UUID NOT NULL DEFAULT gen_random_uuid(),
    parent_span UUID,
    event_type TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    tokens_in INT,
    tokens_out INT,
    cost_usd NUMERIC(10,6),
    latency_ms INT,
    outcome TEXT,
    confidence NUMERIC(4,3),
    payload JSONB
);

-- Partition agent events by time.
SELECT create_hypertable(
    'public.agent_events',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS agent_events_review_ts_idx
    ON agent_events (installation_id, review_id, ts DESC);

COMMIT;
