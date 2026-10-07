-- Normalized, redacted Bash metadata. Owner-local detail files never enter these tables.
CREATE TABLE pi_tool_call.pi_command_parse (
    occurrence_id text PRIMARY KEY REFERENCES pi_tool_call.pi_tool_call_occurrence(id) ON DELETE CASCADE,
    parser_version integer NOT NULL,
    parse_status text NOT NULL CHECK (parse_status IN ('parsed', 'unsupported')),
    whole_form text,
    final_command_name text,
    final_command_form text,
    step_count integer NOT NULL DEFAULT 0,
    operators text[] NOT NULL DEFAULT '{}',
    options text[] NOT NULL DEFAULT '{}',
    arg_roles text[] NOT NULL DEFAULT '{}',
    prior_command text,
    same_resource boolean NOT NULL DEFAULT false,
    execution_uncertain boolean NOT NULL DEFAULT false,
    stderr_suppressed boolean NOT NULL DEFAULT false,
    exit_code integer,
    error_class text NOT NULL DEFAULT 'unknown',
    output_present boolean NOT NULL DEFAULT false
);
CREATE INDEX pi_command_parse_lookup
    ON pi_tool_call.pi_command_parse (final_command_name, exit_code, error_class)
    WHERE parse_status = 'parsed';

CREATE TABLE pi_tool_call.pi_command_step (
    occurrence_id text NOT NULL REFERENCES pi_tool_call.pi_command_parse(occurrence_id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    command_name text NOT NULL,
    command_form text NOT NULL,
    options text[] NOT NULL DEFAULT '{}',
    arg_roles text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (occurrence_id, ordinal)
);
CREATE INDEX pi_command_step_name ON pi_tool_call.pi_command_step (command_name);

CREATE TABLE pi_tool_call.pi_fix_example (
    id text PRIMARY KEY,
    fix_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_known_fix(id),
    approved_digest text NOT NULL CHECK (approved_digest ~ '^[0-9a-f]{64}$'),
    approved_by text NOT NULL CHECK (approved_by IN ('agent_one', 'agent_two', 'agent_three')),
    approved_at timestamptz NOT NULL,
    source_occurrence_id text REFERENCES pi_tool_call.pi_tool_call_occurrence(id),
    agents text[] NOT NULL,
    context_note text NOT NULL CHECK (char_length(context_note) <= 500),
    command_name text NOT NULL,
    command_form text NOT NULL,
    whole_form text NOT NULL,
    step_count integer NOT NULL,
    prior_command text,
    same_resource boolean NOT NULL DEFAULT false,
    exit_code integer,
    error_class text NOT NULL,
    output_present boolean NOT NULL,
    options text[] NOT NULL DEFAULT '{}',
    arg_roles text[] NOT NULL DEFAULT '{}',
    intent_sensitive boolean NOT NULL DEFAULT false,
    active boolean NOT NULL DEFAULT true
);
CREATE INDEX pi_fix_example_lookup
    ON pi_tool_call.pi_fix_example (command_name, exit_code, error_class)
    WHERE active;

CREATE TABLE pi_tool_call.pi_match_trial (
    occurrence_id text NOT NULL REFERENCES pi_tool_call.pi_tool_call_occurrence(id) ON DELETE CASCADE,
    fix_id text NOT NULL,
    matcher_version integer NOT NULL,
    score smallint NOT NULL CHECK (score BETWEEN 0 AND 100),
    disposition text NOT NULL CHECK (disposition = 'conditional_hint'),
    shown_to_user boolean NOT NULL,
    matched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (occurrence_id, fix_id, matcher_version)
);
