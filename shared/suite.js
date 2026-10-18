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
    // Small, safe Markdown subset for model answers: paragraphs, numbered and bullet lists, **bold**.
    md(text) {
      const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
