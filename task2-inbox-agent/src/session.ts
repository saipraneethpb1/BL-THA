/**
 * Demo accounts: the signed session cookie shared by the three services.
 * Same format as the Knowledge Brain (kb/session.py): `account.expiry.signature`, where the
 * signature is HMAC-SHA256 with SESSION_SECRET. No cookie (or a bad one) means "guest".
 */
import { createHmac, timingSafeEqual } from "node:crypto";

export const COOKIE = "bl_session";
export const GUEST = "guest";
export const DEMO_ACCOUNTS = ["demo1", "demo2", "demo3", "demo4", "demo5"] as const;

/** A Google login (Connect Gmail) gets a private workspace named from a hash of its Google id. */
export const GOOGLE_ACCOUNT = /^g_[0-9a-f]{16}$/;

export function isAccount(account: string): boolean {
  return (DEMO_ACCOUNTS as readonly string[]).includes(account) || GOOGLE_ACCOUNT.test(account);
}

export function sessionSecret(env: NodeJS.ProcessEnv = process.env): string {
  return env.SESSION_SECRET || env.KB_API_KEY || "change-me";
}

function sign(secret: string, payload: string): string {
  return createHmac("sha256", secret).update(payload).digest("hex");
}

export function makeToken(secret: string, account: string, nowS = Date.now() / 1000): string {
  const payload = `${account}.${Math.floor(nowS + 30 * 24 * 3600)}`;
  return `${payload}.${sign(secret, payload)}`;
}

/** The signed-in demo account, or null for a missing, forged, expired or unknown session. */
export function accountFromToken(secret: string, token: string | undefined, nowS = Date.now() / 1000): string | null {
  const parts = (token ?? "").split(".");
  if (parts.length !== 3) return null;
  const [account, expires, sig] = parts as [string, string, string];
  const expected = Buffer.from(sign(secret, `${account}.${expires}`));
  const given = Buffer.from(sig);
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
  if (!/^\d+$/.test(expires) || Number(expires) < nowS) return null;
  return isAccount(account) ? account : null;
}

/** Same rule as kb/session.py: share cookies across kb. / inbox. / scope. of one host;
 *  host-only (undefined) for localhost and plain IPs. */
export function cookieDomain(host: string | undefined): string | undefined {
  if (!host) return undefined;
  let h = host.split(":")[0]!.toLowerCase();
  for (const prefix of ["kb.", "inbox.", "scope."]) {
    if (h.startsWith(prefix)) { h = h.slice(prefix.length); break; }
  }
  if (h === "localhost" || h === "127.0.0.1" || /^[\d.]+$/.test(h) || !h.includes(".")) return undefined;
  return h;
}

export function cookieValue(header: string | undefined, name = COOKIE): string | undefined {
  for (const part of (header ?? "").split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return decodeURIComponent(rest.join("="));
  }
  return undefined;
}
