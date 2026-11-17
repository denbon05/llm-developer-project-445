# 03. Action contracts

Changes to a tariff, a service or contact details: prepare a card, confirm it,
execute it once. Terms: [glossary](../glossary.md). The read-only turn is in
[02](02-orchestrator.md).

## Processes

`make actions` serves `actions.server:app` on :8220, MCP at `/actions/mcp`.
Sessions connect to it like to the read servers, so a session opens only while
it runs. No auth yet: see Known issues.

## Tools

| Tool | Called by | Result |
| --- | --- | --- |
| `prepare_tariff_change(sub, tariff)` | `tariff` agent | prepared action |
| `prepare_toggle_service(sub, service, enabled)` | `tariff` agent | prepared action |
| `prepare_contacts_update(sub, phone, email)` | `tariff` agent | prepared action |
| `execute_action(sub, action, details_json, pending_id, idempotency_key)` | orchestrator code only | mock result with `outcome`, plus `pending_id` |
| `create_ticket(sub, subject, note)` | `tickets` agent, orchestrator code | `ticket_id`, `status` |

A prepared action is `{"ok": true, "action", "details", "view"}` and changes
nothing. `action` is `tariff_change`, `service_toggle` or `contacts_update`.
`view` is flat: Russian label → scalar, because the web chat renders the keys
as rows. `execute_action` is in no agent's tool set.

`phone` and `email` are `str | None = None`: models send `null` for the contact
they keep. The SDK's strict schema lists both as required, so the model sees
`anyOf: [string, null]`. The server accepts null, a blank string or an omitted
field, and all three mean "not given".

## Outcome classes

| Class | Where | What happens |
| --- | --- | --- |
| Validation error | `ActionError`, `retryable: true` | the model fixes the call from the hints and calls again |
| Business rule | `ActionError`, code in `BUSINESS_RULE_CODES`, `retryable: false` | the model reports the refusal |
| Infrastructure failure | transport, HTTP error | `McpPool` retries once; the model sees an error, never a stack trace |

`ActionError` reaches the client as `MCPError(code=-32002, message=<code>,
data=to_mcp_data())`. `data` is `{"error", "detail", "retryable", **hints}`.
`detail` is Russian.

| Code | Class | Hints |
| --- | --- | --- |
| `unknown_tariff` | validation | `available`: available tariffs |
| `unknown_service` | validation | `available`: the subscriber's services |
| `bad_phone`, `bad_email` | validation | `example` |
| `nothing_to_update` | validation | none |
| `unknown_action`, `bad_details` | validation | `execute_action` only |
| `same_tariff` | business | `current` |
| `tariff_not_available` | business | `available` |
| `service_already_on`, `service_already_off`, `same_contacts` | business | none |

## Normalization

`actions/contracts.py`, exact match after cleanup only. A prefix or fuzzy
match could change the wrong thing.

- Tariff and service: remove quotes and guillemets, casefold, ё → е, collapse
  spaces, drop a leading «тариф» / «услуга». A known but closed tariff (Про) is
  `tariff_not_available`.
- The service catalog is the subscriber's own service list from the CRM: the
  CRM mock toggles only services listed for the subscriber.
- Phone: digits, spaces, `()-.` and an optional `+`; 11 digits starting with 7
  or 8 → `+7 900 111-22-33`.
- Email: trimmed, lowercased, `x@y.z` shape.

## Preconditions

| Tool | Refusals |
| --- | --- |
| `prepare_tariff_change` | `unknown_tariff`, `tariff_not_available`, `same_tariff` |
| `prepare_toggle_service` | `unknown_service`, `service_already_on`, `service_already_off` |
| `prepare_contacts_update` | `nothing_to_update`, `bad_phone`, `bad_email`, `same_contacts` |

Contacts details and view hold only the fields that change. `execute_action`
checks nothing again: the mocks refuse what they can't do, and setting a value
twice is harmless.

## Agents and gate

