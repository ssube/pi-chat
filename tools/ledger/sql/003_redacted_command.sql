-- Tool-call command metadata is redacted before it reaches the shared ledger.
-- Historical rows remain NULL; no raw command or success detail file is inferred.
ALTER TABLE pi_tool_call.pi_tool_call_occurrence
    ADD COLUMN redacted_command text;
ALTER TABLE pi_tool_call.pi_tool_call_occurrence
    ADD CONSTRAINT pi_tool_call_occurrence_command_length
    CHECK (redacted_command IS NULL OR char_length(redacted_command) <= 2048);
