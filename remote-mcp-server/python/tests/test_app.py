"""Tests for app.py's fail-closed startup guards and per-user token
extraction. app.py performs its ENABLE_AUTH/REQUIRED_SCOPES validation at
import time (so a misconfigured deployment refuses to start at all); the
validation logic itself is extracted into pure helper functions so it can be
exercised here without re-importing (and re-triggering the import-time
side effects of) the module for every case.
"""
import pytest

import app


class TestResolveEnableAuth:
    def test_explicit_true(self):
        assert app._resolve_enable_auth("true", allow_insecure=False) is True

    def test_explicit_false_does_not_require_opt_in(self):
        # Matches the documented local-dev workflow: ENABLE_AUTH=false alone
        # (no ALLOW_INSECURE_NO_AUTH) is an explicit, intentional choice.
        assert app._resolve_enable_auth("false", allow_insecure=False) is False

    def test_case_insensitive(self):
        assert app._resolve_enable_auth("True", allow_insecure=False) is True
        assert app._resolve_enable_auth("FALSE", allow_insecure=False) is False

    def test_unset_without_opt_in_raises(self):
        with pytest.raises(RuntimeError, match="unset or ambiguous"):
            app._resolve_enable_auth(None, allow_insecure=False)

    def test_empty_string_without_opt_in_raises(self):
        with pytest.raises(RuntimeError):
            app._resolve_enable_auth("", allow_insecure=False)

    @pytest.mark.parametrize("value", ["1", "0", "yes", "no", "enabled", "TRUEISH"])
    def test_ambiguous_values_without_opt_in_raise(self, value):
        with pytest.raises(RuntimeError):
            app._resolve_enable_auth(value, allow_insecure=False)

    def test_unset_with_opt_in_disables_auth(self):
        assert app._resolve_enable_auth(None, allow_insecure=True) is False

    def test_ambiguous_with_opt_in_disables_auth(self):
        assert app._resolve_enable_auth("1", allow_insecure=True) is False


class TestResolveRequiredScopes:
    def test_unset_uses_documented_default(self):
        assert app._resolve_required_scopes(None, enable_auth=True, opt_out=False) == [
            "gws:read",
            "gws:write",
        ]

    def test_non_empty_value_used_as_is(self):
        assert app._resolve_required_scopes(
            "gws:execute", enable_auth=True, opt_out=False
        ) == ["gws:execute"]

    def test_empty_with_auth_on_and_no_opt_out_raises(self):
        with pytest.raises(RuntimeError, match="empty string"):
            app._resolve_required_scopes("", enable_auth=True, opt_out=False)

    def test_empty_with_explicit_opt_out_is_allowed(self):
        assert app._resolve_required_scopes("", enable_auth=True, opt_out=True) == []

    def test_empty_with_auth_off_is_allowed(self):
        assert app._resolve_required_scopes("", enable_auth=False, opt_out=False) == []


class TestPerUserTokenExtraction:
    def test_extract_from_header(self, monkeypatch):
        monkeypatch.setattr(app, "GWS_USER_TOKEN_CLAIM", "")
        headers = {b"x-gws-user-token": b"user-token-from-header"}
        assert (
            app.ASGIAuthMiddleware._extract_user_token(headers, payload={})
            == "user-token-from-header"
        )

    def test_missing_header_returns_none(self, monkeypatch):
        monkeypatch.setattr(app, "GWS_USER_TOKEN_CLAIM", "")
        assert app.ASGIAuthMiddleware._extract_user_token({}, payload={}) is None

    def test_blank_header_returns_none(self, monkeypatch):
        monkeypatch.setattr(app, "GWS_USER_TOKEN_CLAIM", "")
        headers = {b"x-gws-user-token": b"   "}
        assert app.ASGIAuthMiddleware._extract_user_token(headers, payload={}) is None

    def test_extract_from_jwt_claim_takes_priority(self, monkeypatch):
        monkeypatch.setattr(app, "GWS_USER_TOKEN_CLAIM", "google_token")
        headers = {b"x-gws-user-token": b"should-be-ignored"}
        payload = {"google_token": "token-from-claim"}
        assert (
            app.ASGIAuthMiddleware._extract_user_token(headers, payload)
            == "token-from-claim"
        )

    def test_claim_missing_returns_none(self, monkeypatch):
        monkeypatch.setattr(app, "GWS_USER_TOKEN_CLAIM", "google_token")
        assert app.ASGIAuthMiddleware._extract_user_token({}, payload={}) is None


class TestDocsGatingDefaultsClosed:
    def test_docs_disabled_by_default(self):
        # Imported under the conftest's default env, which does not set
        # EXPOSE_API_DOCS, so it must default to disabled.
        assert app.EXPOSE_API_DOCS is False
        assert app.app.docs_url is None
        assert app.app.openapi_url is None
        assert "/docs" not in app._UNAUTHENTICATED_PATHS
        assert "/openapi.json" not in app._UNAUTHENTICATED_PATHS
        assert "/health" in app._UNAUTHENTICATED_PATHS


