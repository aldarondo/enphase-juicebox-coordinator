"""
Enphase Enlighten API client for the coordinator.

Handles auth (login → session cookie → CSRF token) and exposes the two
calls the coordinator needs: get_tariff() and get_status().

Auth logic mirrors claude-enphase/auth.py — if credentials rotate or the
login flow changes, keep both files in sync.
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://enlighten.enphaseenergy.com"
SITE_ID   = os.getenv("ENPHASE_SITE_ID", "3687112")
LOGIN_URL = f"{BASE_URL}/login/login"
TOKEN_URL = f"{BASE_URL}/service/auth_ms_enho/api/v1/session/token"


class EnphaseClient:
    def __init__(self):
        self.email    = os.getenv("ENPHASE_EMAIL")
        self.password = os.getenv("ENPHASE_PASSWORD")
        if not self.email or not self.password:
            raise ValueError("ENPHASE_EMAIL and ENPHASE_PASSWORD must be set in .env")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    async def login(self) -> None:
        client = await self._get_client()
        resp = await client.post(
            LOGIN_URL,
            data={"user[email]": self.email, "user[password]": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

    async def _csrf_token(self) -> str:
        client = await self._get_client()
        resp = await client.get(TOKEN_URL)
        if resp.status_code == 401:
            await self.login()
            resp = await client.get(TOKEN_URL)
        resp.raise_for_status()
        return resp.text.strip().strip('"')

    async def _request(self, method: str, url: str, *, params: dict | None = None, retry: bool = True) -> dict:
        client = await self._get_client()
        resp = await client.request(method, url, params=params)
        if resp.status_code == 401 and retry:
            await self.login()
            return await self._request(method, url, params=params, retry=False)
        resp.raise_for_status()
        return resp.json()

    async def get_tariff(self) -> dict:
        """Full TOU rate structure — seasonal tiers, rates, and hourly schedules."""
        return await self._request("GET", f"/app-api/{SITE_ID}/tariff.json", params={"country": "us"})

    async def get_status(self) -> dict:
        """Battery SOC, active profile, and today's energy totals."""
        from datetime import date as _date
        today = await self._request("GET", f"/pv/systems/{SITE_ID}/today")
        battery = await self._request(
            "GET",
            f"/service/batteryConfig/api/v1/batterySettings/{SITE_ID}",
            params={"source": "enho", "userId": os.getenv("ENPHASE_USER_ID", "3263059")},
        )
        intervals = today.get("intervals", [])
        latest = intervals[-1] if intervals else {}
        return {
            "battery_soc_pct":      today.get("battery_soc") or latest.get("soc") or 0,
            "battery_profile":      battery.get("usage"),
            "solar_produced_wh":    today.get("energy_produced"),
            "consumed_wh":          today.get("energy_consumed"),
            "battery_charged_wh":   today.get("energy_charged"),
            "battery_discharged_wh": today.get("energy_discharged"),
        }

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# Module-level singleton
_client: EnphaseClient | None = None

def get_client() -> EnphaseClient:
    global _client
    if _client is None:
        _client = EnphaseClient()
    return _client
