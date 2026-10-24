/**
 * Email categories: which part of the customer's Brilliant Directories site an email is
 * about (Home Page, Header, Filters...). Each sits under its parent setting type: the area
 * of the BD admin where that part is managed (Design Settings, Members, Finance...). The
 * defaults follow the help center's own structure; every inbox can add, rename, re-describe
 * or remove categories in Settings, and the classifier is given the current list.
 *
 * The request type (taxonomy.ts: how-to, refund, complaint...) is kept alongside as the
 * safety layer: it decides what a reply must never do, and some types always need a person.
 * Unedited approvals are counted per category; a category auto-sends once it reaches X.
 */
import type { InboxStore } from "../store/db.js";

export interface Topic {
  id: string;
  label: string;
  /** The parent setting type: where in the BD admin this is managed. */
  parent: string;
  /** Shown to the classifier: what belongs here. */
  description: string;
  /** May this category auto-send once it has earned it? */
  auto_send: boolean;
}

export const OTHER = "other";
export const MAX_TOPICS = 40;

const t = (id: string, label: string, parent: string, description: string): Topic => ({
  id, label, parent, description, auto_send: true,
});

export const DEFAULT_TOPICS: readonly Topic[] = [
  t("home_page", "Home Page", "Design Settings", "The homepage: its layout, hero or slider, sections, featured members or content."),
  t("header", "Header", "Design Settings", "The site header: logo, top bar, header menu or buttons, sticky header."),
  t("footer", "Footer", "Design Settings", "The site footer: footer links, columns, copyright text, social icons in the footer."),
  t("colors_fonts", "Colors & Fonts", "Design Settings", "Theme, colours, fonts, global styling, custom CSS, mobile layout of the design."),
  t("menus", "Menus & Navigation", "Toolbox", "Menus and navigation links (Menu Manager), dropdowns, mobile menu."),
  t("widgets_sidebars", "Widgets & Sidebars", "Toolbox", "Widgets, sidebars, custom HTML widgets (Widget Manager, Sidebar Manager)."),
  t("forms", "Forms", "Toolbox", "Contact, signup or custom forms (Form Manager), form fields, form submissions."),
  t("web_pages", "Web Pages", "My Content", "Creating or editing pages with the Web Page Builder, landing pages, page content."),
  t("posts_blog", "Posts & Blog", "My Content", "Blog articles, events, classifieds, other post types, post settings and categories."),
  t("seo", "SEO", "My Content", "Search-engine optimisation: SEO templates, meta titles and descriptions, sitemaps, rankings."),
  t("search_filters", "Search & Filters", "Members", "Member search, search results, search filters, sorting, maps on search pages."),
  t("member_profiles", "Member Profiles", "Members", "Member listing or profile pages, profile fields, photos, reviews shown on profiles."),
  t("member_signup", "Member Signup & Import", "Members", "Member registration and signup flow, member categories, importing or exporting members."),
  t("membership_plans", "Membership Plans", "Finance", "Membership plans, plan features, upgrades and downgrades, free versus paid plans."),
  t("payments_billing", "Payments & Billing", "Finance", "Payment gateways, invoices, charges, subscriptions, coupons, refunds and cancellations."),
  t("leads_reviews", "Leads & Reviews", "Interactions", "Member leads, lead matching, quote requests from visitors, member reviews."),
  t("emails_newsletters", "Emails & Newsletters", "Emails", "Site emails and email templates, notifications not arriving, newsletters, email accounts."),
  t("domain_ssl", "Domain, SSL & Hosting", "Settings", "Domain name, DNS, SSL certificate, site down or slow, hosting, redirects, staging mode."),
  t("maps_location", "Maps & Location", "Settings", "Google Maps, location search, addresses, map API keys, geolocation."),
  t("admin_access", "Admin Login & Accounts", "Settings", "Logging in to the admin, passwords, admin accounts, site ownership, access for staff."),
  t("api_integrations", "API & Integrations", "Developer Hub", "API keys, webhooks, Zapier and other integrations, custom code, database access."),
  t("add_ons", "Add-ons", "Add-Ons", "Buying, enabling or using a BD add-on (chat, credits, multi-location, backups and others)."),
  t("project_work", "New Project or Quote", "Agency Services", "Asking our agency to build, redesign or change something, or for a price or estimate."),
  t(OTHER, "Other", "General", "Anything that fits no other category: thanks, feedback, general questions, no reply needed."),
];

export function slug(label: string): string {
  return label.toLowerCase().replace(/&/g, "and").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "category";
}

export function readTopics(store: InboxStore): Topic[] {
  const saved = store.getSetting("topics") as Topic[] | undefined;
  return Array.isArray(saved) && saved.length ? saved : DEFAULT_TOPICS.map((x) => ({ ...x }));
}

export class TopicError extends Error {}

/** Checks and stores an edited list. Ids stay stable when a category is renamed, so its
 *  streak and its emails keep their category; a new one gets an id from its name. */
export type TopicIn = Omit<Topic, "id" | "auto_send"> & { id?: string | undefined; auto_send?: boolean | undefined };

export function saveTopics(store: InboxStore, list: TopicIn[]): Topic[] {
  if (list.length > MAX_TOPICS) throw new TopicError(`at most ${MAX_TOPICS} categories`);
  const out: Topic[] = [];
  const seen = new Set<string>();
  for (const raw of list) {
    const label = raw.label.trim(), parent = raw.parent.trim(), description = raw.description.trim();
    if (!label || label.length > 40) throw new TopicError("each category needs a name of up to 40 characters");
    if (!parent || parent.length > 40) throw new TopicError(`"${label}" needs a parent setting type of up to 40 characters`);
    if (description.length > 300) throw new TopicError(`"${label}": keep the description under 300 characters`);
    let id = raw.id?.trim() || slug(label);
    if (seen.has(id) && !raw.id) {
      let n = 2;
      while (seen.has(`${id}_${n}`)) n++;
      id = `${id}_${n}`;
    }
    if (seen.has(id)) throw new TopicError(`two categories share the id ${id}`);
    seen.add(id);
    out.push({ id, label, parent, description: description || label, auto_send: raw.auto_send !== false });
  }
  if (!seen.has(OTHER)) {
    out.push({ ...DEFAULT_TOPICS.find((x) => x.id === OTHER)! });
  }
  store.setSetting("topics", out);
  return out;
}

export function topicOf(topics: readonly Topic[], id: string | null | undefined): Topic {
  return topics.find((x) => x.id === id) ?? topics.find((x) => x.id === OTHER) ?? DEFAULT_TOPICS[DEFAULT_TOPICS.length - 1]!;
}

/** The parent setting types, in the order their categories are listed. */
export function parentsOf(topics: readonly Topic[]): string[] {
  return [...new Set(topics.map((x) => x.parent))];
}

export function topicsForPrompt(topics: readonly Topic[]): string {
  return topics.map((x) => `- \`${x.id}\` (${x.parent} › ${x.label}): ${x.description}`).join("\n");
}
