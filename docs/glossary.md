# Glossary

Terms used in this repository. New terms are added with the code that
introduces them.

| Term | Meaning |
| --- | --- |
| Access mode | Whether a plan step only reads data or changes it |
| Audit entry | A row of the append-only journal: who tried to change what or asked for an operator, the outcome, the state before and after |
| Card | A confirmation card: a prepared action the subscriber must confirm before it runs |
| Dropped step | A plan step the gate removed, with the reason |
| Editor | The agent that writes the reply from the agents' reports |
| Gate | Code that checks the plan before anything runs |
| Idempotency key | `<action>:<pending_id>`: one confirmed card runs its action at most once |
| JWT, JWKS | A signed access token, and the public keys used to verify it |
| Live card | A card in state `pending` with time left (`Card.is_live`) |
| PDP | Policy Decision Point: the service that answers Allow or Deny |
| PEP | Policy Enforcement Point: code that asks the PDP and obeys the answer |
| Plan | The planner's proposal: which agents to run and what each must do |
| Planner | The agent that writes the plan and never answers the subscriber |
| Stand | The ready-made environment: web chat, read-only MCP servers, mocks, token issuer |
| Telemetry | Records of the system's own work, for cost and speed: one row per agent run (`llm_calls`: tokens, latency) and per tool call a model makes (`tool_calls`: outcome, latency). Unlike the audit, a lost row is acceptable |
| Trace id | The id that links everything that happened in one turn |
| Turn | One subscriber message and everything done to answer it |
| Worker agent | A narrow LLM agent with the tools of one domain only, run by the orchestrator |

Agent naming: the planner and the editor are named after their job, worker
agents after their domain (`data`, `kb`). These short ids appear in plans and
logs. Functions and variables that hold agents end in `_agent`.
