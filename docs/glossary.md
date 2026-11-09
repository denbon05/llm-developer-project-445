# Glossary

Terms used in this repository. New terms are added with the code that
introduces them.

| Term | Meaning |
| --- | --- |
| Access mode | Whether a plan step only reads data or changes it |
| Dropped step | A plan step the gate removed, with the reason |
| Editor | The agent that writes the reply from the agents' reports |
| Gate | Code that checks the plan before anything runs |
| JWT, JWKS | A signed access token, and the public keys used to verify it |
| PDP | Policy Decision Point: the service that answers Allow or Deny |
| PEP | Policy Enforcement Point: code that asks the PDP and obeys the answer |
| Plan | The planner's proposal: which agents to run and what each must do |
| Planner | The agent that writes the plan and never answers the subscriber |
| Stand | The ready-made environment: web chat, read-only MCP servers, mocks, token issuer |
| Trace id | The id that links everything that happened in one turn |
| Turn | One subscriber message and everything done to answer it |
| Worker agent | A narrow LLM agent with the tools of one domain only, run by the orchestrator |

Agent naming: the planner and the editor are named after their job, worker
agents after their domain (`data`, `kb`). These short ids appear in plans and
logs. Functions and variables that hold agents end in `_agent`.
