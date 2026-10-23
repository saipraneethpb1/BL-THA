/**
 * Each inbox's own settings: X (unedited approvals in a row before a label auto-sends)
 * and which labels may ever auto-send. Stored in the inbox's database, so every demo
 * account keeps its own. A change applies at once to every label (autonomy.reconsider).
 */
import { reconsider, type AutonomyEvent } from "./feedback/autonomy.js";
import { CATEGORY_IDS, eligibleByDefault, type CategoryId } from "./email/taxonomy.js";
import type { InboxStore } from "./store/db.js";

export const THRESHOLD_MIN = 1;
export const THRESHOLD_MAX = 100;

export interface InboxSettings {
  autonomyThreshold: number;
  autoSend: Record<CategoryId, boolean>;
}

export interface SettingsPatch {
  autonomyThreshold?: number | undefined;
  autoSend?: Partial<Record<CategoryId, boolean>> | undefined;
}

export function readSettings(store: InboxStore, defaultThreshold: number): InboxSettings {
  const t = store.getSetting("autonomy_threshold");
  const saved = (store.getSetting("auto_send") ?? {}) as Partial<Record<CategoryId, boolean>>;
  const autoSend = Object.fromEntries(
    CATEGORY_IDS.map((k) => [k, typeof saved[k] === "boolean" ? saved[k] : eligibleByDefault(k)]),
  ) as Record<CategoryId, boolean>;
  return { autonomyThreshold: typeof t === "number" ? t : defaultThreshold, autoSend };
}

export function updateSettings(
  store: InboxStore,
  defaultThreshold: number,
  patch: SettingsPatch,
  actor: string,
): { settings: InboxSettings; events: AutonomyEvent[] } {
  const before = readSettings(store, defaultThreshold);
  const t = patch.autonomyThreshold;
  if (t !== undefined && (!Number.isInteger(t) || t < THRESHOLD_MIN || t > THRESHOLD_MAX)) {
    throw new RangeError(`threshold must be a whole number from ${THRESHOLD_MIN} to ${THRESHOLD_MAX}`);
  }
  const settings: InboxSettings = {
    autonomyThreshold: t ?? before.autonomyThreshold,
    autoSend: { ...before.autoSend, ...patch.autoSend },
  };
  store.setSetting("autonomy_threshold", settings.autonomyThreshold);
  // Only the labels that differ from their default are stored, so new defaults still apply.
  store.setSetting("auto_send", Object.fromEntries(CATEGORY_IDS.filter((k) => settings.autoSend[k] !== eligibleByDefault(k)).map((k) => [k, settings.autoSend[k]])));

  const events: AutonomyEvent[] = [];
  const changes: string[] = [];
  if (settings.autonomyThreshold !== before.autonomyThreshold) changes.push(`threshold ${before.autonomyThreshold} → ${settings.autonomyThreshold}`);
  for (const k of CATEGORY_IDS) {
    if (settings.autoSend[k] !== before.autoSend[k]) changes.push(`${k} auto-send ${settings.autoSend[k] ? "on" : "off"}`);
  }
  if (changes.length) {
    store.logAutonomyEvent("settings", "settings_changed", `${changes.join(", ")} by ${actor}`);
  }
  for (const k of CATEGORY_IDS) {
    const next = reconsider(store.autonomy(k), { eligible: settings.autoSend[k], threshold: settings.autonomyThreshold });
    if (next.events.length) {
      store.saveAutonomy(next.state, next.events, null);
      events.push(...next.events);
    }
  }
  return { settings, events };
}
