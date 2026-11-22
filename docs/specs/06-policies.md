# 06. Policies

The write path no longer decides in code whether a card may be prepared,
confirmed or executed. The orchestrator is the enforcement point (PEP,
`app/pep.py`): before each stage it asks the decision service (PDP,
`pdp/service.py`, `make pdp`, port 9100) and does what the answer says. The
PDP evaluates Dogwood policies (Cedar plus temporal rules) in
`pdp/policies/tariff.dw` against the schema `pdp/policies/schema.cedarschema`,
and logs every decision to `policy_events`. An unreachable PDP closes writes;
reads never ask it. Cards and execution: [03](03-action-contracts.md); audit:
[04](04-audit.md); allowlist and barriers: [05](05-permissions.md).

## Card cycle

Three requests per card, each with the same input:

1. `Prepare`, when a `prepare_*` tool returned a prepared action, before the
   card is stored. Deny: no card exists.
2. `ConfirmCard`, on every "да" to the card. The PDP first checks its own
   journal: a card it never allowed → `card_not_prepared`, a card older than
   `CARD_TTL_SEC` by its clock → `confirmation_expired`; only then the policy.
3. `Execute`, after an allowed `ConfirmCard`, before `execute_pending`.

Input of all three (the schema's closed `WriteInput` record):

```json
{
  "action": "tariff_change",
  "pending_id": "p_1a2b3c4d",
  "details": "{\"tariff\": \"Семейный\"}",
  "guard_ok": true,
  "sub": "u_4f2a",
  "role": "client",
  "scopes": ["tariffs:read", "tariffs:write", "charges:read"]
}
```

- `details` is `json.dumps(card.details, ensure_ascii=False, sort_keys=True)`.
  `sort_keys` is required: the policy compares the string, and jsonb returns
  keys in its own order, so the card read back for "да" would not match the
  one prepared.
- `guard_ok` is always `true`: no text check exists yet, and the policy
  doesn't read it.
- `sub`, `role`, `scopes` (`scope` split on spaces) come from the session's
  token claims.

## Policy

`pdp/policies/tariff.dw` replaces the TODO comment with four rules, each with
a plain comment for a reader new to policies. Cedar semantics: deny unless a
permit matches; a forbid beats every permit.

1. `permit Prepare` when `role == "client"`, `scopes.contains("tariffs:write")`
   and the input has none of `user_id`, `account`, `subscriber`, `msisdn`
   (`!(context.input has user_id) && …`, the PDP's `IDENTITY_KEYS`; `sub` is
   the subject's own field). The binding doesn't check requests against the
   schema (see the facts below), so only these checks refuse such a key.
2. `permit ConfirmCard` `when temporal { formerly within 1h
   Tel::Action::"Prepare"::request{ input.pending_id:
   context.input.pending_id, input.action: context.input.action,
   input.details: context.input.details } }`: "да" binds to the exact card.
3. `permit Execute`: the same pattern over `ConfirmCard`.
4. `forbid Execute` `when { context.input.action == "tariff_change" } when
   temporal { count_distinct_within(card, 24h,
   Tel::Action::"Execute"::request{ input.action: "tariff_change",
   input.pending_id: card }) > 2 }`.

Facts checked on dogwood-py 0.0.12:

- Dogwood records every request as an event, allowed or denied, and
  `formerly` / `count_within` see them all. Live events carry no timestamps,
  so `within` windows never close in the running PDP.
- `count_within` over all `Execute` requests would count a repeated "да" and
  every retry of one card. `count_distinct_within` counts distinct
  `pending_id`s: a card spends one slot however many times it is confirmed.
  Its key argument must be a bare identifier (`card`), not `?card` or `$card`.
- Without the `when { … == "tariff_change" }` condition the forbid also blocks
  service toggles and contact updates after two tariff changes.
- `when { … }` followed by `when temporal { … }` validates, and so do
  patterns written over several lines.
- The schema checks only the policy, at start. Requests reach the policy as
  they are: an extra key in `input`, `input` without `guard_ok`, a principal
  of another or an unknown entity type (all checked: Allow for a `Prepare`
  that passes rule 1). The PDP builds the principal itself (`_principal`),
  and the PEP sends only the card's fields and the token's subject.

The PDP validates the policy at start and exits on an error (ready-made).

## PDP changes

`pdp/service.py` is ready-made; only logging changes:

- `trace_id`: `/authorize` and `/confirm` read an optional `"trace_id"` from
  the request body (not from `input`) and pass it to `_audit`, which writes it.
  `db/001_pending_cards_trace_id.sql` is renamed `db/001_trace_ids.sql` and
  gains `alter table policy_events add column if not exists trace_id text;`
  (the repo keeps one migration file besides `000_init.sql`).
- One `policy_events` row per request: today an allowed `ConfirmCard` writes
  two (`_authorize`, then the final `_audit`). The final `_audit` stays only
  for the PDP's own refusals (`card_not_prepared`, `confirmation_expired`).
- `_audit` also logs `policy_decision` (`session_id`, `action`, `decision`,
  `reason`) at info level. `make pdp` gets `LOG_LEVEL=$(LOG_LEVEL)` like the
  other targets; check the line shows in the PDP's terminal.

Not changed (see Known issues): the missing check before `Execute` and the
`session_limit` reason counter.

## PEP

`app/pep.py`: a one-line module docstring replaces the TODO; short comments
at the spots where it matters say in plain words what the PDP decides and what
the PEP does with the answer.

- `PolicyReason(StrEnum)`: the reasons code branches on: `session_limit`,
  `confirmation_expired`, `card_not_prepared`, `pdp_unavailable`.
- Decisions are `app.permissions.Decision(allowed, reason)` (smoke reads
  `.allowed` and `.reason`); its docstring becomes general ("whether an
  operation may go ahead and, if not, why").
- `Pep(session_id, tokens)`, methods `async prepare(card)`, `async
  confirm(card)`, `async execute(card)` → `Decision`.
  - `prepare`: `POST {PDP_URL}/authorize {"session_id", "action": "Prepare",
    "input", "trace_id"}`; `execute` the same with `"Execute"`; `confirm`:
    `POST /confirm {"session_id", "input", "trace_id"}`.
  - `trace_id` = `core.logging.read_trace_id()`.
  - The PDP session is opened lazily before the first request: `POST /session
    {"session_id", "subject": {"sub", "role", "scopes"}}`, subject from `await
    tokens.claims()`. Marked open only on success, so a failed open is tried
    again on the next request.
  - 404 (the PDP restarted and forgot the session): open it again and repeat
    the request once. A card shown before the restart then gets
    `card_not_prepared`.
  - Anything else that isn't a 200 with `decision` `Allow` or `Deny` (no
    connection, timeout `PDP_TIMEOUT_SEC = 3`, other status, bad JSON) →
    `Decision(False, "pdp_unavailable")` and a warning `pdp_unavailable`
    (`action`, error type or status).
  - Every decision logs `policy_decision` (`action`, `pending_id`,
    `decision`, `reason`).
  - `build_pdp_client()` returns `httpx2.AsyncClient(base_url=PDP_URL,
    trust_env=False, timeout=PDP_TIMEOUT_SEC)`; one client per decision
    serves the open, the request and its repeat.
  - `Pep.request_decision(action, card)` is the only method that talks to the
    PDP; the fakes of the tests override it, so `policy_decision` is still
    logged.

`app/sessions.py`: `Session.pep: Pep | None = None`; `pep_of(session) -> Pep`
creates `Pep(session.session_id, session.tokens)` on first use and returns the
same one after. Tests put a fake in `session.pep`. The docstring keeps only the
`guard_state` part for the guardrail.

## Prepare path

- `app/cards.py`: `build_new_card(session, action, details, view) -> Card` (in
  memory, new `pending_id`) and `store_card(session, card) -> None` (replace
  the pending card, insert). `create` = both, kept for smoke and tests.
  `CardState.EXPIRED = "expired"`.
- `app/wrap.py`: `create_card(session, tool_name, args, prepared)` becomes
  async: build, `await
  pep_of(session).prepare(card)`, on Allow store and answer the model as
  before. On Deny nothing is stored, the previous pending card stays live, and
  the call ends through `deny_tool_call(session, tool_name, args, reason)`:
  audit `denied` with the bare reason as `detail`, `tool_calls` outcome
  `denied`, log `tool_call_denied`. `stop_run_at_denial` picks the final
  output by the denial's reason: `pdp_unavailable` → `CHANGE_UNAVAILABLE`,
  any other → `OPERATION_DENIED`. The `TODO(права)` comment goes.

## "да" path

`app/orchestrator.py`. `apply_card_answer`, for typed answers and button
presses:

| Newest card | Answer | Result |
| --- | --- | --- |
| live | yes | `confirm_and_execute` |
| `pending`, expired | yes | `confirm_and_execute` (the PDP decides; expect `confirmation_expired`) |
| `confirmed`, time left | yes | `confirm_and_execute`: same card, no new slot; the key gives the duplicate reply |
| live | no, operator, topic | as before |
| anything else | anything | normal turn |

`confirm_and_execute(session, card) -> str`: `pep.confirm`; if allowed,
`pep.execute`; if allowed, `execute_pending` (unchanged, no PEP inside: smoke
group 3 calls it directly after `prepare`). A refusal at either stage:

- `confirmation_expired`: a `pending` card is settled `expired` with an audit
  entry `expired`; reply `CARD_EXPIRED`. A `confirmed` card (the PDP's clock
  is a few ms ahead of the database) is left as it is, same reply. The next
  "да" goes to the agents.
- any other reason: audit `denied` (card entry, `detail` = reason), the card
  stays `pending` so «оператор» can still turn it into a ticket whose note
  lists `denied (session_limit)`; reply by reason: `session_limit` →
  `SESSION_TARIFF_LIMIT`, `pdp_unavailable` → `CHANGE_UNAVAILABLE_RETRY`,
  `card_not_prepared` → `CARD_NOT_PREPARED`, else `OPERATION_DENIED`.

A "да" with no card, or to a rejected, replaced or expired-state card, goes to
the agents without a PDP call: they can't execute anything (`execute_action`
is never visible), so nothing runs.

A card that expired during a slow turn (`send_card_and_ticket_replies`) is
settled `expired` with an audit entry `expired`, besides the `card_expired`
log and the `CARD_EXPIRED` reply.

The module docstring's `TODO(права)` goes.

## Replies

`app/messages.py`. Refusals name no internal service: the reason stays in the
log, the audit and `policy_events`.

- `SESSION_TARIFF_LIMIT`: «В этом чате можно сменить тариф не больше двух раз.
  Могу передать запрос оператору — напишите «оператор».» (the number is the
  policy's).
- `CHANGE_UNAVAILABLE`: «Сейчас не получается выполнить изменение. Попробуйте
  позже или напишите «оператор».»
- `CHANGE_UNAVAILABLE_RETRY`: «Сейчас не получается выполнить изменение.
  Ответьте «да» позже, чтобы повторить, или напишите «оператор».»
- `CARD_NOT_PREPARED`: «Карточка не найдена или истекла. Повторите,
  пожалуйста, просьбу.»

## Trace

`app/audit.py`: `trace()` reads a fourth table, `policy_events` by
`trace_id`, `source` `policy_events`; the newest-trace lookup includes it. A
line: `14:05:20 policy_events  Execute  Deny  session_limit` (reason only on a
Deny); the source column widens to 14 characters. Card histories stay as they are: policy rows of
other turns don't join them.

## Other changes

- `Makefile`: the `typecheck` comment names only `guardrail_client.check` as
  missing.
- `docs/specs/03-action-contracts.md`: the card-answer table and its
  paragraph, the slow-turn expiry, and the Known issues this step closes (smoke
  group 3 needs the PEP; a new card replaced before the policy approves it;
  `apply_card_answer` returning before the PEP; `Execute` counted per
  request). Keep the unguarded `settle_card` and unknown-state items.
- `docs/specs/04-audit.md`: `trace()` reads four tables; drop the Known issues
  about `expired` not written and smoke group 3 needing `pep_of`.
- `docs/specs/05-permissions.md`: the intro's "comes with the PDP of step 6"
  links to this spec.
- `docs/specs/README.md`: row 6; rows 4 and 5 become "done".
- `README.md`: already names the policy service and `make pdp`; change only
  what turns untrue. No glossary entries.

## Tests

- `tests/unit/test_policy.py`: the real `tariff.dw` and schema in
  `dogwood.native.NativeAuthorizer`, no HTTP, no database. A table, request
  sequence → decision of the last one: three "да" to one card, then a second
  card → Allow; a third distinct card → Deny; a service after the limit →
  Allow; `ConfirmCard` with other `details` than `Prepare` → Deny. Smoke group
  4 covers the rest.
- `tests/unit/test_pep.py`: a fake PDP on `httpx2.MockTransport`, reached by
  patching `pep.build_pdp_client`. One test: the bodies sent for a card at
  the three stages (session opened once, input fields, sorted `details`,
  `trace_id`). A table: a Deny with its reason; no connection, timeout or 500
  → `pdp_unavailable`; 404 → the session is opened again and the request
  repeated once; a failed open is tried again on the next request.
- `tests/unit/test_wrap.py`: the "prepare creates a card" test becomes a
  two-row table: Allow stores the card; Deny (`pdp_unavailable`) stores
  nothing and ends the run with `CHANGE_UNAVAILABLE`. Its `FakePep` gives one
  decision to every stage.
- `tests/integration/fake_pep.py`: `FakePep(decision_by_action)`, a `Pep`
  subclass; a stage without a set decision is allowed. `make_session` in
  `fake_mcp.py` gives every session an allowing one; a test replaces
  `session.pep`. `conftest.py` truncates `policy_events` with the other
  tables.
- `tests/integration/test_card_answers.py`: a table, set decisions → reply,
  card state, audit entries, nothing executed: `session_limit` → its text,
  the card stays pending, audit `denied (session_limit)`;
  `confirmation_expired` on a card expired in the database too →
  `CARD_EXPIRED`, card `expired`, audit `expired` (it replaces the old test
  that a "да" to an expired card goes to the agents); `pdp_unavailable` → its
  text, card pending. The existing tests get the allowing fake. The slow-turn
  expiry gets one check in its existing test: card `expired`, audit
  `expired`.
- `tests/integration/test_audit.py`: the existing timeline test gets a
  `policy_events` row written by the PDP's `_audit` with the trace id.

## Decisions

- A page reload opens a new session and so a new tariff limit, by design: the
  billing mock's daily limit (`DAILY_TARIFF_CHANGE_LIMIT = 3`, counted in
  `tariff_change_log`) is the hard cap across sessions.
- Every "да" goes through the PDP, repeats included; counting distinct cards
  keeps repeats and retries from spending the limit.

## Known issues

- The ready-made PDP checks its journal before `ConfirmCard` but not before
  `Execute`. Since `formerly` sees denied requests too, a denied `ConfirmCard`
  would open `Execute` for that card. The PEP never asks `Execute` after a
  refusal, so the chat can't reach it.
- The PDP names an `Execute` refusal `session_limit` by counting every
  `Execute` request, services and repeats included. In the chat an `Execute`
  refusal comes only from the limit, so the name is right there.
- The PDP keeps its history in process memory: a restart forgets every
  session. The PEP reopens it; cards shown before get `card_not_prepared`.
- Two clocks: the database's `expires_at` decides which card a message answers
  and what the chat shows, the PDP's clock decides a "да". The PDP's starts
  first (prepare comes before the insert), so it is never more lenient. Both
  processes must read the same `CARD_TTL_SEC`.
- A card counts toward the limit from its first `Execute` request, also if
  execution then fails.
- `within 1h` and `24h` never close live (no event timestamps); the session's
  life is the real window. The card's lifetime is the PDP's code, not the
  policy; after QA it moves into the policy as data (the `POST-QA` note at rule
  2 of `tariff.dw`). The course checks the windows with replays, which may
  feed events without the new fields.
- The PDP never drops a session (`SESSION_IDLE_SEC` is unused).
- After `CARD_EXPIRED` asks to repeat the request, a bare «да» goes to the
  agents, and the planner may take it as the repeat: in the live check it
  prepared a new card for the same change (1 of 1). Nothing runs without
  another «да».
- A refused `prepare_*` ends the run with `CHANGE_UNAVAILABLE`, but the editor
  rewrites the report like any other: in the live check it said «выполнить это
  изменение».

## Acceptance

- `make test`, `make test-integration`, `make lint`, `make typecheck` pass.
- `make migrate`, then with stand, actions, pdp and api running: `make smoke
  G=3` (4 checks) and `make smoke G=4` (21) are green, groups 0–2 stay green.
- Chat as anna: «переведи меня на семейный» → card; «да» → executed; «да» →
  «Эта операция уже исполнена…»; a second change → executed; a third →
  `SESSION_TARIFF_LIMIT`; «оператор» → a ticket whose note lists `denied
  (session_limit)`; a service toggle → executed. Each decision has a
  `policy_events` row with the turn's `trace_id`; `make trace` of the first
  "да" shows them.
- With `CARD_TTL_SEC=15` for api and pdp: a card, a wait, «да» →
  `CARD_EXPIRED`, card `expired`, audit `expired`, `policy_events`
  `ConfirmCard Deny confirmation_expired`; the next «да» goes to the agents.
- PDP stopped: a change request gets a reply from `CHANGE_UNAVAILABLE`, no
  card; «да» to a card shown before → `CHANGE_UNAVAILABLE_RETRY`, nothing
  executed. PDP restarted: «да» to the earlier card → `CARD_NOT_PREPARED`; a
  new card works.
- boris and olga behave as before.
