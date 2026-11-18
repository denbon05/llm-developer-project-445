# 04. Audit and telemetry

Every turn leaves a record of what the models and tools did, and every outcome
of a change or an escalation leaves an audit entry, refusals included. Terms:
[glossary](../glossary.md). Cards and execution: [03](03-action-contracts.md).

## Three records

| Record | Where | For | A lost row |
| --- | --- | --- | --- |
| Logs | structlog, stdout | developers, temporary | fine |
| Telemetry | `app/telemetry.py`: `llm_calls`, `tool_calls` | metrics, cost, latency | acceptable |
| Audit | `app/audit.py`: `audit_log` | who did what, with what result, state before and after | a defect |

No record holds message text.

`trace_id` links the three. `start_trace` makes it once per turn and once per
card button press. `session_id` links the turns of one dialogue.
`pending_cards.trace_id` is the turn that created the card, so an entry with
the card's key leads to the turn with the plan and the `prepare_*` call.

Example, anna: «переведи меня на семейный», «да», «да»:

1. `tr_a1`: `llm_calls` planner and `tariff`; `tool_calls` `get_my_tariff` →
   `ok`, `prepare_tariff_change` → `card_created`; card `p_1a2b` with
   `trace_id = tr_a1`; no audit entry, the card waits.
2. `tr_b2`: no model runs; audit `executed`, `before` `"Базовый"`, `after`
   `"Семейный"`, key `tariff_change:p_1a2b`, `card_trace_id` `tr_a1`.
3. `tr_c3`: audit `duplicate_suppressed`, same key.

## Audit module

`app/audit.py`, append-only: it has `append` and reads, no update or delete.

- `AuditOutcome(StrEnum)`: `executed`, `duplicate_suppressed`,
  `rejected_by_user`, `rejected_by_contract`, `expired`, `escalated`,
  `denied`, `guardrail_unsafe`, `guardrail_degraded`, `token_refreshed`,
  `server_limit`, `budget_exhausted`, `error`. `OUTCOMES =
  frozenset(AuditOutcome)`. Later steps write the values this step doesn't;
  the set is fixed in one place.
- `append(who, what, outcome, *, trace_id=None, session_id=None, args=None,
  before=None, after=None, idempotency_key=None, detail=None) -> None`.
  - An outcome not in `OUTCOMES` raises `ValueError` before any SQL.
  - `args`, `before`, `after` are stored as jsonb; `None` is SQL NULL. A
    string stays a JSON string: smoke group 3 compares `before == "Базовый"`.
  - A database error is caught and logged as `audit_write_failed` at error
    level with every field, so the entry can be restored from the log. The
    caller goes on: the change already happened and the reply must say so.
- `tail(limit=20, who=None) -> list[dict]`: the newest `limit` entries, of
  one `who` if given, oldest first. Not `n`: pylint allows no one-letter
  names, and smoke passes only `who`.
- `find_card_entries(idempotency_key) -> list[dict]`: a card's entries, oldest
  first.
- `trace(trace_id) -> list[dict]`: one timeline of the turn from the three
  tables, by `ts`, then `id`. Each item is the row plus `source` (`audit_log`,
  `llm_calls`, `tool_calls`). Every audit item also has `card_trace_id`:
  `pending_cards.trace_id` joined on
  `idempotency_key = action || ':' || pending_id`, `None` when the key
  belongs to no card.

`who` is the subscriber's `sub`. `what` is the action (`tariff_change`,
`service_toggle`, `contacts_update`), or `create_ticket` for a ticket no card
led to.

### Reading

`python -m app.audit [trace_id]` prints a line with the trace id, then one
line per item. With no argument it prints the newest trace: the `trace_id` of
the newest row of the three tables. `make trace T=<trace_id>`, `T` optional.
An audit line shows `detail` when it has one.

A card's story spans several turns. When a trace's audit entries are about a
card, the command prints the trace together with the card's history
(`audit.add_card_histories`): the items of the turn that created the card and
every entry with its key, from all turns. Each item appears once, in time
order, under a `turn <trace_id>` line. Sample, `make trace T=tr_c3` from the
example above:

