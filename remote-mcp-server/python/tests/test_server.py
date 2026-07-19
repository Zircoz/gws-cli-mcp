"""Tests for server.py's execute_gws: service allowlist enforcement,
per-request credential isolation, and logging content.
"""
import asyncio
import logging

import pytest

import server
from tests.conftest import FakeProcess


# --- Service allowlist boundary (GWS_ALLOWED_SERVICES hardening) ---


async def test_colon_syntax_rejected_when_unrestricted(monkeypatch):
    """`service:version` must be closed even with an unrestricted allowlist —
    it's how gws reaches arbitrary (non-Workspace) Discovery APIs."""
    monkeypatch.setattr(server, "ALLOWED_SERVICES", None)
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", None)

    async def fail_spawn(*a, **k):
        pytest.fail("must not spawn a subprocess for a colon-syntax service")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    result = await server.execute_gws(service="compute:v1", command="instances list")
    assert "Discovery syntax" in result


async def test_colon_syntax_rejected_when_restricted(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"drive"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"drive"})

    result = await server.execute_gws(service="cloudresourcemanager:v3", command="projects list")
    assert "Discovery syntax" in result


async def test_non_allowed_service_rejected(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"drive"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"drive"})

    result = await server.execute_gws(service="gmail", command="messages list")
    assert "not permitted" in result
    assert "drive" in result


async def test_alias_and_canonical_are_interchangeable(monkeypatch):
    """Allowing "reports" (an alias) must also allow "admin-reports" (the
    other alias for the same canonical "admin" service), and vice versa —
    the allowlist must not be bypassable/blockable by alias spelling."""
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"reports"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"admin"})

    async def fake_spawn(*cmd, stdout, stderr, env):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    result = await server.execute_gws(service="admin-reports", command="activities list")
    assert "not permitted" not in result


async def test_auth_always_blocked_even_if_added_to_allowlist(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"auth", "drive"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"auth", "drive"})

    result = await server.execute_gws(service="auth", command="export --unmasked")
    assert "meta-command" in result


async def test_schema_and_generate_skills_always_blocked(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", None)
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", None)

    for blocked in ("schema", "generate-skills"):
        result = await server.execute_gws(service=blocked, command="")
        assert "meta-command" in result


async def test_api_version_flag_rejected_in_args(monkeypatch):
    """--api-version overrides only the Discovery *version* while keeping
    the service's canonical api_name (crates/google-workspace-cli/src/
    main.rs's parse_service_and_version), so it can silently repoint an
    allowed service (e.g. "reports") at a completely different Google API
    that happens to share the same api_name (e.g. "admin"'s "directory_v1"
    instead of "reports_v1") without ever touching the `service` argument
    the allowlist checks. Must be rejected regardless of allowlist state."""
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"reports"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"admin"})

    async def fail_spawn(*a, **k):
        pytest.fail("must not spawn a subprocess when --api-version is present")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    result = await server.execute_gws(
        service="reports",
        command="users list",
        args=["--api-version", "directory_v1"],
    )
    assert "--api-version" in result
    assert "not permitted on this server" not in result  # rejected by the api-version check, not the allowlist


