# 01. Stand and logging

Process map and ports: [README](../../README.md#how-it-works).

## Scope

The stand (`make stand`, :8100) and the code in `core/`, `services/`, `kb/`
and `pdp/service.py` are starter code. Keep its behavior unless this spec
lists a change.

For manual tool calls, `make inspect-mcp LOGIN=<login>` opens every MCP server
in MCP Inspector with fresh tokens, from the git-ignored `.mcp-inspector.json`.

## Acceptance

- `make smoke G=0` passes 9/9 with an embedding model (vector search) and
  without one (lexical search). CI has no embedding model.
- With the stand running: `/health` is ok, `POST /token` returns
  `access_token`, `/mock/crm/subscriber?sub=u_4f2a` returns the subscriber,
  `/jwks.json` returns the key, `/operator.html` loads.

## Changes to starter code

- Dependencies are managed with uv. `make install` keeps `requirements.txt`
  in sync, because CI installs from it with pip.
- `httpx2` replaces `httpx`.
- `search_kb` embeds queries with `QUERY_PREFIX` (it used `DOC_PREFIX`).
  Stored vectors are unchanged.

## Logging

`core/logging.py` provides `get_logger(name)`: structlog console lines on
stdout, level from `LOG_LEVEL` (default `INFO`). `merge_contextvars` is there
for the trace id.

- Never log tokens or the full text of messages. Log identifiers, tool names,
  statuses and error types.
- Event names are snake_case English.
- Failures that are swallowed on purpose must leave a line:

| Failure | Level |
| --- | --- |
| Query embedding unavailable, search falls back to words | `warning` |
| `policy_events` write failed | `exception` |
| MCP transport failure before the retry | `warning` |
| MCP connection close failed | `warning` |
| Bearer token rejected by `JwtVerifier` | `warning` with the reason |

## Known issues

Left as is to keep the starter behavior.

- `JwkCache` refetches keys only for an unknown `kid`, and the issuer reuses
  one `kid` with a new key on every start. Restart `make actions` after
  restarting `make stand`, otherwise every token gets 401.
- `resource_metadata` in the 401 response points to
  `/.well-known/oauth-protected-resource/crm`, which is 404. The document is
  served under the mount: `/crm/.well-known/oauth-protected-resource/crm`.
- The lexical search ignores `limit` and always returns 3 results.
- `make seed` prints `users: 2`, but that counts subscribers. 3 users exist.
- The embedding model id is exact (`EMBED_MODEL`). Any other id silently
  switches search to words, which now shows as a `warning`.
