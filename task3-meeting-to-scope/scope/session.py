"""Demo accounts: a signed session cookie shared by the three services.

Signing in is optional. Without it, a visitor uses the shared "guest" workspace; with a demo
account, their inbox and scope projects are their own and persist across visits. The cookie
is `account.expiry.signature` (HMAC-SHA256 with SESSION_SECRET), so any service can check it
without a shared database. The same format is implemented in the Inbox Agent (src/session.ts)
and the Scope Builder (scope/session.py).
"""

import hashlib
import hmac
import os
import time

COOKIE = "bl_session"
GUEST = "guest"
MAX_AGE_S = 30 * 24 * 3600

# Preset demo accounts. The passwords are shown on the sign-in screen: they separate
# workspaces for a demo, they don't protect anything secret.
DEMO_ACCOUNTS = {f"demo{i}": f"bd-demo-{i}" for i in range(1, 6)}


def secret() -> bytes:
    return (
        os.environ.get("SESSION_SECRET") or os.environ.get("KB_API_KEY") or "change-me"
    ).encode()


def _sign(payload: str) -> str:
    return hmac.new(secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(account: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + MAX_AGE_S)
    payload = f"{account}.{expires}"
    return f"{payload}.{_sign(payload)}"


def account_from_token(token: str | None, now: float | None = None) -> str | None:
    """The signed-in account, or None for a missing, forged, expired or unknown session."""
    if not token or token.count(".") != 2:
        return None
    account, expires, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(f"{account}.{expires}")):
        return None
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return None
    return account if account in DEMO_ACCOUNTS else None


def check_password(account: str, password: str) -> bool:
    expected = DEMO_ACCOUNTS.get(account)
    return expected is not None and hmac.compare_digest(expected, password)


def cookie_domain(host: str | None) -> str | None:
    """Share the cookie across kb. / inbox. / scope. subdomains of one host. None (host-only)
    for localhost and plain IPs, where a Domain attribute isn't allowed."""
    if not host:
        return None
    host = host.split(":", 1)[0].lower()
    for prefix in ("kb.", "inbox.", "scope."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if host in ("localhost", "127.0.0.1") or host.replace(".", "").isdigit() or "." not in host:
        return None
    return host