- `tariff`: the three `prepare_*`, `get_my_tariff`, `list_tariffs`,
  `get_my_profile`. Named after tariffs, it also covers services and contacts.
  Its first call for a change is the matching `prepare_*`. If the session has
  none of them, code gives it `TARIFF_NO_ACTIONS_PROMPT`: changes are
  unavailable, offer an operator.
- `tickets`: `create_ticket`, called at once, no clarifying questions.
- The gate keeps `write` only for `ACTION_AGENTS = ("tariff", "tickets")`.

## Wrapper

- If a tool schema has `sub`, the model gets the schema without it, and every
  call gets `session.user.sub`, overwriting what the model sent.
- A successful `prepare_*` → `cards.create(...)` → the model gets
  `{"card_created": true, "card": <view>, "next": ...}`. Errors stay JSON with
  `error` and the data.

## Turn with cards

`run_turn` holds `session.turn_lock` for the whole turn; the decision endpoint
takes it too. One turn or decision at a time keeps two "да" from both passing
the idempotency check.

At turn start the turn reads the session's newest card in any state,
`cards.find_latest_card(session_id)`. The database also counts the card's
`seconds_left` (0 once expired). A live card (`Card.is_live`) is `pending`
with time left. `apply_card_answer` decides for typed answers and button
presses alike:

| Newest card | Answer | Result |
| --- | --- | --- |
| live | yes | `execute_pending` |
| live | no | settle `rejected`, `Отменил, ничего не изменилось.` |
| live | operator | `create_ticket` by code, then settle `rejected`, reply with the ticket id |
| live | topic | normal turn, the card stays live |
| `confirmed`, time left | yes | `execute_pending`: the key is taken, so the duplicate reply and nothing runs twice |
| `confirmed` | anything but yes | normal turn |
| anything else (none, expired, rejected, replaced) | anything | normal turn |

The course asks for this: «да», «да» gives `executed` and then
`duplicate_suppressed` (step 4). The repeat window is the card's lifetime, not
"the next message", so no session state remembers a confirmed card. A "да"
with no card or after the card's lifetime goes to the agents for now. For the
policy of step 6 to refuse it, step 6 has to ask the PEP before
`apply_card_answer` returns early (see "Known issues").

After the agents, code writes the parts a model must not retell: card prices
and ticket ids (a small model turned `t_1674` into «ТЗ-1674»). A run is
answered by code if a `prepare_*` call returned `card_created`, or a
`create_ticket` call returned a `ticket_id`. If any run is, the turn sends:

1. if other runs exist, the editor writes over their reports only and its
   `reply` is sent;
2. for each ticket, `reply` with `TICKET_CREATED` and the id from the tool
   output;
3. for a card created this turn (the session's newest card after the agents
   differs from the one before them, `find_new_card`), read again after the
   editor so its state and seconds left are current: if it is live,
   `{"type": "card", "pending_id", "card": public_view()}` with the seconds it
   has left, then `reply` with `CARD_READY`; if it is `pending` but expired
   during a slow turn, only `reply` with `CARD_EXPIRED`, so nobody is asked to
   confirm a card that can't be answered. A new card in another state (a
   refusal by the policy or the guardrail of later steps) needs its own reply.

A failed `prepare_*` or `create_ticket` gives no card or id, and its report goes
to the editor as usual. The planner history gets the texts sent, joined by a
blank line. Every card decision goes there too.

## Classifier

`app/answers.py`, vocabularies `YES_WORDS`, `NO_PHRASES`, `OPERATOR_PHRASES`
(moved there from `app/cards.py`), case-insensitive. A message mixed with a
question or another request must not confirm or escalate a card.

- A message with `?` is a topic, whatever words it has.
- NO matches whole words or phrases anywhere: `(?<!\w)…(?!\w)`.
- YES only when every word is a yes word or a filler (`YES_FILLER_WORDS`:
  «пожалуйста», «спасибо», «ну»): «да, пожалуйста» confirms, «давай лучше
  Игровой» doesn't.
- OPERATOR matches at a word start («оператора» matches), in a message of at
  most `OPERATOR_REQUEST_MAX_WORDS` (4) words: a request is short.
