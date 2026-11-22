# 05. Permissions

Who may call which tool comes from the session's token, not from a prompt. The
action server reads the subscriber from its verified token. The backend derives
an allowlist from the token's claims and checks it twice: when it builds the
agents (barrier 1) and when a model calls a tool (barrier 2). Terms:
[glossary](../glossary.md). Contracts and cards: [03](03-action-contracts.md).
The write-path policy and its PDP: [06](06-policies.md).

## Tokens

The backend is a trusted client: `core/tokens.py` gets one token per MCP server
from the issuer, with `aud` = that server's resource URI. Claims of boris's
token for the action server:

```json
{
  "iss": "http://127.0.0.1:8100",
  "sub": "u_9b1c",
  "aud": "http://127.0.0.1:8220/actions",
  "role": "client",
  "scope": "tariffs:read charges:read",
  "iat": 1795000000,
  "exp": 1795003600
}
```

- The action server is built with `make_mcp_server("actions",
  resource=RESOURCES["actions"])` from `core/server_auth.py`. Every request
  needs a Bearer token; the server checks the signature (JWKS), `iss`, `aud`
  and `exp`, otherwise answers 401 with `WWW-Authenticate` and
  `resource_metadata`.
- No tool has a `sub` argument. Each takes the subject from
  `get_access_token().subject` through `read_token_subject()`; a call without a
  verified token raises `RuntimeError` (unreachable behind the SDK's auth
  middleware).
- The server checks no role or scope: smoke group 2 expects boris's
  `prepare_tariff_change` to reach the business rule (`same_tariff`). Write
  rights are the barriers' job here and the policy's in step 6.
- An expired token: `McpPool` refreshes it and retries once (unchanged). The
  401 reaches the pool as an `MCPError`, a branch that retries without a log
  line. No audit entry: `token_refreshed` stays unused.

## Allowlist

`app/permissions.py`. `Allowlist = frozenset[str]`: the names of the operations
(tools) a session may call.

| Group | Operations |
| --- | --- |
| `OWN_DATA_OPS` | `get_my_profile`, `get_my_tariff`, `list_my_charges` |
| `SHARED_READ_OPS` | `list_tariffs`, `search_kb` |
| `CARD_OPS` | `PREPARE_TOOL_NAMES` (the three `prepare_*`) |
| `OPERATOR_OPS` | `get_subscriber_overview` |
| `ESCALATION_OPS` | `create_ticket` |

`OPS_BY_ROLE`: `client` = own data + shared read + escalation; `operator` =
operator + shared read + escalation.

`allowlist_from_claims(claims)` returns `OPS_BY_ROLE[role]`, an empty set for
an unknown role, plus `CARD_OPS` for a `client` whose space-separated `scope`
has `tariffs:write`. `execute_action` is in no group: only code calls it.

| User | Visible | Count |
| --- | --- | --- |
| anna (client, `tariffs:write`) | own data, shared read, `create_ticket`, three `prepare_*` | 9 |
| boris (client) | own data, shared read, `create_ticket` | 6 |
| olga (operator) | `get_subscriber_overview`, shared read, `create_ticket` | 4 |

Decisions:

- Only `tariffs:write` narrows a role's set. Read scopes (`tariffs:read`,
  `charges:read`, `support:read`) are not checked: a read touches only the
  caller's own data (the server takes the subject from the token) or needs the
  operator role, which the CRM server checks.
- The operator changes nothing and acts for no one. A card is always for the
  token's subject, and the operator has no subscriber account, so the "my"
  tools would only return `not_found`.
- `create_ticket` is open to every role.

## Barrier 1: visible tools

`visible_tools(allowlist, tool_names) -> list[str]`: the names that are in
the allowlist, in their order. A model never sees a tool outside it.

- `build_agent_tools` keeps an agent spec's tools that are both offered by the
  session's MCP servers (as before) and visible.
