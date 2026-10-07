-- Keep the suggested fix IDs attached to each observed error, even if a fix
-- is later retired from the catalog. An occurrence may have agent_twol matches.
CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_suggestion (
    occurrence_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_occurrence(id),
    fix_id text NOT NULL,
    suggested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (occurrence_id, fix_id)
);