- Order: question, no, yes, operator, else topic. Both yes and no → no: a wrong
  cancel is cheaper than a wrong execution. «нет, лучше смени на Игровой»
  cancels, and the new request has to be repeated.

## Execution

`execute_pending(session, card) -> str`:

1. key `f"{action}:{pending_id}"`;
2. `idempotency.execute(key, sub, action, ...)` around the MCP call
   `execute_action`, with `sub` passed by code and `details_json` as a JSON
   string;
3. settle `confirmed` only when the account has the change: the outcome is in
   `idempotency.ACCOUNT_CHANGED_OUTCOMES`, or the key was taken already. After
   an exception, a refusal or an unknown outcome the card stays `pending`:
   while it lives, "да" runs it again with the same key and «оператор» hands
   it to a ticket with its details (`ACTION_DAILY_LIMIT` suggests that);
4. the reply is built by code from `app/messages.py`.

| Outcome | Reply |
| --- | --- |
| `executed` | `Готово: тариф изменён с «A» на «B», N ₽/мес.` |
| `toggled` | `Готово: услуга «X» подключена.` / `отключена.` |
| `updated` | `Готово: контакты обновлены — телефон …, почта ….` |
| `duplicate` (our table or billing) | `Эта операция уже исполнена — повтор не выполнялся.` |
| `daily_limit` | `Лимит смен тарифа на сегодня исчерпан. …` |
| `same_tariff`, `tariff_not_available`, `unknown_tariff`, `unknown_service`, `not_found` | a sentence from `EXECUTION_REFUSALS` |
| exception, other outcome | `Не удалось выполнить операцию. Ответьте «да», чтобы повторить.` |

## Idempotency

`app/idempotency.py`, table `idempotency`.

- Key present → `StoredResult(is_duplicate=True, stored)`, nothing runs.
- Otherwise run; `executed`, `toggled`, `updated` and `duplicate` take the key
  with `INSERT ... ON CONFLICT (key) DO NOTHING`. Other outcomes and exceptions
  take nothing, so a failure doesn't lock out a retry.
- Tariff changes have two barriers: the billing mock records the same key in
  the same table and answers `duplicate`. Services and contacts have only ours.

## Decision endpoint

`POST /api/cards/{pending_id}/decision` `{"session_id", "answer": "yes"|"no"}`
→ `{"text", "outcome"}`, `outcome` is `confirmed` or `rejected`. The id names
the card exactly: `cards.find_card(session_id, pending_id)` reads it in any
state, and `apply_card_answer` applies the table of "Turn with cards". So a
click after a typed "да", or a double click, gets the duplicate reply. When
the answer changes nothing (no such card in this session, expired, rejected,
replaced, or "нет" to a confirmed card) → 404 `{"text": "Карточка не найдена или истекла.",
"outcome": "not_found"}`.

## Logging

`card_created` (action, pending_id), `card_decision` (action, pending_id,
answer, also `topic`), `action_executed` (action, pending_id, outcome),
`action_failed` (error type), `ticket_created` (ticket_id), `ticket_failed`
(pending_id: the ticket tool answered without an id, the card stays live,
reply `TICKET_FAILED`), `card_expired` (pending_id: a new card expired before
it could be shown). `turn_finished` always has `sources`, `card` (the id of the
card shown, or empty) and `tickets` (count). No message text.

## Changes to starter code

- `Session`: `pending_card` is gone; new `turn_lock`. `open_session` connects
  `actions` too.
- `app/cards.py` keeps no state: the database is the only source of the cards.
  The session cache is gone (`active()` returned a cached card even after it
  expired and wrote the cache as a side effect). Queries change nothing:
  `find_latest_card(session_id)`, `find_card(session_id, pending_id)`.
  Commands change the table: `create` (name and signature kept, Hexlet's smoke
  checks call it), `settle_card(card, state)`. `Card` carries its `state`
  (a `CardState`; an unknown state fails when the row is read) and
  `seconds_left`, counted by the database, which also sets `expires_at`.
  `public_view()` takes no TTL: it shows the seconds left. The answer phrases
  moved to `app/answers.py`, and the unused `classify_answer` is gone.