- `choose_prompt(agent_id, visible_tool_names)` in `app/workers.py` holds
  every prompt fork as an explicit `if`; `AgentSpec.prompt` is the default.
  The forks read the visible tools, never the role: the allowlist already
  turned role and scope into tools. If the forks multiply, agents per role
  that the planner sees only when they have visible tools would replace them.
  1. `tariff` with no `prepare_*` (boris, olga) → `TARIFF_NO_ACTIONS_PROMPT`:
     its report says both that the change is unavailable to the subscriber
     here and that an operator can help, with a sample sentence. Without the
     sample the local model wrote only «Оператор может помочь с этим
     изменением.»
  2. `data` when `permissions.has_cross_account_access` (olga) →
     `OPERATOR_DATA_PROMPT`; otherwise `DATA_PROMPT` (see Operator overview).
  `has_cross_account_access` is true when any operator operation is visible,
  so a new one joins the fork without a change there.
- The editor's prompt is the one fork that reads the role (see Operator
  overview): who reads the reply is a fact about the person, not a
  permission. A client who could see other accounts, say a family plan's
  owner, would still be a subscriber.
- `Session.list_visible_tools()` = `visible_tools(session.allowlist,
  session.mcp.tool_names())`, used by `/api/session` (the web chat shows it in
  its tools panel), `session_opened` and the worker agents.

## Barrier 2: admission

`admit(allowlist, operation, args) -> Decision`. `Decision(allowed: bool,
reason: str = "")`, frozen; smoke reads both fields by these names.

1. `operation` not in the allowlist → `operation_not_allowed:<operation>`.
2. A top-level argument key in `IDENTITY_ARGS` (`user_id`, `account`,
   `subscriber`, `msisdn`, `sub`), exact match, first in argument order →
   `object_from_arguments:<key>`.
3. Otherwise allowed, `reason` empty.

`phone` is a new contact value, not an identity. `subscriber_id` of
`get_subscriber_overview` is the object an operator names; the CRM server
checks the role.

`call_mcp_tool` in `app/wrap.py` runs it after parsing the model's arguments
and before the MCP call. A denied call:

- never reaches the MCP server and is not recorded by `record_tool_use`;
- gives the model `{"error": "denied", "reason": <reason>, "retryable":
  false}`, so `tool_calls` gets the outcome `denied`;
- writes an audit entry `denied` (see Audit);
- logs `tool_call_denied` (`tool_name`, `reason`).

Code's own calls (`execute_action`, `create_ticket` for a card) don't go
through it. In practice barrier 2 catches identity keys: the SDK refuses a tool
the agent doesn't have (`ModelBehaviorError`) before the wrapper runs, so
`operation_not_allowed` is a backstop.

## Stop at a denial

Every worker agent gets `tool_use_behavior=stop_run_at_denial` from
`app/wrap.py`, next to the denial it detects (`build_agent` takes an optional
`tool_use_behavior`; the planner and the editor don't get one). After each tool step the SDK passes the step's tool
results; if any output is JSON with `error` = `denied`, the run ends with the
final output `messages.OPERATION_DENIED`, otherwise the model goes on. The
model never sees the denial, so it can't retry with other arguments, such as
the same change for the caller's own account.

`OPERATION_DENIED` (`app/messages.py`): «Операция не выполнена: в чате можно
работать только со своим аккаунтом и только с разрешёнными вам операциями.» It
goes to the editor like any report. It names no id and no reason, so it doesn't
confirm that another subscriber exists. The reason stays in `audit_log` and
`tool_calls`; tool outputs never reach the browser.

## Audit

- `name_audited_action(tool_name)` = `ACTION_BY_PREPARE_TOOL.get(tool_name,
  tool_name)` gives `what` for every entry the wrapper writes: a refused
  `prepare_*`, an agent's ticket, a denial. `what` is the account action the
  call attempts; a tool without a card action is its own action. So `what =
  'tariff_change'` finds every attempt at a tariff change, denied, refused or
  executed.
- `remove_free_text_arguments(args)` drops `FREE_TEXT_ARGUMENTS` (`note`) from
  the `args` of every wrapper entry: the journal keeps no retelling of the
  dialogue. The ticket entry still stores `{"subject": …}`.
- A denial: `who` = `session.user.sub`, `outcome` `denied`, `args` = the
  model's arguments without free text (identity keys stay: they show the
  attempt), `detail` = the reason.

```
what=tariff_change  outcome=denied  args={"sub": "u_9b1c", "tariff": "семейный"}  detail=object_from_arguments:sub
```

## Operator overview

- `ToolName.GET_SUBSCRIBER_OVERVIEW` joins the `data` agent's tools. Barrier 1
  leaves it to the operator only.