class TestBuildUnauthenticatedPaths:
    def test_docs_disabled(self):
        paths = app._build_unauthenticated_paths(expose_docs=False)
        assert paths == {"/health"}

    def test_docs_enabled_includes_oauth2_redirect(self):
        """FastAPI's Swagger "Authorize" flow redirects the browser to
        /docs/oauth2-redirect with no Bearer header — it needs the same
        bypass as /docs itself, or completing OAuth login from the docs UI
        401s even though /docs renders fine."""
        paths = app._build_unauthenticated_paths(expose_docs=True)
        assert paths == {"/health", "/docs", "/openapi.json", "/docs/oauth2-redirect"}


class TestExtractTokenScopes:
    def test_standard_space_delimited_scope_string(self):
        assert app._extract_token_scopes({"scope": "gws:read gws:write"}) == [
            "gws:read",
            "gws:write",
        ]

    def test_okta_style_scp_array(self):
        """Okta — an IdP this server's docs explicitly support — commonly
        issues scopes as an "scp" array claim instead of the standard
        space-delimited "scope" string."""
        assert app._extract_token_scopes({"scp": ["gws:read", "gws:write"]}) == [
            "gws:read",
            "gws:write",
        ]

    def test_scope_string_takes_priority_over_scp(self):
        payload = {"scope": "gws:read", "scp": ["gws:write"]}
        assert app._extract_token_scopes(payload) == ["gws:read"]

    def test_missing_both_claims_returns_empty(self):
        assert app._extract_token_scopes({}) == []

    def test_non_list_scp_ignored(self):
        assert app._extract_token_scopes({"scp": "not-a-list"}) == []


class TestCheckPerUserTokenPreconditions:
    def test_per_user_mode_off_is_always_fine(self):
        app._check_per_user_token_preconditions(
            per_user_token_mode=False, enable_auth=False, stateless_http=False
        )  # must not raise

    def test_per_user_mode_on_with_auth_and_stateless_is_fine(self):
        app._check_per_user_token_preconditions(
            per_user_token_mode=True, enable_auth=True, stateless_http=True
        )  # must not raise

    def test_per_user_mode_without_auth_raises(self):
        with pytest.raises(RuntimeError, match="requires ENABLE_AUTH=true"):
            app._check_per_user_token_preconditions(
                per_user_token_mode=True, enable_auth=False, stateless_http=True
            )

    def test_per_user_mode_without_stateless_http_raises(self):
        """USER_TOKEN's cross-request isolation depends on stateless_http:
        without it, the mcp SDK's stateful path can run tool calls in a
        long-lived session task whose context was captured at
        session-creation time, letting a later caller silently reuse an
        earlier caller's token instead of failing closed."""
        with pytest.raises(RuntimeError, match="stateless_http=True"):
            app._check_per_user_token_preconditions(
                per_user_token_mode=True, enable_auth=True, stateless_http=False
            )


class TestAuthorizationHeaderDecoding:
    async def test_invalid_utf8_bytes_return_401_not_500(self):
        """The Authorization header must be decoded with errors="replace":
        HTTP field values may legally contain obs-text bytes (RFC 7230)
        that aren't valid UTF-8, and a strict decode would raise
        UnicodeDecodeError here — producing an unhandled 500 instead of the
        intended 401 for what is just an invalid/malformed token."""
        middleware = app.ASGIAuthMiddleware(app=None)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer \xe9\xffnot-a-real-jwt")],
        }

        await middleware(scope, receive, send)

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 401


class TestExceptionTextNotLeakedToClient:
    async def test_invalid_token_response_does_not_echo_exception_text(self, monkeypatch, caplog):
        """The exception detail must be logged server-side only — echoing
        str(e) to the client can leak internals (e.g. PyJWKClient
        connection errors embed the configured JWKS URI and network error
        text)."""

        class ExplodingJWKSClient:
            def get_signing_key_from_jwt(self, token):
                raise RuntimeError(
                    f"Fail to fetch data from the url, err: connection refused to {app.JWKS_URI}"
                )

        monkeypatch.setattr(app, "get_jwks_client", lambda: ExplodingJWKSClient())

        middleware = app.ASGIAuthMiddleware(app=None)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer some.fake.jwt")],
        }

        import logging

        with caplog.at_level(logging.WARNING):
            await middleware(scope, receive, send)

        assert sent[0]["status"] == 401
        body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
        assert app.JWKS_URI.encode() not in body
        assert b"connection refused" not in body
        assert b"Invalid token" in body
        # The detail is still available server-side, for operators, in logs.
        assert any(app.JWKS_URI in record.message for record in caplog.records)
