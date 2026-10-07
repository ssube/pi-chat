-- Duration of each observed tool call in milliseconds, recorded by the owner's
-- capture extension. Historical rows and provider/session records stay NULL.
-- Same occurrence table as version 3: the existing ingester INSERT grant
-- covers the new column, and this migration makes no grants.
ALTER TABLE pi_tool_call.pi_tool_call_occurrence
    ADD COLUMN duration_ms integer;
ALTER TABLE pi_tool_call.pi_tool_call_occurrence
    ADD CONSTRAINT pi_tool_call_occurrence_duration_ms_nonnegative
    CHECK (duration_ms IS NULL OR duration_ms >= 0);
