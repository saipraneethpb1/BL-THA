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

  // Demo accounts. Signing in is optional: guests share one workspace; a demo account keeps
  // its own inbox and scope projects across visits. Deployed only (the proxy routes /_auth).
  const acctSlot = document.getElementById("s-account");
  async function accountWidget() {
    if (!acctSlot || !deployed) return;
    let me;
    try { me = await fetch("/_auth/me").then((r) => r.ok ? r.json() : null); } catch { return; }
    if (!me) return;
    const initial = (me.account || "g").replace(/^demo/, "")[0].toUpperCase();
    acctSlot.innerHTML = me.account
      ? `<button class="s-acct" type="button" title="Signed in as ${me.account}: your own inbox and projects. Click to sign out."><span class="who">${initial}</span>${me.account}</button>`
      : `<button class="s-acct guest" type="button" title="You're in the shared guest workspace. Sign in with a demo account to keep your own work."><span class="who">G</span>Guest · Sign in</button>`;
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

  // Footer with the documents reviewers usually want next to the running app.
  const repo = "https://github.com/saipraneethpb1/BL-THA";
  if (current !== "hub") {
    const foot = Object.assign(document.createElement("footer"), { className: "s-foot" });
    foot.innerHTML = `<span>AI Tech Consultant</span><a href="${repo}" target="_blank" rel="noopener">Code</a><a href="${repo}/blob/main/EVALS.md" target="_blank" rel="noopener">Evals</a><a href="${repo}/blob/main/DESIGN.md" target="_blank" rel="noopener">Design</a>`;
    document.body.append(foot);
  }

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
      const capped = parts.filter((p) => p.over_budget).map((p) => ({ kb: "Knowledge Brain", inbox: "Inbox Agent", scope: "Scope Builder" })[p.service]);
      const pct = budget ? Math.min(100, (total / budget) * 100) : null;
      const near = pct !== null && pct >= 80;
      bar.className = `usage-bar ${paused || capped.length ? "paused" : near ? "near" : "ok"}`;
      bar.innerHTML = `<div class="ub-in">
        <span class="ub-state"><span class="ub-dot"></span>${capped.length ? `AI budget for this demo used up (${esc(capped.join(", "))}): browsing still works` : paused ? `Claude API paused: ${esc(/credit balance/i.test(lastErr.message) ? "out of credit" : suite.friendlyError(lastErr.message))}` : "Claude API available"}</span>
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