```
trace tr_c3
turn tr_a1
14:05:12 llm_calls  planner  in=812 out=64  1820 ms
14:05:14 tool_calls prepare_tariff_change  card_created  {"tariff": "семейный", "sub": "u_4f2a"}  95 ms
14:05:15 llm_calls  tariff  in=1617 out=31  2425 ms
turn tr_b2
14:05:20 audit_log  executed  tariff_change  Базовый → Семейный  key=tariff_change:p_1a2b
turn tr_c3
14:05:25 audit_log  duplicate_suppressed  tariff_change  key=tariff_change:p_1a2b
```

Two numbers for the whole system:

- repeats stopped by a known key:
  `select count(*) from audit_log where outcome = 'duplicate_suppressed';`
- operations executed twice, must return nothing:
  `select idempotency_key, count(*) from audit_log where outcome = 'executed' group by 1 having count(*) > 1;`

## Telemetry module

`app/telemetry.py`:

- `record_llm_call(trace_id, agent, model, input_tokens, output_tokens,
  latency_ms) -> None`;
- `record_tool_call(trace_id, tool, args, outcome, latency_ms) -> None`;
- a database error is caught and logged as `telemetry_write_failed`, warning
  level, with the table and the error type. The caller goes on.

### llm_calls

`run_agent` writes one row after each run: the agent's name, its model,
`usage.input_tokens`, `usage.output_tokens`, the run's latency in ms. The
trace id is the one `start_trace` binds to the log context (structlog
contextvars), read with `core.logging.read_trace_id()`. The planner, workers
and editor don't pass it. A run that raises writes no row.

### tool_calls

The tool wrapper writes one row per model call, whatever happens. Latency is
measured around the whole call.

- `args`: the parsed arguments, `sub` included as sent; top-level strings cut
  to `TOOL_ARG_MAX_CHARS` (100). Arguments that aren't JSON:
  `{"raw": <the first 100 characters>}`.
