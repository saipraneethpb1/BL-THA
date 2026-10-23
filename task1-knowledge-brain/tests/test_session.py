"""Demo accounts: signed sessions, sign-in, and settings that need a signed-in account."""

import pytest
from fastapi.testclient import TestClient

from kb import api, session
from tests.test_api import H, client  # noqa: F401  (shared fixture)


def test_tokens_are_signed_expire_and_name_a_real_account() -> None:
    token = session.make_token("demo2", now=1_000)
    assert session.account_from_token(token, now=1_000 + 60) == "demo2"
    assert session.account_from_token(token, now=1_000 + session.MAX_AGE_S + 1) is None  # expired
    account, expires, sig = token.split(".")
    assert session.account_from_token(f"demo3.{expires}.{sig}", now=1_100) is None  # forged
    assert session.account_from_token(f"{account}.{expires}.{'0' * 64}", now=1_100) is None
    other = session.make_token("admin", now=1_000)
    assert session.account_from_token(other, now=1_100) is None  # not a demo account
    assert (
        session.account_from_token("garbage") is None and session.account_from_token(None) is None
    )


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("kb.13-203-203-28.sslip.io", "13-203-203-28.sslip.io"),
        ("inbox.13-203-203-28.sslip.io", "13-203-203-28.sslip.io"),
        ("13-203-203-28.sslip.io", "13-203-203-28.sslip.io"),
        ("localhost:8001", None),
        ("kb.localhost", None),
        ("127.0.0.1:8001", None),
    ],
)
def test_the_cookie_is_shared_across_the_subdomains(host: str, domain: str | None) -> None:
    assert session.cookie_domain(host) == domain


def test_sign_in_sign_out_and_who_am_i(client: TestClient) -> None:  # noqa: F811
    api.login_limiter.calls.clear()
    me = client.get("/auth/me").json()
    assert me["account"] is None and me["workspace"] == "guest"
    assert {"account": "demo1", "password": "bd-demo-1"} in me["demo_accounts"]
    assert (
        client.post("/auth/login", json={"account": "demo1", "password": "wrong"}).status_code
        == 401
    )
    ok = client.post("/auth/login", json={"account": " Demo1 ", "password": "bd-demo-1"})
    assert ok.status_code == 200 and ok.json() == {"account": "demo1"}
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert client.get("/auth/me").json()["workspace"] == "demo1"
    client.post("/auth/logout")
    assert client.get("/auth/me").json()["account"] is None


def test_public_visitors_must_sign_in_to_change_shared_settings(client: TestClient) -> None:  # noqa: F811
    api.login_limiter.calls.clear()
    visitor = {**H, "X-Forwarded-For": "203.0.113.7"}
    body = {"threshold": 4, "action_gone": "quarantine", "action_moved": "flag"}
    refused = client.put("/url-health/settings", json=body, headers=visitor)
    assert refused.status_code == 401 and "Sign in" in refused.json()["detail"]
    assert (
        client.put("/url-health/settings", json=body, headers=H).status_code == 200
    )  # internal call
    client.post(
        "/auth/login",
        json={"account": "demo4", "password": "bd-demo-4"},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert client.put("/url-health/settings", json=body, headers=visitor).status_code == 200
    assert client.get("/url-health", headers=visitor).status_code == 200  # reading needs no sign-in