- The `data` agent has two prompts, picked by `choose_prompt`. Both end with
  `DATA_RULES`, the rules they share, so each reads whole in one place:
  1. `OPERATOR_DATA_PROMPT`: for an id (`u_…`) get the overview; it has only
     the id, the name and the tariff, so the report is a sentence about that
     subscriber and names anything else asked as not in the overview, with a
     sample («Абонент u_4f2a — Анна, тариф «Базовый». Баланса в обзоре
     абонента нет.»). With a rule but no sample the local model reported a
     bare field list, and the editor answered «какой баланс?» with the tariff
     and an operator with «Здравствуйте! Ваш тариф — Базовый.»
     No search by name: without an id the report asks for one («Нужен id
     абонента (u_…): поиска по имени нет.»), instead of naming the tool's
     `subscriber_id` argument.
  2. `DATA_PROMPT`: the tools give only the subscriber's own data; for another
     id call no tool and report «Данные других абонентов недоступны.» Without
     it anna's «покажи абонента u_9b1c» got her own profile labelled
     «Профиль абонента u_9b1c» (3 of 3).
- `data` description for the planner adds: for a support operator, another
  subscriber's overview by id. It is the same for every session.
- The editor has two prompts too, picked by `choose_editor_prompt(user_role)`:
  `write_reply` takes the session and reads `role` from
  `session.tokens.claims()`; `operator` gets the operator's prompt, any
  other role the subscriber's. Both hold
  `EDITOR_RULES`, and its input is neutral («Message:», «Write the reply.»):
  the prompt says who reads.
  1. `OPERATOR_EDITOR_PROMPT`: the reader is a support operator, the reports
     are about other subscribers; write about them in the third person, never
     greet them or write «ваш» / «у вас», with a sample. A rule line in the
     subscriber prompt wasn't enough: to the operator's bare «u_9b1c» it
     replied «Здравствуйте, Борис. Ваш тариф — «Игровой».»
  2. `EDITOR_PROMPT`: as before.
  The rule for no reports stays last in both: placed first, it made the
  local model greet anna instead of answering «почему списание 440?».

## Session

- `Session.allowlist: Allowlist`, built in `open_session` from `await
  tokens.claims()` after the pool connects.
- The claims stay in `session.tokens.claims()`; step 6 reads `sub`, `role` and
  `scope` there for the PDP.
- `session.user.sub` stays `who` in the journals and the subject of
  idempotency: it is the value the issuer puts in `sub`.

## Logging

- `session_opened` (`session_id`, `role` from the claims, `visible_tools`):
  the rights a session got.
- `tool_call_denied` (`tool_name`, `reason`), warning: barrier 2 refused a
  call.
- `agent_run_stopped` (`reason` `denied`): `stop_run_at_denial` ended a run.
  The SDK gives that function no agent, so the event names none; it sits
  between the denial and the run's `llm_run_completed`.

A denied call in the log of a turn:

```
plan_ready         agents=['tariff'] dropped_steps=[]
tool_call_denied   tool_name=prepare_tariff_change reason=object_from_arguments:sub
agent_run_stopped  reason=denied
llm_run_completed  agent=tariff tools=1 llm_calls=1
```

## Changes to starter code

- `actions/server.py`: `make_mcp_server`, `sub` removed from the five tools,
  `read_token_subject()`.
- `app/wrap.py`: `wrap_mcp_tool` → `build_agent_tool`, `wrap_tools` →
  `build_agent_tools` (smoke calls neither). The nested `call_tool` becomes the
  module-level `call_mcp_tool(session, tool_name, input_json)`: parse the
  arguments → barrier 2 → call the server → card or ticket. `build_agent_tool`
  only builds the `FunctionTool` around it and records telemetry.
  `hide_subject`, `SUBJECT_ARGUMENT` and the `sub` substitution are gone.
  One-line module docstring; `TODO(права)` keeps only the `pep.prepare` part.
- `app/orchestrator.py`: `execute_action` and the card's `create_ticket` are
  called without `sub`.
- `app/permissions.py`, `app/sessions.py`: the step's `TODO(права)` parts are
  done and removed; the parts for `pep` and `guard_state` stay.
