-- The turn that created the card, so its audit entries lead back to it.
alter table pending_cards add column if not exists trace_id text;
-- The turn that asked for a policy decision, so a trace shows the decisions.
alter table policy_events add column if not exists trace_id text;
