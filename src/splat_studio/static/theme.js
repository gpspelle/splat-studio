// Light / dark theme for the Studio page. "system" (default) follows the computer's setting.
// Stored per browser in localStorage "theme"; applied before first paint (load this in <head>).
(function () {
  const KEY = "theme", root = document.documentElement;
  const get = () => { try { return localStorage.getItem(KEY) || "system"; } catch { return "system"; } };
  function set(v) {
    try { localStorage.setItem(KEY, v); } catch {}
    if (v === "system") root.removeAttribute("data-theme"); else root.dataset.theme = v;
  }
  set(get());
  function mount() {
    document.querySelectorAll("[data-theme-switcher]").forEach((host) => {
      const t = (k, fb) => (window.t && window.i18n && window.i18n.has(k) ? window.t(k) : fb);
      const sel = document.createElement("select");
      sel.className = "theme-select";
      sel.dataset.testid = "theme-select";
      const render = () => {
        sel.setAttribute("aria-label", t("theme.label", "Theme"));
        sel.innerHTML = ["system", "light", "dark"].map((v) =>
          `<option value="${v}">${t("theme." + v, v[0].toUpperCase() + v.slice(1))}</option>`).join("");
        sel.value = get();
      };
      render();
      sel.onchange = () => set(sel.value);
      addEventListener("langchange", render);
      host.replaceWith(sel);
    });
  }
  window.theme = { get, set, mount };
})();