async def test_api_version_flag_rejected_equals_form_and_in_command(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", None)
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", None)

    async def fail_spawn(*a, **k):
        pytest.fail("must not spawn a subprocess when --api-version is present")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    result = await server.execute_gws(service="drive", command="files list --api-version=v2")
    assert "--api-version" in result


async def test_allowed_service_reaches_subprocess(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_SERVICES", {"drive"})
    monkeypatch.setattr(server, "ALLOWED_SERVICES_CANONICAL", {"drive"})
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", False)

    captured = {}

    async def fake_spawn(*cmd, stdout, stderr, env):
        captured["cmd"] = cmd
        return FakeProcess(stdout=b"file list output")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    result = await server.execute_gws(service="drive", command="files list")
    assert result == "file list output"
    assert captured["cmd"] == ("gws", "drive", "files", "list")


# --- Per-request credential isolation (GWS_PER_USER_TOKEN) ---


async def test_per_user_token_injected_into_child_env(monkeypatch):
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", True)
    captured = {}

    async def fake_spawn(*cmd, stdout, stderr, env):
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    server.USER_TOKEN.set("user-a-token")
    await server.execute_gws(service="drive", command="about get")

    assert captured["env"]["GOOGLE_WORKSPACE_CLI_TOKEN"] == "user-a-token"


async def test_concurrent_requests_do_not_share_token(monkeypatch):
    """The core multi-tenant isolation guarantee: two concurrent calls on the
    same worker, carrying different per-user tokens, must each spawn a child
    with only its own token — never a shared/global value."""
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", True)
    seen_tokens = []

    async def fake_spawn(*cmd, stdout, stderr, env):
        # Yield control so the two concurrent calls actually interleave,
        # the way real concurrent requests handled by one worker would.
        await asyncio.sleep(0)
        seen_tokens.append(env["GOOGLE_WORKSPACE_CLI_TOKEN"])
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    async def call_as(token):
        server.USER_TOKEN.set(token)
        await asyncio.sleep(0)  # yield before spawning to force interleaving
        return await server.execute_gws(service="drive", command="about get")

    await asyncio.gather(call_as("token-for-user-a"), call_as("token-for-user-b"))

    assert sorted(seen_tokens) == ["token-for-user-a", "token-for-user-b"]


async def test_empty_token_fails_closed_no_host_fallback(monkeypatch):
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", True)

    async def fail_spawn(*a, **k):
        pytest.fail("must not spawn gws when per-user mode has no token")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    server.USER_TOKEN.set(None)
    result = await server.execute_gws(service="drive", command="about get")
    assert "Error" in result
    assert "per-user token" in result.lower()


async def test_host_identity_mode_unaffected_when_per_user_mode_off(monkeypatch):
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", False)
    monkeypatch.setenv("GWS_TEST_MARKER_VAR", "host-identity-marker")
    captured = {}

    async def fake_spawn(*cmd, stdout, stderr, env):
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    await server.execute_gws(service="drive", command="about get")

    assert captured["env"]["GWS_TEST_MARKER_VAR"] == "host-identity-marker"


# --- Logging must not include PII (params/args content) ---


async def test_logging_excludes_params_and_args(monkeypatch, caplog):
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", False)

    async def fake_spawn(*cmd, stdout, stderr, env):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    with caplog.at_level(logging.INFO):
        await server.execute_gws(
            service="gmail",
            command="messages send",
            params={"to": "secret@example.com", "body": "sensitive content"},
            args=["--raw", "supersecretpayload"],
        )

    log_text = "\n".join(record.message for record in caplog.records)
    assert "secret@example.com" not in log_text
    assert "sensitive content" not in log_text
    assert "supersecretpayload" not in log_text
    assert "gmail" in log_text
    assert "messages send" in log_text


async def test_logging_truncates_command_beyond_resource_and_verb(monkeypatch, caplog):
    """gws's own CLI surface never needs more than two tokens here — every
    Discovery-driven method takes its parameters via --params/--json, never
    positionally — so anything a caller appends beyond that (e.g. a resource
    ID mistakenly placed in `command` instead of `params`) must not reach
    the logs."""
    monkeypatch.setattr(server, "PER_USER_TOKEN_MODE", False)

    async def fake_spawn(*cmd, stdout, stderr, env):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    with caplog.at_level(logging.INFO):
        await server.execute_gws(
            service="drive", command="files get 1AbC_a_customers_secret_file_id"
        )

    log_text = "\n".join(record.message for record in caplog.records)
    assert "1AbC_a_customers_secret_file_id" not in log_text
    assert "files get" in log_text


# --- GWS_PER_USER_TOKEN strict parsing ---


def test_resolve_per_user_token_mode_unset_defaults_false():
    assert server._resolve_per_user_token_mode(None) is False
    assert server._resolve_per_user_token_mode("") is False


def test_resolve_per_user_token_mode_explicit_values():
    assert server._resolve_per_user_token_mode("true") is True
    assert server._resolve_per_user_token_mode("True") is True
    assert server._resolve_per_user_token_mode("false") is False
    assert server._resolve_per_user_token_mode("FALSE") is False


@pytest.mark.parametrize("value", ["1", "0", "yes", "no", "enabled"])
def test_resolve_per_user_token_mode_ambiguous_values_raise(value):
    """Unlike the other new boolean env vars, an ambiguous value here must
    fail loudly rather than silently disabling per-user credential
    isolation (the one direction that's actually unsafe)."""
    with pytest.raises(RuntimeError, match="not a recognized value"):
        server._resolve_per_user_token_mode(value)
