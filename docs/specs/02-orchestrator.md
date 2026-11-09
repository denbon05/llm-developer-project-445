# 02. Orchestrator

One chat turn for read-only questions: plan, gate, worker agents, editor, SSE.
Terms: [glossary](../glossary.md).

## Flow

`POST /api/chat` runs `run_turn` in its own task and streams its events.

1. The planner (LLM, no tools) reads the dialogue and returns a `Plan`.
2. `gate()` (code, no LLM) filters the plan.
3. Each kept step runs one worker agent, one after another.
4. The editor (LLM, no tools) writes the reply. Only its text reaches the
   subscriber, except a clarifying question, which skips the agents and the
   editor.

## Contracts

| From → to | Contract |
| --- | --- |
| Planner → gate | `Plan` |
| Gate → orchestrator | `Plan` and `list[DroppedStep]` |
| Orchestrator → worker agent | `Step.task`, a string. No question, no history, no other reports |
| Worker agent → editor | `AgentRun`: report and tool calls |
| Orchestrator → browser | SSE events |

## Plan

`Plan`: `reasoning`, `steps`, `question`.

- `reasoning` is required and comes first. The model writes JSON in field order,
  so this text is written before the decision and improves it. Nothing reads it
  to make decisions, and it stays on the server.
- `steps` is required. With a default, the model returned its own shape, such as
  `{"plan": ...}`, and the plan came out silently empty.
- `question` is optional: a missing value means there is nothing to ask.

`Step`: `agent_id`, `task`, `access_mode` (`read` or `write`). `agent_id` also
accepts `agent` and `agentName`, `task` also accepts `job`, because small models
answer in their own dialect. An unknown `access_mode` becomes `read`.

The output schema is non-strict: strict mode is too demanding for small models.

Routing knowledge lives in the `CATALOG` descriptions and in a few examples in
the prompt, not in rules about particular questions. Checked against questions
the prompt was not tuned on, this beat question-specific rules.

## Gate

`gate()` is a pure function. In order:

| Condition | Result | `DropReason` |
| --- | --- | --- |
| `question` is set | all steps dropped, the question goes to the subscriber | `clarification_requested` |
| `agent_id` not in `CATALOG` | step dropped | `unknown_agent` |
| Same `agent_id` and `task` as a kept step | step dropped | `duplicate` |
| 4 steps already kept | step dropped | `too_many_steps` |
| `agent_id` in `READ_AGENTS` | `access_mode` forced to `read` | none |

`CATALOG` is the single source of agents: the planner prompt is generated from
it, so a new entry is known to both.

## Worker agents and editor

- `data` has `get_my_profile`, `get_my_tariff`, `list_tariffs`,
  `list_my_charges`. `kb` has `search_kb`.
- Tool names live in `ToolName`. `wrap_tools` skips unknown names silently, so a
  unit test checks every name against the MCP servers.
- Agents get at most `MAX_AGENT_TURNS` (6) LLM calls.
- Small models sometimes end with an empty message after a tool call. The editor
  then gets `The agent returned no report.` and `llm_run_empty_output` is logged
  as a warning. Raw tool results never replace a report: for `kb` they are
  article text, which must not reach the editor unfiltered.
- The editor sees only the message and the numbered reports. With no reports it
  answers on its own and must not claim that data was checked.
- If articles were found and the reply has no citation, the editor is asked once
  to restore them, with the exact citations in the request. Prompt wording alone
  left about a third of such replies uncited.
- Code removes every citation that names no article the search returned. Small
  models invent them, for example `(источник: 1)`.
- Prompts are in English. Tasks, search queries and replies are in Russian,
  because the knowledge base is in Russian.

## LLM boundary

Every agent, including the planner and the editor, runs through
`app.llm.run_agent`. It returns a typed `AgentRun` and logs `llm_run_completed`.
Accounting and failover belong at this boundary.

Retries are the OpenAI client's: `max_retries=2` in `core/config.py`. They cover
network errors and 408, 409, 429 and 5xx, with jittered backoff. An invalid plan
is not retried. Don't add `ModelSettings(retry=...)` on top: the client's retries
stay active on the first attempt, so one call becomes 5 requests instead of 3.

## Session

- `planner_messages`: subscriber messages and the replies they saw, in memory,
  last 20 messages. Updated only after a reply is sent.
- `used_tools`: tool names in call order, written by `record_tool_use()`.
- `dropped_steps`: everything the gate removed, across all turns. It stays on
  the server: the session and the `plan_ready` log line.
- `trace_id`: the current turn.

## SSE

Each event is `data: {json}\n\n`.

| Event | Fields | When |
| --- | --- | --- |
| `plan` | `agents` | at least one step is kept |
| `reply` | `text`, `sources` | the turn produced a reply |
| `error` | `detail` | the turn failed or the session is unknown |
| `done` | none | always last |

`agents` holds `"[data] <task>"` strings: the web chat joins them into the plan
line, and scenarios look for the agent id by substring. `sources` lists the
`source` of every article `search_kb` returned.

## Failures

Any exception in a turn gives one `error` event with a fixed sentence from
`app/messages.py`. The session stays usable and the history is unchanged.

`turn_failed` carries the trace id, the error type and the stack frames, but not
the exception text: a plan validation error quotes the model's output.

A client disconnect stops the stream, not the turn: cancelling a turn in the
middle of an action would leave the action done and unrecorded.

## HTTP

- `GET /api/health`.
- `POST /api/session` `{"user"}` returns `session_id`, `name`, `role`,
  `visible_tools`. An unknown user gets 400 with `{"error": ...}`, the field the
  web chat reads.
- `POST /api/chat` `{"session_id", "message"}` returns the SSE stream. An unknown
  session gets an `error` event, because the web chat reads only the stream.

## Logging

`run_turn` binds `trace_id` and `session_id` to the logging context. Events:
`turn_started`, `plan_ready`, `llm_run_completed`, `llm_run_empty_output`,
`turn_finished`, `turn_failed`. Message text, tasks and `reasoning` are never logged. A field is
logged even when empty, so every event of a kind has the same fields.

`llm_run_completed` has `tools` and `llm_calls`. Tools requested in one model
response run concurrently, so `tools` of 2 with `llm_calls` of 2 means one
response asked for both and the second wrote the report.

## Changes to starter code

- `Session`: `thread` became `planner_messages`, `trace` became `used_tools`.
- `core/config.py` sets `max_retries` explicitly (same value as the default).
- `make test` runs pytest. `make smoke` is unchanged.
- The user dropdown in `web/app.js` opens a new session in place. It used to
  reload the page, which reset the selection to the first user.
- `.PHONY` lists only `actions`, `pdp` and `guardrail`: they share a name with a
  directory, so without it `make` reports them up to date and starts nothing.

## Known issues

- The `sessions.thread` column is never read or written.
- Sessions and their MCP connections live until the process exits.
- A hung model takes about 9 minutes to fail: the 180 s timeout is retried.
- `sources` can list articles the reply doesn't cite.
- The stream carries the stages of a turn, not tokens, so the reply arrives
  whole. The output check has to see the full text before it is shown.
- If the repair also loses the citations, the reply is sent as it is.

## Acceptance

- `make test` passes and `make smoke G=1` passes 5/5.
- Scenario `10_read_tariff` passes on a live model. `30_tariff_change_ok` needs
  the action agents.
- In the web chat as `anna`, "какой у меня тариф и почему списание 440?" shows
  the plan line, then a reply with the tariff, the rule and its source.
- After a failed turn, the next message in the same session works.
