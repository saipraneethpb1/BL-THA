/**
 * How close is a draft to what the human actually sent? Word-level longest common
 * subsequence, scaled to 0..1 (1 = identical wording). Cheap, deterministic, and it
 * rewards exactly what reviewers care about: fewer words they had to change.
 */
export function words(text: string): string[] {
  return text.toLowerCase().match(/[\p{L}\p{N}']+/gu) ?? [];
}

export function similarity(a: string, b: string): number {
  const x = words(a);
  const y = words(b);
  if (x.length === 0 && y.length === 0) return 1;
  if (x.length === 0 || y.length === 0) return 0;
  let prev = new Uint16Array(y.length + 1);
  let cur = new Uint16Array(y.length + 1);
  for (let i = 1; i <= x.length; i++) {
    for (let j = 1; j <= y.length; j++) {
      cur[j] = x[i - 1] === y[j - 1] ? (prev[j - 1] ?? 0) + 1 : Math.max(prev[j] ?? 0, cur[j - 1] ?? 0);
    }
    [prev, cur] = [cur, prev];
    cur.fill(0);
  }
  return (2 * (prev[y.length] ?? 0)) / (x.length + y.length);
}

export function mean(xs: number[]): number {
  return xs.length === 0 ? 0 : xs.reduce((s, v) => s + v, 0) / xs.length;
}
