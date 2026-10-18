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
