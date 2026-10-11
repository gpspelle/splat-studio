// Site header and footer, shared by the Studio, History and recording guide pages.
//
//   <body data-page="studio">        which nav item is current (studio | history | guide)
//   window.site.mount({ tools })     tools: true adds the "Tools ready" button (Studio only)
//
// Load after i18n.js and theme.js, and call before i18n.apply() so the labels get translated.
(function () {
  const REPO = "https://github.com/gpspelle/splat-studio";

  const LOGO = `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" fill="var(--signal)"/>
    <ellipse cx="13" cy="18.5" rx="8.5" ry="5" transform="rotate(-32 13 18.5)" fill="var(--on-signal)"/>
    <ellipse cx="21.5" cy="10.5" rx="5" ry="2.8" transform="rotate(28 21.5 10.5)" fill="var(--on-signal)" opacity=".6"/>
    <circle cx="23" cy="22.5" r="2.6" fill="var(--on-signal)" opacity=".35"/>
  </svg>`;

  const GITHUB = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>`;

  const ext = (href, key, text) => `<a href="${href}" target="_blank" rel="noopener" data-i18n="${key}">${text}</a>`;

  function header(page, tools) {
    const nav = [["studio", "/", "nav.studio", "Studio"], ["library", "/#library", "nav.library", "Library"],
                 ["history", "/history", "nav.history", "History"],
                 ["guide", "/guide", "nav.guide", "Recording guide"]]
      .map(([id, href, key, text]) => `<a href="${href}" data-testid="nav-${id}" data-i18n="${key}"${id === page ? ' aria-current="page"' : ""}>${text}</a>`)
      .join("") + ext(REPO + "#readme", "nav.docs", "Docs");
    return `<div class="site-head-in">
      <a class="wordmark" href="/" aria-label="Splat Studio">${LOGO}<span>Splat Studio</span></a>
      <nav class="site-nav" data-i18n-aria-label="nav.label" aria-label="Main">${nav}</nav>
      <div class="site-tools">
        <span data-lang-switcher></span>
        <span data-theme-switcher></span>
        ${tools ? '<button id="setupPill" class="status-pill" data-testid="setup-pill" data-i18n="head.checking">Checking tools…</button>' : ""}
        <a class="btn ghost icon" href="${REPO}" target="_blank" rel="noopener" data-testid="github-link" data-i18n-title="foot.github" title="Source code on GitHub">${GITHUB}<span class="sr-only">GitHub</span></a>
      </div>
    </div>`;
  }

  function footer() {
    return `<div class="site-foot-in">
      <div class="foot-brand">
        <a class="wordmark" href="/">${LOGO}<span>Splat Studio</span></a>
        <p data-i18n="foot.about">Make 3D Gaussian splats from phone videos with open-source tools.</p>
      </div>
      <div class="foot-col"><b data-i18n="foot.app">App</b>
        <a href="/" data-i18n="nav.studio">Studio</a>
        <a href="/#library" data-i18n="nav.library">Library</a>
        <a href="/history" data-i18n="nav.history">History</a>
        <a href="/guide" data-i18n="nav.guide">Recording guide</a></div>
      <div class="foot-col"><b data-i18n="foot.help">Help</b>
        ${ext(REPO + "/blob/main/docs/how-it-works.md", "foot.how", "How it works")}
        ${ext(REPO + "#troubleshooting", "foot.troubleshooting", "Troubleshooting")}
        ${ext(REPO + "/blob/main/CHANGELOG.md", "foot.changelog", "Changelog")}</div>
      <div class="foot-col"><b data-i18n="foot.project">Project</b>
        ${ext(REPO, "foot.source", "Source code")}
        ${ext(REPO + "/issues/new", "foot.bug", "Report a bug")}
        ${ext(REPO + "/issues", "foot.contact", "Contact")}</div>
      <div class="foot-base">
        <span><span id="siteVersion"></span><a href="${REPO}/blob/main/LICENSE" target="_blank" rel="noopener" data-i18n="foot.license">MIT License</a></span>
        <span data-i18n-html="foot.credits.html">Built on <a href="https://colmap.github.io/">COLMAP</a>, <a href="https://github.com/ArthurBrussee/brush">Brush</a> and <a href="https://sparkjs.dev">Spark</a>.</span>
      </div>
    </div>`;
  }

  function mount(opts) {
    opts = opts || {};
    const page = document.body.dataset.page || "";
    const head = document.createElement("header");
    head.className = "site-head";
    head.innerHTML = header(page, opts.tools);
    document.body.prepend(head);
    const foot = document.createElement("footer");
    foot.className = "site-foot";
    foot.innerHTML = footer();
    document.body.append(foot);
    setVersion(opts.version);
  }

  function setVersion(v) {
    const el = document.getElementById("siteVersion");
    if (el && v) el.textContent = "v" + v + " · ";
  }

  window.site = { mount, setVersion, REPO };
})();