- `app/tools.py`: `GET_SUBSCRIBER_OVERVIEW`.
- `app/workers.py`: `AgentSpec.prompt_without_actions` is gone; `choose_prompt`
  holds the prompt forks.
- `README.md`: the JWT point also says that a role allowlist hides forbidden
  tools from the model and checks every call.

## Tests

- `tests/unit/test_workers.py`: a table, demo user and agent → the prompt
  `choose_prompt` picks: anna and boris for `tariff`, anna and olga for `data`.
- `tests/unit/test_editor.py`: a table, reader role → the prompt
  `choose_editor_prompt` picks: client, operator, an unknown role.
- `tests/unit/test_permissions.py`: (1) a table, claims → `visible_tools` over
  all tool names: anna 9 without `execute_action`, boris 6, olga 4, an unknown
  role none; (2) a table for `admit`: an own operation is allowed, boris and
  `prepare_tariff_change` → `operation_not_allowed`, anna with `sub` →
  `object_from_arguments:sub`, `phone` in `prepare_contacts_update` is allowed,
  olga's overview with `subscriber_id` is allowed, `execute_action` is denied.
- `tests/unit/test_wrap.py`: the `sub` tests go. New: `build_agent_tools` gives
  only visible tools; a denied call never reaches the MCP pool and returns the
  denial.
- `stop_run_at_denial`: a two-row table, a denial in the results → final output
  `OPERATION_DENIED`; none → the run goes on.
- `tests/unit/test_actions_server.py`: calls carry no `sub`; a patched
  `get_access_token` gives the subject, and one test checks the service gets
  the token's subject.
- `tests/integration/test_tool_calls.py`: the refusal table gets a `denied`
  row (`what`, `detail`, no `note` in `args`).
- The 401 for a foreign audience and the token refresh need the live issuer
  and action server: smoke group 4 and the live check cover them.

## Known issues

- The operator sees only what the CRM overview returns: id, name and tariff.
  Services, balance and charges of another subscriber are out of reach, and
  the chat says the data is unavailable.
- The editor is written for subscribers; only one prompt line keeps it from
  treating another subscriber's data as the operator's own.
- The model copies `subscriber_id` from the task, and a small model may garble
  it: `not_found`, or another subscriber's overview, which the operator may see
  anyway. Several ids become one `data` step calling the tool several times, or
  several steps (distinct tasks pass the gate).
- In the chat, a request about another subscriber's account rarely reaches
  barrier 2: the schema has no `sub`, so the model drops the id. Seen 2 of 2
  times on the local model: anna's «смени тариф абоненту u_9b1c на семейный»
  gave a card for her own tariff (Базовый → Семейный). No other account is
  touched, but the card doesn't say whose account it is; it still needs "да".
- The `resource_metadata` link in the 401 header points to
  `/.well-known/oauth-protected-resource/actions`, which answers 404: the MCP
  app is mounted under `/actions`, so the metadata is served at
  `/actions/.well-known/...`. The stand's read servers have the same gap.
- After restarting `make stand`, restart `make actions`: the issuer makes a new
  key under the same `kid`, and the key cache refetches only for an unknown
  `kid`, so every call gets 401.
- `Session.allowlist` is a snapshot from the session's token: a role or scope
  change at the issuer applies to new sessions only.

## Acceptance

- `make test`, `make test-integration` and `make lint` pass.
- `make smoke`: groups 0, 1 and 2 (6 checks) are green. In group 4 the
  barrier, audience, IDOR and contract checks pass. Its PDP checks and group 3
  wait for step 6, and its token refresh check runs after the PDP ones, so the
  live check covers it for now.
- Chat as boris: «переведи меня на семейный» gives "unavailable, an operator
  can help", no card, the mock tariff stays Игровой; the tools panel shows 6.
- Chat as anna: the card flow of [03](03-action-contracts.md) works with the
  token.
- A wrapped `prepare_tariff_change` called for anna with `{"sub": "u_9b1c",
  …}` returns the denial, writes `denied` to the audit and `denied` to
  `tool_calls`.
- Chat as olga: «покажи абонента u_4f2a» gives the overview; the tools panel
  shows 4.
- The action server answers 401 without a token and with the CRM token, with
  `resource_metadata` in `WWW-Authenticate`.
- After `force_short()` the next action call succeeds.
