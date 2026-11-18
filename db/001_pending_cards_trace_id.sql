-- The turn that created the card, so its audit entries lead back to it.
alter table pending_cards add column if not exists trace_id text;
