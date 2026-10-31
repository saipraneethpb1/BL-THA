// Shared behaviour for the suite bar. Edit here, then run  python scripts/sync_suite.py
// Links between the three services work both locally (ports 8001-8003) and deployed
// (kb. / inbox. / scope. subdomains of one host, with the hub page on the bare host).
// Locally the hub page is served on port 8000:  python -m http.server 8000 -d deploy/site
(function () {
  const ports = { hub: 8000, kb: 8001, inbox: 8002, scope: 8003 };
  const { protocol, hostname } = location;
  const sub = hostname.match(/^(kb|inbox|scope)\.(.+)$/);
  const base = sub ? sub[2] : hostname;
  const deployed = !location.port && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(hostname);
  const urlFor = (app) => app === "hub" && deployed ? `${protocol}//${base}/`
    : deployed ? `${protocol}//${app}.${base}/` : `${protocol}//${hostname}:${ports[app]}/`;
  const current = document.body.dataset.app;
  document.querySelectorAll("[data-suite-link]").forEach((a) => {
    a.href = urlFor(a.dataset.suiteLink);
    if (a.dataset.suiteLink === current) a.setAttribute("aria-current", "page");
  });
  const home = document.querySelector("[data-suite-home]");
  if (home) home.href = urlFor("hub");

  // The page's own status badge (#s-right) belongs next to the page title, not in the rail.
  const statusSlot = document.querySelector("[data-suite-status]") || document.querySelector(".app-head") || document.querySelector(".gm-top");
  if (statusSlot) statusSlot.append(document.getElementById("s-right"));

  // Live dots on the rail: is each agent's service answering? (no-cors: reachable is enough)
  async function ping() {
    await Promise.all(["kb", "inbox", "scope"].map(async (app) => {
      const dot = document.querySelector(`[data-live="${app}"]`); if (!dot) return;
      const ok = await fetch(`${urlFor(app)}health`, { mode: "no-cors", cache: "no-store" }).then(() => true, () => false);
      dot.className = `s-live ${ok ? "up" : "down"}`;
      const link = dot.parentElement;
      link.dataset.state = ok ? "online" : "offline";
      link.title = `${link.title.replace(/ · (online|offline)$/, "")} · ${ok ? "online" : "offline"}`;
    }));
    window.dispatchEvent(new Event("suite:live"));
  }
  ping(); setInterval(ping, 60000);

  // Light / dark toggle. The choice is shared by all three services: a cookie on the
  // parent host when deployed (subdomains share it), localStorage as a fallback.
  const root = document.documentElement;
  const effective = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const themeBtn = document.getElementById("s-theme");
  if (themeBtn) themeBtn.onclick = () => {
    const next = effective() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch {}
    window.dispatchEvent(new Event("suite:theme"));
    document.cookie = `theme=${next}; path=/; max-age=31536000; SameSite=Lax${deployed ? `; domain=${base}` : ""}`;
  };

  // Demo accounts. Signing in is optional: guests share one workspace; a demo account keeps
  // its own inbox and scope projects across visits. Deployed only (the proxy routes /_auth).
  const acctSlot = document.getElementById("s-account");
  async function accountWidget() {
    if (!acctSlot || !deployed) return;
    let me;
    try { me = await fetch("/_auth/me").then((r) => r.ok ? r.json() : null); } catch { return; }
    if (!me) return;
    // A Google login (Connect Gmail) is named by a hash; show its address instead.
    const who = me.account?.startsWith("g_") ? decodeURIComponent((document.cookie.match(/(?:^|; )bl_who=([^;]*)/) || [])[1] || "Gmail account") : me.account;
    const initial = (who || "g").replace(/^demo/, "")[0].toUpperCase();
    const safe = String(who).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    acctSlot.innerHTML = me.account
      ? `<button class="s-acct" type="button" title="Signed in as ${safe}: your own inbox and projects. Click to sign out."><span class="who">${initial}</span><span class="nm">${safe}</span></button>`
      : `<button class="s-acct guest" type="button" title="You're in the shared guest workspace. Sign in with a demo account to keep your own work."><span class="who">G</span><span class="nm">Guest · Sign in</span></button>`;
    acctSlot.querySelector("button").onclick = me.account ? signOut : () => signInDialog(me.demo_accounts);
  }
  async function signOut() {
    if (!confirm("Sign out? Your work stays saved in this demo account for next time.")) return;
    await fetch("/_auth/logout", { method: "POST" }).catch(() => null);
    location.reload();
  }
  function signInDialog(accounts) {
    const wrap = Object.assign(document.createElement("div"), { className: "s-modal" });
    wrap.innerHTML = `<div class="s-modal-box" role="dialog" aria-modal="true" aria-labelledby="s-si-title">
      <h2 id="s-si-title">Sign in with a demo account</h2>
      <p class="meta" style="margin:0">Your inbox, drafts, learning and scope projects are saved to the account, so you can come back and pick up where you left off. Without signing in you share the guest workspace.</p>
      <div class="s-accts">${accounts.map((a) => `<button type="button" data-a="${a.account}" data-p="${a.password}">${a.account}<small>${a.password}</small></button>`).join("")}</div>
      <form><input name="account" placeholder="Account, e.g. demo1" autocomplete="username" required>
        <input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
        <div class="row" style="margin-top:4px"><button class="btn">Sign in</button><button class="btn ghost" type="button" data-close>Cancel</button><span class="meta" data-msg></span></div></form></div>`;
    document.body.append(wrap);
    const form = wrap.querySelector("form");
    const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    wrap.onclick = (e) => { if (e.target === wrap) close(); };
    wrap.querySelector("[data-close]").onclick = close;
    wrap.querySelectorAll("[data-a]").forEach((b) => b.onclick = () => {
      form.account.value = b.dataset.a; form.password.value = b.dataset.p;
      wrap.querySelectorAll("[data-a]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      form.querySelector(".btn").focus();
    });
    form.onsubmit = async (e) => {
      e.preventDefault();
      const msg = wrap.querySelector("[data-msg]");
      try {
        const r = await fetch("/_auth/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ account: form.account.value, password: form.password.value }) });
        if (!r.ok) { msg.textContent = (await r.json().catch(() => ({}))).detail || "Sign-in failed."; return; }
        location.reload();
      } catch { msg.textContent = "Couldn't reach the server."; }
    };
    form.account.focus();
  }
  accountWidget();

  // Popovers that open beside the rail button that triggered them.
  let openPop = null;
  function popover(btn, html) {
    if (openPop) { const same = openPop.btn === btn; openPop.close(); if (same) return null; }
    const pop = Object.assign(document.createElement("div"), { className: "s-pop", innerHTML: html });
    document.body.append(pop);
    const r = btn.getBoundingClientRect();
    if (innerWidth <= 760) { pop.style.top = `${r.bottom + 8}px`; pop.style.right = "8px"; }
    else { pop.style.left = `${r.right + 10}px`; pop.style.bottom = `${Math.max(8, innerHeight - r.bottom)}px`; }
    btn.setAttribute("aria-expanded", "true");
    const onDoc = (e) => { if (!pop.contains(e.target) && !btn.contains(e.target)) close(); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const close = () => { pop.remove(); btn.setAttribute("aria-expanded", "false"); document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); openPop = null; };
    setTimeout(() => { document.addEventListener("mousedown", onDoc); document.addEventListener("keydown", onKey); });
    openPop = { btn, close, pop };
    return pop;
  }

  // The documents reviewers usually want next to the running app.
  const repo = "https://github.com/saipraneethpb1/BL-THA";
  const DOCS = [["Code on GitHub", repo, "repo"], ["Evals", `${repo}/blob/main/EVALS.md`, "EVALS.md"], ["Design notes", `${repo}/blob/main/DESIGN.md`, "DESIGN.md"], ["How AI was used", `${repo}/blob/main/AI_LOG.md`, "AI_LOG.md"]];
  const docsBtn = document.getElementById("s-docs");
  if (docsBtn) docsBtn.onclick = () => popover(docsBtn, `<h4>Read more</h4>${DOCS.map(([l, u, f]) => `<a href="${u}" target="_blank" rel="noopener">${l}<small>${f} ↗</small></a>`).join("")}`);

  // Voice input. First choice: the browser's own live dictation (words appear as you speak).
  // It sends audio to Google or Microsoft and fails behind many VPNs, in Brave and similar
  // browsers, and doesn't exist in Firefox. Then we record and transcribe on our own server
  // (Whisper, /_speech), which works in every browser. The switch is remembered per visit.
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const speechUrl = deployed ? "/_speech" : null; // locally the proxy (and so the fallback) isn't there
  const canRecord = Boolean(speechUrl && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  const preferServer = () => { try { return sessionStorage.getItem("speech-mode") === "server"; } catch { return false; } };
  const rememberServer = () => { try { sessionStorage.setItem("speech-mode", "server"); } catch {} };
  const MIC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`;
  let active = null; // { stop(), field }

  function insert(field, text, base) {
    field.value = (base ?? (field.value && !/\s$/.test(field.value) ? field.value + " " : field.value)) + text;
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function setState(btn, state) {
    btn.classList.toggle("on", state === "listening");
    btn.classList.toggle("busy", state === "busy");
    btn.innerHTML = state === "busy" ? `<span class="spinner"></span>` : MIC;
    btn.title = { listening: "Listening… click to stop", busy: "Turning your speech into text…" }[state] || "Speak instead of typing";
  }

  function startBrowser(field, btn) {
    const rec = new Recognition();
    rec.lang = field.dataset.micLang || navigator.language || "en-US";
    rec.continuous = true; rec.interimResults = true;
    const base = field.value && !/\s$/.test(field.value) ? field.value + " " : field.value;
    let finalText = "", failedOver = false;
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t; else interim += t;
      }
      insert(field, finalText + interim, base);
    };
    rec.onerror = (e) => {
      if (["network", "service-not-allowed", "language-not-supported", "audio-capture"].includes(e.error) && canRecord) {
        failedOver = true; rememberServer();
        suite.toast("Your browser's dictation isn't available here; using the server's speech recognition instead.", 4500);
      } else {
        const msg = { "not-allowed": "Microphone access was blocked. Allow it in the address bar to use voice input.", "no-speech": "Didn't hear anything. Try again.", "network": "Your browser's dictation service can't be reached (a VPN or browser setting may block it)." }[e.error];
        if (msg) suite.toast(msg, 4500);
      }
    };
    rec.onend = () => {
      setState(btn, "idle"); if (active?.rec === rec) active = null;
      if (failedOver) startServer(field, btn);
    };
    try { rec.start(); } catch { return; }
    active = { rec, field, stop: () => rec.stop() };
    setState(btn, "listening"); field.focus();
  }

  async function startServer(field, btn) {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { suite.toast("Microphone access was blocked. Allow it in the address bar to use voice input.", 4500); return; }
    const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    const limit = setTimeout(() => recorder.state === "recording" && recorder.stop(), 60000);
    recorder.onstop = async () => {
      clearTimeout(limit); stream.getTracks().forEach((t) => t.stop());
      if (active?.recorder === recorder) active = null;
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      if (blob.size < 1000) { setState(btn, "idle"); suite.toast("Didn't hear anything. Try again."); return; }
      setState(btn, "busy");
      try {
        const r = await fetch(speechUrl, { method: "POST", headers: { "content-type": blob.type }, body: blob });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || `Error ${r.status}`);
        if (body.text) insert(field, body.text); else suite.toast("Didn't catch any words. Try again, a little closer to the mic.");
      } catch (err) { suite.toast(`Couldn't turn the recording into text: ${err.message}`, 5000); }
      finally { setState(btn, "idle"); field.focus(); }
    };
    recorder.start();
    active = { recorder, field, stop: () => recorder.state === "recording" && recorder.stop() };
    setState(btn, "listening"); btn.title = "Recording… click to stop (up to 1 minute)";
  }

  function attachMic(field) {
    if ((!Recognition && !canRecord) || field.dataset.micReady) return;
    field.dataset.micReady = "1";
    const wrap = Object.assign(document.createElement("div"), { className: "mic-wrap" });
    if (field.style.flex) { wrap.style.flex = field.style.flex; wrap.style.minWidth = field.style.minWidth; field.style.flex = ""; }
    if (field.style.marginTop) { wrap.style.marginTop = field.style.marginTop; field.style.marginTop = "0"; }
    field.replaceWith(wrap); wrap.append(field);
    const btn = Object.assign(document.createElement("button"), { type: "button", className: "mic-btn" });
    btn.setAttribute("aria-label", "Speak instead of typing");
    setState(btn, "idle");
    wrap.append(btn);
    btn.onclick = () => {
      if (btn.classList.contains("busy")) return;
      if (active) { const was = active.field; active.stop(); if (was === field) return; }
      if (Recognition && !preferServer()) startBrowser(field, btn); else startServer(field, btn);
    };
  }
  const scanMics = () => document.querySelectorAll("[data-mic]").forEach(attachMic);
  scanMics();
  new MutationObserver(scanMics).observe(document.body, { childList: true, subtree: true });

  // AI usage: Claude spend recorded by this site and whether Claude (on Amazon Bedrock) is
  // currently answering.
  // A ring in the rail (click for details); a bar along the bottom only when the API is paused
  // or the budget is used up. Deployed, the proxy exposes all three services' figures on every
  // host (/_usage/...); locally, only this service's own figure is shown.
  (function usage() {
    const btn = document.getElementById("s-usage");
    const bar = Object.assign(document.createElement("div"), { className: "usage-bar", role: "status", hidden: true });
    bar.setAttribute("aria-live", "polite");
    document.body.append(bar);
    const own = { kb: "/usage", inbox: "/api/usage", scope: "/api/usage" }[current];
    const get = (url) => fetch(url, { headers: { accept: "application/json" } }).then((r) => r.ok ? r.json() : null).catch(() => null);
    const money = (v) => `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`;
    const ago = (iso) => { if (!iso) return "never"; const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
    let last = null;
    // Why the last Claude call failed, in a few words (Amazon Bedrock and Anthropic API errors).
    const pauseReason = (msg) => {
      const m = String(msg ?? "");
      if (/credit balance|payment|billing|insufficient.*credit|credits? (are )?(exhausted|expired)/i.test(m)) return "out of AWS credits";
      if (/security token|token (has )?expired|expiredtoken|unrecognizedclient|signature|invalid.*(bearer|api.?key)|401/i.test(m)) return "AWS credentials rejected or expired";
      if (/don't have access to the model|not authorized|accessdenied|403/i.test(m)) return "no access to this model in Bedrock";
      if (/inference profile|on-demand throughput/i.test(m)) return "model id needs an inference profile";
      if (/throttl|too many requests|rate.?limit|429/i.test(m)) return "rate-limited by Bedrock";
      return "last call failed";
    };
    const details = () => {
      const u = last; if (!u) return "";
      return `<h4>AI usage</h4><div class="pad">
        <div class="kv"><span>${esc(u.headline)}</span></div>
        ${u.pct !== null ? `<div class="bar"><span style="width:${u.pct}%;background:var(${u.paused ? "--err" : u.near ? "--warn" : "--ok"})"></span></div>` : ""}
        <div class="kv"><span class="muted">Today</span><b>${money(u.today)}</b></div>
        <div class="kv"><span class="muted">Total</span><b>${money(u.total)}${u.budget ? ` <span class="muted">of $${Number(u.budget).toFixed(2)}</span>` : ""}</b></div>
        <div class="kv"><span class="muted">Last AI call</span><b>${ago(u.lastOk)}</b></div>
        <p class="meta" style="margin:8px 0 0">Spend recorded by ${esc(u.scope)} from each call's token counts at Anthropic's list prices. Claude runs on Amazon Bedrock: actual charges and remaining credits are in AWS Billing, and can be a little higher (regional profiles such as au. cost about 10% more).</p></div>`;
    };
    if (btn) btn.onclick = () => popover(btn, details());
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
      if (!parts.length) { if (btn) btn.hidden = true; bar.hidden = true; document.body.classList.remove("has-usage-bar"); return; }
      const total = parts.reduce((a, p) => a + Math.max(0, p.total_usd || 0), 0);
      const today = parts.reduce((a, p) => a + Math.max(0, p.today_usd || 0), 0);
      const lastOk = parts.map((p) => p.last_success_at).filter(Boolean).sort().at(-1) || null;
      const lastErr = parts.map((p) => p.last_error).filter(Boolean).sort((a, b) => a.at < b.at ? -1 : 1).at(-1) || null;
      const apiPaused = lastErr && (!lastOk || lastErr.at > lastOk);
      const capped = parts.filter((p) => p.over_budget).map((p) => ({ kb: "Knowledge Brain", inbox: "Inbox Agent", scope: "Scope Builder" })[p.service]);
      const pct = budget ? Math.min(100, (total / budget) * 100) : null;
      const paused = Boolean(apiPaused || capped.length), near = pct !== null && pct >= 80;
      const headline = capped.length ? `AI budget for this demo used up (${capped.join(", ")}): browsing still works`
        : apiPaused ? `Claude paused: ${pauseReason(lastErr.message)}. Everything else still works.`
        : "Claude answering (Amazon Bedrock)";
      last = { today, total, budget, pct, lastOk, paused, near, headline, scope };
      if (btn) {
        btn.hidden = false;
        btn.className = `s-icon s-usage ${paused ? "paused" : near ? "near" : ""}`;
        btn.style.setProperty("--p", pct !== null ? pct.toFixed(1) : "0");
        btn.title = `${headline} · today ${money(today)} · total ${money(total)}`;
        if (openPop?.btn === btn) openPop.pop.innerHTML = details();
      }
      bar.hidden = !paused;
      document.body.classList.toggle("has-usage-bar", paused);
      bar.innerHTML = paused ? `<div class="ub-in"><span class="ub-state"><span class="ub-dot"></span>${esc(headline)}</span></div>` : "";
    }
    refresh(); setInterval(refresh, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("suite:usage-changed", refresh);
  })();

  // Command palette (Ctrl/Cmd + K): jump between agents, run page commands, or hand a
  // question straight to the Knowledge Brain. Pages add their own with suite.commands([...]).
  const ICON = {
    go: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    ask: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><path d="M12 7.5 13.6 10.4 16.5 12 13.6 13.6 12 16.5 10.4 13.6 7.5 12 10.4 10.4z"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    theme: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    view: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    cmd: '<path d="m9 18 6-12"/>',
  };
  const svg = (k) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k] || ICON.cmd}</svg>`;
  const pageCommands = [];
  const AGENTS = [["hub", "Home", "start a task"], ["kb", "Knowledge Brain", "Task 1"], ["inbox", "Inbox Agent", "Task 2"], ["scope", "Scope Builder", "Task 3"]];
  function allCommands(q) {
    const list = [
      ...pageCommands,
      ...AGENTS.filter(([a]) => a !== current).map(([a, l, h]) => ({ group: "Go to", label: l, hint: h, icon: "go", run: () => { location.href = urlFor(a); } })),
      { group: "Preferences", label: "Switch light / dark mode", icon: "theme", keywords: "theme dark light", run: () => themeBtn?.click() },
      ...DOCS.map(([l, u, f]) => ({ group: "Read more", label: l, hint: f, icon: "doc", run: () => window.open(u, "_blank", "noopener") })),
    ];
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = list.filter((c) => words.every((w) => `${c.group} ${c.label} ${c.hint || ""} ${c.keywords || ""}`.toLowerCase().includes(w)));
    if (q.trim().length < 3) return hits;
    const ask = { group: "Ask", label: `Ask the Knowledge Brain: “${q.trim()}”`, icon: "ask",
                  run: () => current === "kb" && window.suite.ask ? window.suite.ask(q.trim()) : (location.href = `${urlFor("kb")}?q=${encodeURIComponent(q.trim())}`) };
    return hits.length ? [...hits, ask] : [ask];
  }
  function palette(initial = "") {
    if (document.querySelector(".s-cmdk")) return;
    openPop?.close();
    const wrap = Object.assign(document.createElement("div"), { className: "s-cmdk" });
    wrap.innerHTML = `<div class="s-cmdk-box" role="dialog" aria-modal="true" aria-label="Commands">
      <div class="s-cmdk-in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input placeholder="Jump to an agent, run a command, or ask a question…" aria-label="Command" autocomplete="off" spellcheck="false"></div>
      <div class="s-cmdk-list" role="listbox"></div>
      <div class="s-cmdk-foot"><span><span class="kbd">↑</span> <span class="kbd">↓</span> move</span><span><span class="kbd">Enter</span> run</span><span><span class="kbd">Esc</span> close</span></div></div>`;
    document.body.append(wrap);
    const input = wrap.querySelector("input"), list = wrap.querySelector(".s-cmdk-list");
    let items = [], sel = 0;
    const mark = () => list.querySelectorAll("[data-i]").forEach((x) => x.setAttribute("aria-selected", String(Number(x.dataset.i) === sel)));
    const draw = () => {
      items = allCommands(input.value);
      sel = Math.min(sel, Math.max(0, items.length - 1));
      let grp = null;
      list.innerHTML = items.length ? items.map((c, i) => {
        const head = c.group !== grp ? `<div class="s-cmdk-grp">${esc(grp = c.group)}</div>` : "";
        return `${head}<div class="s-cmdk-item" role="option" data-i="${i}" aria-selected="${i === sel}"><span class="ic">${svg(c.icon)}</span><span class="l">${esc(c.label)}</span>${c.hint ? `<small>${esc(c.hint)}</small>` : ""}</div>`;
      }).join("") : `<div class="empty" style="padding:22px">No matching commands.</div>`;
    };
    const move = (d) => { if (!items.length) return; sel = (sel + d + items.length) % items.length; mark(); list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); };
    const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey, true); };
    const run = (i) => { const c = items[i]; if (!c) return; close(); c.run(); };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); run(sel); }
    };
    document.addEventListener("keydown", onKey, true);
    input.addEventListener("input", () => { sel = 0; draw(); });
    list.addEventListener("mousemove", (e) => { const it = e.target.closest("[data-i]"); if (it && Number(it.dataset.i) !== sel) { sel = Number(it.dataset.i); mark(); } });
    list.addEventListener("click", (e) => { const it = e.target.closest("[data-i]"); if (it) run(Number(it.dataset.i)); });
    wrap.addEventListener("mousedown", (e) => { if (e.target === wrap) close(); });
    input.value = initial; draw(); input.focus();
  }
  document.getElementById("s-cmd")?.addEventListener("click", () => palette());
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); palette(); }
  });

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  window.suite = {
    url: urlFor,
    palette,
    /** Add page commands to the palette: [{ group, label, hint?, keywords?, icon?, run }] */
    commands(list) { pageCommands.push(...list); },
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
    // Turns a raw error from Claude (Amazon Bedrock or the Anthropic API) into one sentence a
    // reviewer can act on.
    friendlyError(text) {
      const raw = String(text ?? "");
      if (/credit balance is too low|payment|billing|insufficient.*credit|credits? (are )?(exhausted|expired)/i.test(raw)) return "The AWS account's credits for Claude are used up, so AI features are paused. Everything else still works.";
      if (/overloaded|529|serviceunavailable|model ?not ?ready|503/i.test(raw)) return "Claude on Amazon Bedrock is busy right now. Please try again in a minute.";
      if (/throttl|too many requests|rate.?limit|429/i.test(raw)) return "Too many requests to Claude on Amazon Bedrock right now. Please try again in a minute.";
      if (/security token|token (has )?expired|expiredtoken|unrecognizedclient|signature|authentication|invalid x-api-key|401/i.test(raw)) return "The AWS credentials for Claude on Amazon Bedrock were rejected or have expired, so AI features are paused.";
      if (/don't have access to the model|not authorized|accessdenied|403/i.test(raw)) return "This AWS account has no access to the Claude model it asked for. Enable it under Bedrock model access.";
      if (/inference profile|on-demand throughput/i.test(raw)) return "Bedrock needs an inference profile for this model: check CLAUDE_BEDROCK_PROFILE or the model id.";
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
