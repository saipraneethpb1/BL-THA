// Shared behaviour for the suite bar. Edit here, then run  python scripts/sync_suite.py
// Links between the three services work both locally (ports 8001-8003) and deployed
// (kb. / inbox. / scope. subdomains of one host, with the hub page on the bare host).
(function () {
  const ports = { kb: 8001, inbox: 8002, scope: 8003 };
  const { protocol, hostname } = location;
  const sub = hostname.match(/^(kb|inbox|scope)\.(.+)$/);
  const base = sub ? sub[2] : hostname;
  const deployed = !location.port && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(hostname);
  const urlFor = (app) => deployed ? `${protocol}//${app}.${base}/` : `${protocol}//${hostname}:${ports[app]}/`;
  const current = document.body.dataset.app;
  document.querySelectorAll("[data-suite-link]").forEach((a) => {
    a.href = urlFor(a.dataset.suiteLink);
    if (a.dataset.suiteLink === current) a.setAttribute("aria-current", "page");
  });
  const home = document.querySelector("[data-suite-home]");
  if (home) home.href = deployed ? `${protocol}//${base}/` : urlFor("kb");

  // Light / dark toggle. The choice is shared by all three services: a cookie on the
  // parent host when deployed (subdomains share it), localStorage as a fallback.
  const root = document.documentElement;
  const effective = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const themeBtn = document.getElementById("s-theme");
  if (themeBtn) themeBtn.onclick = () => {
    const next = effective() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch {}
    document.cookie = `theme=${next}; path=/; max-age=31536000; SameSite=Lax${deployed ? `; domain=${base}` : ""}`;
  };

  // Footer with the documents reviewers usually want next to the running app.
  const repo = "https://github.com/saipraneethpb1/BL-THA";
  if (current !== "hub") {
    const foot = Object.assign(document.createElement("footer"), { className: "s-foot" });
    foot.innerHTML = `<span>AI Tech Consultant</span><a href="${repo}" target="_blank" rel="noopener">Code</a><a href="${repo}/blob/main/EVALS.md" target="_blank" rel="noopener">Evals</a><a href="${repo}/blob/main/DESIGN.md" target="_blank" rel="noopener">Design</a>`;
    document.body.append(foot);
  }

  // Voice input. Uses the browser's own speech recognition (Chrome, Edge, Safari): free,
  // no API key. In Chrome and Edge the audio is processed by the browser vendor's service.
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const MIC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`;
  let active = null;
  function attachMic(field) {
    if (!Recognition || field.dataset.micReady) return;
    field.dataset.micReady = "1";
    const wrap = Object.assign(document.createElement("div"), { className: "mic-wrap" });
    if (field.style.flex) { wrap.style.flex = field.style.flex; wrap.style.minWidth = field.style.minWidth; field.style.flex = ""; }
    if (field.style.marginTop) { wrap.style.marginTop = field.style.marginTop; field.style.marginTop = "0"; }
    field.replaceWith(wrap); wrap.append(field);
    const btn = Object.assign(document.createElement("button"), { type: "button", className: "mic-btn", innerHTML: MIC, title: "Speak instead of typing" });
    btn.setAttribute("aria-label", "Speak instead of typing");
    wrap.append(btn);
    btn.onclick = () => {
      if (active) { const was = active.field; active.rec.stop(); if (was === field) return; }
      const rec = new Recognition();
      rec.lang = field.dataset.micLang || navigator.language || "en-US";
      rec.continuous = true; rec.interimResults = true;
      const start = field.value && !/\s$/.test(field.value) ? field.value + " " : field.value;
      let finalText = "";
      rec.onresult = (e) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalText += t; else interim += t;
        }
        field.value = start + finalText + interim;
        field.dispatchEvent(new Event("input", { bubbles: true }));
      };
      rec.onerror = (e) => {
        const msg = { "not-allowed": "Microphone access was blocked. Allow it in the address bar to use voice input.", "no-speech": "Didn't hear anything. Try again.", "network": "Voice input needs an internet connection." }[e.error];
        if (msg) suite.toast(msg, 4000);
      };
      rec.onend = () => { btn.classList.remove("on"); btn.title = "Speak instead of typing"; if (active?.rec === rec) active = null; };
      try { rec.start(); } catch { return; }
      active = { rec, field };
      btn.classList.add("on"); btn.title = "Listening… click to stop"; field.focus();
    };
  }
  const scanMics = () => document.querySelectorAll("[data-mic]").forEach(attachMic);
  scanMics();
  new MutationObserver(scanMics).observe(document.body, { childList: true, subtree: true });

  // Usage bar, fixed to the bottom of every page: Claude spend recorded by this site and
  // whether the API is currently answering. Deployed, the proxy exposes all three services'
  // figures on every host (/_usage/...); locally, only this service's own figure is shown.
  (function usageBar() {
    const bar = Object.assign(document.createElement("div"), { className: "usage-bar", role: "status" });
    bar.setAttribute("aria-live", "polite");
    document.body.append(bar); document.body.classList.add("has-usage-bar");
    const own = { kb: "/usage", inbox: "/api/usage", scope: "/api/usage" }[current];
    const get = (url) => fetch(url, { headers: { accept: "application/json" } }).then((r) => r.ok ? r.json() : null).catch(() => null);
    const money = (v) => `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`;
    const ago = (iso) => { if (!iso) return "never"; const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
    async function refresh() {
      let parts, budget = null, scope = "this site";
      if (deployed) {
        const [kb, inbox, sc, b] = await Promise.all(["kb", "inbox", "scope", "budget"].map((k) => get(`/_usage/${k}`)));
        parts = [kb, inbox, sc].filter(Boolean); budget = b?.budget_usd ?? null;
        // The Inbox Agent's figure already includes its Knowledge Brain lookups: count them once.
        if (kb && inbox?.includes_kb_lookups) {
          const dup = kb.by_caller?.["inbox-agent"] || { usd: 0, today_usd: 0 };
          parts = parts.map((p) => p === kb ? { ...kb, total_usd: kb.total_usd - dup.usd, today_usd: kb.today_usd - dup.today_usd } : p);
        }
      } else {
        const mine = own ? await get(own) : null;
        parts = mine ? [mine] : []; scope = "this service (site-wide totals when deployed)";
      }
      if (!parts.length) { bar.hidden = true; return; }
      bar.hidden = false;
      const total = parts.reduce((a, p) => a + Math.max(0, p.total_usd || 0), 0);
      const today = parts.reduce((a, p) => a + Math.max(0, p.today_usd || 0), 0);
      const lastOk = parts.map((p) => p.last_success_at).filter(Boolean).sort().at(-1) || null;
      const lastErr = parts.map((p) => p.last_error).filter(Boolean).sort((a, b) => a.at < b.at ? -1 : 1).at(-1) || null;
      const paused = lastErr && (!lastOk || lastErr.at > lastOk);
      const pct = budget ? Math.min(100, (total / budget) * 100) : null;
      const near = pct !== null && pct >= 80;
      bar.className = `usage-bar ${paused ? "paused" : near ? "near" : "ok"}`;
      bar.innerHTML = `<div class="ub-in">
        <span class="ub-state"><span class="ub-dot"></span>${paused ? `Claude API paused: ${esc(/credit balance/i.test(lastErr.message) ? "out of credit" : suite.friendlyError(lastErr.message))}` : "Claude API available"}</span>
        <span class="ub-sep"></span>
        <span>Today <b>${money(today)}</b></span>
        <span>Total <b>${money(total)}</b>${budget ? ` of <b>$${Number(budget).toFixed(2)}</b> budget` : ""}</span>
        ${pct !== null ? `<span class="ub-meter" title="${pct.toFixed(0)}% of the budget used"><span style="width:${pct}%"></span></span>` : ""}
        <span class="ub-meta" title="Spend recorded by ${esc(scope)} from each call's token counts at list prices. The account's credit balance is shown in the Anthropic Console.">Last AI call ${ago(lastOk)} · ${esc(scope)} ⓘ</span>
      </div>`;
    }
    refresh(); setInterval(refresh, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("suite:usage-changed", refresh);
  })();

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  window.suite = {
    toast(msg, ms = 2600) {
      const t = Object.assign(document.createElement("div"), { className: "toast", textContent: msg, role: "status" });
      document.body.append(t); setTimeout(() => t.remove(), ms);
    },
    lightbox(src, alt) {
      const box = Object.assign(document.createElement("div"), { className: "lightbox", title: "Click or press Esc to close" });
      const img = Object.assign(document.createElement("img"), { src, alt: alt || "" });
      box.append(img); document.body.append(box);
      const close = () => { box.remove(); document.removeEventListener("keydown", onKey); };
      const onKey = (e) => { if (e.key === "Escape") close(); };
      box.onclick = close; document.addEventListener("keydown", onKey);
    },
    // Turns a raw error from the Claude API into one sentence a reviewer can act on.
    friendlyError(text) {
      const raw = String(text ?? "");
      if (/credit balance is too low/i.test(raw)) return "The Claude API account is out of credit, so AI features are paused. Everything else still works.";
      if (/overloaded|529/i.test(raw)) return "The Claude API is busy right now. Please try again in a minute.";
      if (/rate.?limit|429/i.test(raw)) return "Too many requests to the Claude API right now. Please try again in a minute.";
      if (/authentication|invalid x-api-key|401/i.test(raw) && /claude|anthropic|api/i.test(raw)) return "The Claude API key was rejected, so AI features are paused.";
      const m = raw.match(/['"]message['"]\s*:\s*['"]([^'"]{4,300})['"]/);
      return m ? m[1] : raw;
    },
    // Small, safe Markdown subset for model answers: paragraphs, numbered and bullet lists, **bold**.
    md(text) {
      const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
      const out = []; let list = null;
      const flush = () => { if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; } };
      for (const raw of String(text ?? "").split("\n")) {
        const line = raw.trimEnd();
        const num = line.match(/^\s*\d+[.)]\s+(.*)$/), bul = line.match(/^\s*[-*•]\s+(.*)$/);
        if (num || bul) {
          const tag = num ? "ol" : "ul";
          if (!list || list.tag !== tag) { flush(); list = { tag, items: [] }; }
          list.items.push(`<li>${inline((num || bul)[1])}</li>`);
        } else { flush(); if (line.trim()) out.push(`<p>${inline(line)}</p>`); }
      }
      flush();
      return `<div class="md">${out.join("")}</div>`;
    },
  };
})();
