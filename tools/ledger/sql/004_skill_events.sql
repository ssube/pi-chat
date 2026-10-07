-- Only observed Pi skill access metadata. No prompt, file path, session ID, or body.
-- Provision a writer for the whole ingester (occurrences, suggestions, and
-- skill events) before granting INSERT. This migration makes no grants.
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_skill_event (
    id text PRIMARY KEY CHECK (id ~ '^piskill-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    agent text NOT NULL CHECK (agent ~ '^[a-z][a-z0-9_-]{0,79}$'),
    origin text NOT NULL CHECK (origin ~ '^[a-z][a-z0-9_-]{0,79}$'),
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    skill_name text NOT NULL CHECK (length(skill_name) <= 64 AND skill_name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    event_kind text NOT NULL CHECK (event_kind IN ('loaded', 'invoked')),
    source text NOT NULL CHECK ((event_kind = 'loaded' AND source = 'read') OR
                                (event_kind = 'invoked' AND source = 'skill_command'))
);
CREATE INDEX IF NOT EXISTS pi_skill_event_agent_time
    ON pi_tool_call.pi_skill_event (agent, occurred_at DESC);