- `outcome`: `ok`; `card_created` when a `prepare_*` call created a card;
  otherwise the `error` the model gets (`bad_arguments`, `tool_failed`, or the
  server's code such as `unknown_tariff`).

Code's own calls (`execute_action`, `create_ticket` for a card) are not model
calls: the audit records them.

## Audit entries

Every entry: `who` = `session.user.sub`, `session_id`, `trace_id` =
`session.trace_id`. Smoke group 3 sets `session.trace_id` itself and calls
`execute_pending` directly.

An entry about a card has `what` = `card.action`, `args` = `card.details` and
the card's key `f"{action}:{pending_id}"`, also when nothing ran, so the key
finds a card's whole history.

### execute_pending

| Result | Outcome | Also |
| --- | --- | --- |
| `executed`, `toggled`, `updated` | `executed` | `before`, `after` from the result |
| our key already taken | `duplicate_suppressed` | |
| billing's `duplicate` while our key was free | `executed` | `before`, `after` from billing's result; `detail` `billing had the key: the call was retried`; reply as for `executed` |
| `daily_limit` | `server_limit` | `detail` `daily_limit` |
| `same_tariff`, `tariff_not_available`, `unknown_tariff`, `unknown_service`, `not_found` | `rejected_by_contract` | `detail` = the outcome |
| exception | `error` | `detail` = the error type |
| any other outcome | `error` | `detail` = the outcome |

Billing's `duplicate` while our key was free: billing writes the key into the
same `idempotency` table in the transaction that changes the tariff, and our
check runs before the call. So that answer means `McpPool` retried a call whose
response was lost: the tariff changed once, during this "да". A repeat later
finds the key in our check and is `duplicate_suppressed`.

### Card answers

- "нет" → `rejected_by_user`.
- "оператор" → `escalate_card`. A ticket id → `escalated`, `detail` = the
  ticket id. No ticket id → `error`, `detail` `ticket_failed`. An exception
  from the ticket call propagates as before and is not recorded.
- The ticket's `note`: the card's rows as before, then a line with the card's
  execution attempts from `find_card_entries`, in Russian (`# I18N:`), e.g.
  `Попытки исполнения: server_limit (daily_limit)`, or `Попыток исполнения не
  было`. Code writes the note, no model.

### Tool wrapper

- A `prepare_*` call the action server refused (`MCPError` with code
  `McpErrorCode.ACTION_REFUSED`) → `rejected_by_contract`: `what` = the action
  the tool prepares (`ACTION_BY_PREPARE_TOOL` in `app/tools.py`), `args` = the
  model's arguments, `detail` = the error code. Validation and business codes
  alike. Other failures reach `tool_calls` only.
- `create_ticket` by an agent that returned a `ticket_id` → `escalated`, `what`
  `create_ticket`, `args` `{"subject": …}` only (the note retells the
  subscriber's words), `detail` = the ticket id.

## State before and after

`execute_action` returns `before` and `after` when the account changed:

- tariff: billing's result has them already, tariff names as strings;
- service: the subscriber's services are read from the CRM before the call;
  `before` is the service's `enabled`, `after` the result's `enabled`
  (booleans);
- contacts: the subscriber is read from the CRM before the call; `before` and
  `after` are dicts of the fields that change, e.g. `{"email":
  "anna@example.ru"}` → `{"email": "anna.new@example.ru"}`. Phone and email
  are stored as they are: they are what the change is about.

Only for `toggled` and `updated`; a refusal carries neither.

## Card link

`db/001_pending_cards_trace_id.sql`: `alter table pending_cards add column if
not exists trace_id text`. `cards.create` stores `session.trace_id`, NULL when
it's empty. `Card` doesn't change.

## Tests

- `tests/integration/test_audit.py`: (1) one trace written through `append`,
  both telemetry writers and a card: `trace()` returns the items in time order
  with `source`, the card entry has `card_trace_id`, `tail(who=…)` returns only
  that subscriber's entries; (2) an unknown outcome raises.
- `tests/integration/test_card_answers.py`: each existing outcome test also
  checks its audit entry; a new test for billing's `duplicate` on the first
  "да" (reply «Готово…», entry `executed`); the two ticket tests check the
  attempts line of the note.
- `tests/integration/test_tool_calls.py`: one parametrized table, tool result →
  `tool_calls` row (`outcome`, cut `args`, broken JSON), plus the audit entry
  for a refused `prepare_*` and an agent's ticket.
- `tests/integration/fake_mcp.py`: `FakeMcpPool` and `make_session`, moved
  from `test_card_answers.py` and shared. The session has its own `trace_id`,
  so a test finds the rows it wrote.
- `tests/unit/conftest.py`: an autouse fixture makes `audit.append`,
  `telemetry.record_llm_call` and `telemetry.record_tool_call` do nothing, so
  unit tests never write to a running database. No test reads those calls.
- `tests/unit/test_action_service.py`: the existing service and contacts
  execution tests also check `before` and `after`.
- `llm_calls` from `run_agent`: no test of its own; the live run checks it.

## Known issues

- An audit entry that fails to write exists only in the log. A production
  system writes the intent before the external call and ships entries through
  an outbox or a queue.
- Append-only holds only through the module. A database trigger and separate
  roles would enforce it.
- The way a card was confirmed (button or typed) is not recorded.
- `expired` is not written yet: a "да" to an expired card goes to the agents,
  and a card that expired during a slow turn is only logged (`card_expired`).
  The policy of step 6 checks the lifetime and refuses.
- If every attempt of a first "да" fails after billing changed the tariff, the
  journal has `error`, then `duplicate_suppressed` on the next "да", and no
  `executed`. Billing's `tariff_change_log` has the change.
- `before` is read from the CRM just before the change, not in one
  transaction with it. A failed read fails the execution: the card stays
  pending, and the next "да" retries it.
- The ticket note lists every entry of the card, so after a ticket that got
  no id the next note also shows `error (ticket_failed)` as an attempt.
- A run that raises leaves no `llm_calls` row.
- Smoke group 3 needs `pep_of` and the PDP of step 6.

## Acceptance

- `make test`, `make test-integration` and `make lint` pass.
- Chat as anna: «переведи меня на семейный», «да», «да» gives `executed`
  (`before` Базовый, `after` Семейный, the key), then `duplicate_suppressed`.
  `make trace` of the first "да" names the card's trace; that trace shows the
  planner, the `tariff` agent and `prepare_tariff_change` → `card_created`.
- Smoke group 3's audit checks pass when run without its PDP lines.
