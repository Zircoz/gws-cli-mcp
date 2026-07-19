---
"@googleworkspace/cli": major
---

Harden the Remote MCP Server (`remote-mcp-server/python/`) for multi-tenant deployments. **This is a breaking change**: deployments that previously ran with `ENABLE_AUTH` unset or set to an ambiguous value (e.g. `1`) will now refuse to start until `ENABLE_AUTH` is set to exactly `true`/`false`, or `ALLOW_INSECURE_NO_AUTH=true` is explicitly set.

- Add opt-in per-request credential isolation (`GWS_PER_USER_TOKEN=true`): each call executes with the calling user's own Google access token — extracted per-request from a validated JWT claim or forwarded header and injected into just that one `gws` child process via a `contextvars.ContextVar` — instead of every caller sharing the host's identity. An empty/missing per-user token now fails closed with an error rather than falling back to host credentials. `GWS_PER_USER_TOKEN` itself must also be exactly `true`/`false`/unset — an ambiguous value fails closed (falls back to host-identity mode) rather than silently claiming to be on.
- Close the `GWS_ALLOWED_SERVICES` allowlist's `service:version` Discovery escape hatch and the `--api-version` flag escape hatch (which repoints an allowed service at a different Google API sharing the same canonical name), and make the allowlist alias-aware, so `reports`/`admin-reports` (and other Workspace service aliases) are treated identically instead of one spelling silently bypassing the other.
- `ENABLE_AUTH` now must be explicitly `true` or `false`; an unset or ambiguous value (e.g. `1`, `yes`) refuses to start unless `ALLOW_INSECURE_NO_AUTH=true` is explicitly set, instead of silently disabling authentication.
- An explicit empty `REQUIRED_SCOPES` with auth enabled now refuses to start unless `REQUIRE_SCOPES=false` is explicitly set, instead of silently accepting any authenticated token regardless of scope. The scope check also now accepts Okta-style `scp` array claims, not just the standard space-delimited `scope` string.
- `execute_gws` now logs only the service and a resource+verb pair, never the full command, `--params`, or `args` content, which can carry request PII.
- `/docs` and `/openapi.json` are no longer mounted by default (`EXPOSE_API_DOCS=true` to opt back in).
- Add a `pytest` test suite (`remote-mcp-server/python/tests/`), now run in CI, covering all of the above.
