---
"@googleworkspace/cli": minor
---

Harden the Remote MCP Server (`remote-mcp-server/python/`) for multi-tenant deployments:

- Add opt-in per-request credential isolation (`GWS_PER_USER_TOKEN=true`): each call executes with the calling user's own Google access token — extracted per-request from a validated JWT claim or forwarded header and injected into just that one `gws` child process via a `contextvars.ContextVar` — instead of every caller sharing the host's identity. An empty/missing per-user token now fails closed with an error rather than falling back to host credentials.
- Close the `GWS_ALLOWED_SERVICES` allowlist's `service:version` Discovery escape hatch and make it alias-aware, so `reports`/`admin-reports` (and other Workspace service aliases) are treated identically instead of one spelling silently bypassing the other.
- `ENABLE_AUTH` now must be explicitly `true` or `false`; an unset or ambiguous value (e.g. `1`, `yes`) refuses to start unless `ALLOW_INSECURE_NO_AUTH=true` is explicitly set, instead of silently disabling authentication.
- An explicit empty `REQUIRED_SCOPES` with auth enabled now refuses to start unless `REQUIRE_SCOPES=false` is explicitly set, instead of silently accepting any authenticated token regardless of scope.
- `execute_gws` now logs only the service and command verb, never `--params`/`args` content, which can carry request PII.
- `/docs` and `/openapi.json` are no longer mounted by default (`EXPOSE_API_DOCS=true` to opt back in).
- Add a `pytest` test suite (`remote-mcp-server/python/tests/`) covering all of the above.
