"""
Tests for enphase.py — uses respx to mock httpx.
"""

import sys
import os
import json
import pytest
import httpx
import respx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enphase  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

BASE_URL = "https://enlighten.enphaseenergy.com"
DEFAULT_SITE_ID = "3687112"


@pytest.fixture(autouse=True)
def reset_client(monkeypatch):
    """Reset the module-level singleton before and after every test."""
    monkeypatch.setattr("enphase._client", None)
    yield
    monkeypatch.setattr("enphase._client", None)


@pytest.fixture
def env_vars(monkeypatch):
    """Set required env vars for every test that uses this fixture."""
    monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
    monkeypatch.setenv("ENPHASE_PASSWORD", "secret123")
    # Restore the module-level SITE_ID to the default for isolation
    monkeypatch.setattr("enphase.SITE_ID", DEFAULT_SITE_ID)


# ===========================================================================
# EnphaseClient.__init__ validation
# ===========================================================================

class TestEnphaseClientInit:

    def test_raises_when_email_missing(self, monkeypatch):
        monkeypatch.delenv("ENPHASE_EMAIL", raising=False)
        monkeypatch.setenv("ENPHASE_PASSWORD", "secret")
        with pytest.raises(ValueError, match="ENPHASE_EMAIL"):
            enphase.EnphaseClient()

    def test_raises_when_password_missing(self, monkeypatch):
        monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
        monkeypatch.delenv("ENPHASE_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="ENPHASE_PASSWORD"):
            enphase.EnphaseClient()


# ===========================================================================
# get_client singleton
# ===========================================================================

class TestGetClient:

    def test_returns_enphase_client_instance(self, env_vars):
        client = enphase.get_client()
        assert isinstance(client, enphase.EnphaseClient)

    def test_returns_same_singleton_on_second_call(self, env_vars):
        c1 = enphase.get_client()
        c2 = enphase.get_client()
        assert c1 is c2


# ===========================================================================
# get_tariff — URL construction and response parsing
# ===========================================================================

class TestGetTariff:

    @respx.mock
    async def test_calls_correct_url_with_country_param(self, env_vars):
        tariff_url = f"{BASE_URL}/app-api/{DEFAULT_SITE_ID}/tariff.json"
        expected_response = {"tariff": {"seasons": []}}

        respx.get(tariff_url).mock(
            return_value=httpx.Response(200, json=expected_response)
        )

        client = enphase.EnphaseClient()
        result = await client.get_tariff()
        assert result == expected_response

        # Verify the country=us query param was sent
        request = respx.calls.last.request
        assert "country=us" in str(request.url)

    @respx.mock
    async def test_custom_site_id_used_in_url(self, monkeypatch):
        """ENPHASE_SITE_ID env var overrides the default site ID."""
        monkeypatch.setenv("ENPHASE_EMAIL", "user@example.com")
        monkeypatch.setenv("ENPHASE_PASSWORD", "secret123")
        custom_id = "9999999"
        monkeypatch.setattr("enphase.SITE_ID", custom_id)

        tariff_url = f"{BASE_URL}/app-api/{custom_id}/tariff.json"
        respx.get(tariff_url).mock(
            return_value=httpx.Response(200, json={"tariff": {}})
        )

        client = enphase.EnphaseClient()
        result = await client.get_tariff()
        assert result == {"tariff": {}}

    @respx.mock
    async def test_returns_parsed_json(self, env_vars):
        tariff_url = f"{BASE_URL}/app-api/{DEFAULT_SITE_ID}/tariff.json"
        payload = {"tariff": {"seasons": [{"start_month": 1}]}}
        respx.get(tariff_url).mock(return_value=httpx.Response(200, json=payload))

        client = enphase.EnphaseClient()
        result = await client.get_tariff()
        assert result == payload


# ===========================================================================
# _request — 401 retry logic
# ===========================================================================

class TestRequestRetry:

    @respx.mock
    async def test_retries_after_401_and_succeeds(self, env_vars):
        """First GET → 401 → login POST → second GET → 200."""
        tariff_url = f"{BASE_URL}/app-api/{DEFAULT_SITE_ID}/tariff.json"
        good_response = {"tariff": {"seasons": []}}

        # Alternate: 401 first, then 200
        call_count = {"n": 0}

        def side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(401)
            return httpx.Response(200, json=good_response)

        respx.get(tariff_url).mock(side_effect=side_effect)
        respx.post(f"{BASE_URL}/login/login").mock(return_value=httpx.Response(200))

        client = enphase.EnphaseClient()
        result = await client.get_tariff()
        assert result == good_response
        assert call_count["n"] == 2

    @respx.mock
    async def test_raises_on_non_401_error(self, env_vars):
        """Non-401 HTTP errors (e.g. 500) propagate as HTTPStatusError."""
        tariff_url = f"{BASE_URL}/app-api/{DEFAULT_SITE_ID}/tariff.json"
        respx.get(tariff_url).mock(return_value=httpx.Response(500))

        client = enphase.EnphaseClient()
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_tariff()


# ===========================================================================
# _csrf_token
# ===========================================================================

class TestCsrfToken:

    @respx.mock
    async def test_returns_stripped_token_text_on_success(self, env_vars):
        token_url = f"{BASE_URL}/service/auth_ms_enho/api/v1/session/token"
        respx.get(token_url).mock(
            return_value=httpx.Response(200, text='"abc-token-123"\n')
        )

        client = enphase.EnphaseClient()
        token = await client._csrf_token()
        assert token == "abc-token-123"

    @respx.mock
    async def test_csrf_token_calls_login_on_401_then_retries(self, env_vars):
        """_csrf_token: 401 → login → retry → 200."""
        token_url = f"{BASE_URL}/service/auth_ms_enho/api/v1/session/token"
        login_url = f"{BASE_URL}/login/login"

        call_count = {"n": 0}

        def token_side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(401)
            return httpx.Response(200, text='"fresh-token"')

        respx.get(token_url).mock(side_effect=token_side_effect)
        respx.post(login_url).mock(return_value=httpx.Response(200))

        client = enphase.EnphaseClient()
        token = await client._csrf_token()
        assert token == "fresh-token"
        assert call_count["n"] == 2
