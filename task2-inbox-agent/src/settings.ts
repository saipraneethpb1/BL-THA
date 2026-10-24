/**
 * Each inbox's own settings: X (unedited approvals in a row before a category auto-sends),
 * the email categories themselves (topics.ts, each with its own auto-send switch), and
 * which request types may ever auto-send (refunds, complaints... start switched off).
 * Stored in the inbox's database, so every account keeps its own. A change applies at once
 * to every category (autonomy.reconsider).
 */
import { reconsider, type AutonomyEvent } from "./feedback/autonomy.js";
import { CATEGORY_IDS, eligibleByDefault, type CategoryId } from "./email/taxonomy.js";
import { readTopics, saveTopics, type Topic, type TopicIn } from "./email/topics.js";
import type { InboxStore } from "./store/db.js";

export const THRESHOLD_MIN = 1;
export const THRESHOLD_MAX = 100;

export interface InboxSettings {
  autonomyThreshold: number;
  /** Request types that may auto-send at all (the safety layer). */
  autoSend: Record<CategoryId, boolean>;
  topics: Topic[];
}

export interface SettingsPatch {
  autonomyThreshold?: number | undefined;
  autoSend?: Partial<Record<CategoryId, boolean>> | undefined;
  topics?: TopicIn[] | undefined;
}

export function readSettings(store: InboxStore, defaultThreshold: number): InboxSettings {
  const t = store.getSetting("autonomy_threshold");
  const saved = (store.getSetting("auto_send") ?? {}) as Partial<Record<CategoryId, boolean>>;
  const autoSend = Object.fromEntries(
    CATEGORY_IDS.map((k) => [k, typeof saved[k] === "boolean" ? saved[k] : eligibleByDefault(k)]),
  ) as Record<CategoryId, boolean>;
  return { autonomyThreshold: typeof t === "number" ? t : defaultThreshold, autoSend, topics: readTopics(store) };
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
  const topics = patch.topics ? saveTopics(store, patch.topics) : before.topics;
  const settings: InboxSettings = {
    autonomyThreshold: t ?? before.autonomyThreshold,
    autoSend: { ...before.autoSend, ...patch.autoSend },
    topics,
  };
  store.setSetting("autonomy_threshold", settings.autonomyThreshold);
  // Only the labels that differ from their default are stored, so new defaults still apply.
  store.setSetting("auto_send", Object.fromEntries(CATEGORY_IDS.filter((k) => settings.autoSend[k] !== eligibleByDefault(k)).map((k) => [k, settings.autoSend[k]])));

  const events: AutonomyEvent[] = [];
  const changes: string[] = [];
  if (settings.autonomyThreshold !== before.autonomyThreshold) changes.push(`threshold ${before.autonomyThreshold} → ${settings.autonomyThreshold}`);
  for (const k of CATEGORY_IDS) {
    if (settings.autoSend[k] !== before.autoSend[k]) changes.push(`${k} requests auto-send ${settings.autoSend[k] ? "on" : "off"}`);
  }
  const was = new Map(before.topics.map((x) => [x.id, x]));
  for (const x of topics) {
    const old = was.get(x.id);
    if (!old) changes.push(`category "${x.label}" added`);
    else {
      if (old.label !== x.label || old.parent !== x.parent) changes.push(`"${old.label}" is now ${x.parent} › ${x.label}`);
      else if (old.description !== x.description) changes.push(`"${x.label}" description changed`);
      if (old.auto_send !== x.auto_send) changes.push(`"${x.label}" auto-send ${x.auto_send ? "on" : "off"}`);
    }
  }
  for (const old of before.topics) if (!topics.some((x) => x.id === old.id)) changes.push(`category "${old.label}" removed`);
  if (changes.length) {
    store.logAutonomyEvent("settings", "settings_changed", `${changes.join(", ")} by ${actor}`);
  }
  for (const x of topics) {
    const next = reconsider(store.autonomy(x.id), { eligible: x.auto_send, threshold: settings.autonomyThreshold });
    if (next.events.length) {
      store.saveAutonomy(next.state, next.events, null);
      events.push(...next.events);
    }
  }
  return { settings, events };
}
