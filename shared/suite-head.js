// Runs in <head> before the page draws, so a chosen theme never flashes the wrong colours.
(function () {
  let t = "";
  try { t = (document.cookie.match(/(?:^|; )theme=(light|dark)/) || [])[1] || localStorage.getItem("theme") || ""; } catch {}
  if (t) document.documentElement.dataset.theme = t;
})();