- `find_new_card` compares the newest card before the agents with the newest
  card after them; it reads no session field.
- `ToolName` has the action tools; `PREPARE_TOOLS` lists the three `prepare_*`.
- `READ_AGENTS` became `ACTION_AGENTS` with the inverse check.
- `SERVICE_CATALOG` in `actions/service.py` is gone: the subscriber's service
  list is the catalog.
- `execute_action` echoes `pending_id` in its result.

## Known issues

- `McpPool.call` treats every `MCPError` except forbidden as transport. Each
  action refusal costs a token refresh, a reconnect and a second call. Harmless
  for `prepare_*`, but a real transport fault during `create_ticket` can create
  two tickets.
- The action tools take `sub` as an argument until permissions are done. Smoke
  group 2 calls them without it, so 4 of its 6 checks stay red until then.
- jsonb doesn't keep key order, so a card view re-read from the database may
  show its rows in another order.
- Two sessions using one key at the same moment could both run; the lock is
  per session. Only tariff changes have the billing barrier behind it.
- Smoke group 3 also needs the audit and the PEP.
- `Session` is mutable state in the memory of one process, and any function
  that gets it can change any field. It is lost on restart and can't be shared
  between workers. Only the cards are durable (`pending_cards`).
- When a tool fails unexpectedly, the `tariff` agent's report can contain the
  model's leaked reasoning (e.g. `<|channel>thought ...`). It reaches only the
  editor.
- In a mixed turn the editor sees the whole message, so it may answer a part
  that code answers: asked for charges and an operator, it added its own advice
  on reaching support next to the ticket text.
- Within a confirmed card's lifetime, a "да" that answers an agent's question
  gets the duplicate reply instead of reaching the agents. The course wants
  every "да" to go through the card path (steps 4 and 6).
- Card state changes are not guarded in the database: `settle_card` updates
  from any state, and "one pending card per session" is a convention of
  `create`. Correct today because every caller holds `turn_lock`; to revisit
  with the policy of step 6.
- A new card replaces the session's pending one before the policy approves
  it; a denied card (steps 6–7) would leave the session with none.
- `McpPool` retries after a lost response: the billing mock then answers
  `duplicate`, and the first "да" reads «уже исполнена». To handle with the
  audit of step 4.
- The decision endpoint reports the requested answer as `outcome`, also when
  execution failed, and "нет" to a confirmed card gets the 404 text.
- Step 6: `apply_card_answer` returns `None` for a missing or expired card
  before any PEP call, so the PDP's `card_not_prepared` and
  `confirmation_expired` can't fire from the chat. Step 6 must ask the PEP
  before that return, or reply `CARD_EXPIRED` itself.
- Step 6: the PDP counts every `Execute` request toward the session's
  tariff-change limit, and a repeated "да" calls `execute_pending` again. So
  «смени», «да», «да» would use up two changes. Check the idempotency key
  before asking the PEP, not `card.state` (smoke passes the object `create`
  returned, which stays `pending`).
- Steps 6–7: every turn reads the session's newest card, and `build_card`
  fails on a state that `CardState` doesn't know. Writing such a state (for
  example a raw "denied") would break every later turn of the session. Add a
  new state to `CardState` before writing it.

## Acceptance

- `make test`, `make test-integration` (needs Docker) and `make lint` pass.
- With `make actions` running and `sub` passed explicitly: `prepare_tariff_change`
  for «семейный» returns a card, «люкс» returns `unknown_tariff` with
  `available`, boris and «игровой» returns `same_tariff`.
- `execute_pending` twice on one card, and «да», «да» in the chat: «Готово…»,
  then «Эта операция уже исполнена — повтор не выполнялся.».
- Scenario `30_tariff_change_ok` on a live model: a card with the price
  difference, then «да» gives «Готово» and the mock tariff is Семейный.
- In the chat as `anna`: «поменяй мою почту на anna.new@example.ru» gives a
  card with only Почта, «да» gives «Готово: контакты обновлены…».
- «позови оператора» with no card: the reply names the ticket id that
  `/mock/tickets?sub=u_4f2a` shows.
