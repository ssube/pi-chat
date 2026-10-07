-- Applied by `python3 -m tools.ledger.error_cli migrate`, which records the version/checksum.
CREATE SCHEMA IF NOT EXISTS pi_tool_call;
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_occurrence (
    id text PRIMARY KEY CHECK (id ~ '^(pierr|pitool)-[0-9a-fA-F-]{36}$'),
    agent text NOT NULL CHECK (agent ~ '^[a-z][a-z0-9_-]{0,79}$'),
    origin text NOT NULL,
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL CHECK (kind IN ('tool', 'provider', 'session')),
    outcome text NOT NULL CHECK (outcome IN ('error', 'success')),
    tool_name text,
    signature text NOT NULL,
    detail_ref text,
    detail_sha256 text CHECK (detail_sha256 ~ '^[0-9a-f]{64}$'),
    redacted_summary text NOT NULL,
    reviewed_at timestamptz,
    reviewed_by text,
    review_note text,
    resolved_at timestamptz,
    resolved_by_pr text,
    CHECK ((detail_ref IS NULL) = (detail_sha256 IS NULL)),
    CHECK (outcome <> 'success' OR detail_ref IS NULL),
    CHECK ((reviewed_at IS NULL) = (reviewed_by IS NULL)),
    CHECK ((resolved_at IS NULL) = (resolved_by_pr IS NULL))
);
CREATE INDEX IF NOT EXISTS pi_tool_call_occurrence_review_queue
    ON pi_tool_call.pi_tool_call_occurrence (occurred_at) WHERE outcome = 'error' AND reviewed_at IS NULL;
CREATE INDEX IF NOT EXISTS pi_tool_call_occurrence_trend
    ON pi_tool_call.pi_tool_call_occurrence (signature, occurred_at DESC) WHERE outcome = 'error';

CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_known_fix (
    id text PRIMARY KEY,
    title text NOT NULL,
    signature text NOT NULL,
    match_pattern text NOT NULL,
    redacted_instruction text NOT NULL,
    approved_digest text NOT NULL CHECK (approved_digest ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz NOT NULL,
    approved_by text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    cooldown_seconds integer NOT NULL DEFAULT 3600 CHECK (cooldown_seconds >= 0),
    source text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_fix_owner (
    fix_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_known_fix(id) ON DELETE CASCADE,
    agent text NOT NULL CHECK (agent ~ '^[a-z][a-z0-9_-]{0,79}$'),
    PRIMARY KEY (fix_id, agent)
);
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_fix_applies_to (
    fix_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_known_fix(id) ON DELETE CASCADE,
    agent text NOT NULL CHECK (agent ~ '^[a-z][a-z0-9_-]{0,79}$'),
    PRIMARY KEY (fix_id, agent)
);
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_fix_required_skill (
    fix_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_known_fix(id) ON DELETE CASCADE,
    skill text NOT NULL,
    PRIMARY KEY (fix_id, skill)
);
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_occurrence_fix (
    occurrence_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_occurrence(id),
    fix_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_known_fix(id),
    PRIMARY KEY (occurrence_id, fix_id)
);
