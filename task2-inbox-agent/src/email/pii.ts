/**
 * Customers sometimes paste card numbers or passwords into email. We remove them
 * before the text goes to any model, and the policy check makes sure a draft never
 * repeats them.
 */
export interface Secret {
  kind: "card_number" | "card_cvv" | "password";
  value: string;
}

const CARD_RE = /\b(?:\d[ -]?){13,19}\b/g;
const CVV_RE = /\b(?:cvv|cvc|security code)\s*[:#]?\s*(\d{3,4})\b/gi;
// The token must contain a digit or symbol, so "password reset link" is not flagged.
// Known limit: an all-letters password ("letmein") is not caught.
const PASSWORD_RE =
  /\b(?:password|passwd|pass|pwd)(?:\s+is)?\s*[:=]?\s+(?!is\b)((?=[^\s,;]*[^\sA-Za-z,;])[^\s,;]{4,})/gi;

function luhnValid(digits: string): boolean {
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = Number(digits[i]);
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    double = !double;
  }
  return sum % 10 === 0;
}

export function findSecrets(text: string): Secret[] {
  const found: Secret[] = [];
  for (const m of text.matchAll(CARD_RE)) {
    const digits = m[0].replace(/\D/g, "");
    if (digits.length >= 13 && luhnValid(digits)) found.push({ kind: "card_number", value: m[0].trim() });
  }
  for (const m of text.matchAll(CVV_RE)) if (m[1]) found.push({ kind: "card_cvv", value: m[1] });
  for (const m of text.matchAll(PASSWORD_RE)) if (m[1]) found.push({ kind: "password", value: m[1] });
  return found;
}

export function redact(text: string, secrets: readonly Secret[]): string {
  let out = text;
  for (const s of secrets) {
    const label = s.kind === "password" ? "[REDACTED PASSWORD]" : "[REDACTED CARD DETAILS]";
    out = out.split(s.value).join(label);
  }
  return out;
}
