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
