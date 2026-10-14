// Run rows, shared by the Studio page (runs in progress and recent ones) and the History page.
//
//   runs.split(jobs)                  -> { active, recent, older }
//   runs.render(el, list, opts)       draws rows into el, keeping open logs and their scroll position
//   runs.hooks.stop / retrain / changed   set by the page (stop and retrain need its dialogs)
//
// Runs in progress get the full card: steps, progress bar and time left. Finished runs are one
// compact line (name, result, View splat) that opens to show steps, advice, the other actions and
// the log. Warnings and errors stay visible on the compact line, because they say what to fix.
// Load after i18n.js.
(function () {
  const t = (k, p) => window.t(k, p);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const viewerUrl = (file, title) => "/viewer?" + new URLSearchParams({ file, title: title || file.split("/").pop() });
  const fileUrl = (p) => "/files/" + p.split("/").map(encodeURIComponent).join("/");
  const locale = () => window.i18n.getLang() === "pt" ? "pt-BR" : "en";
  const ago = (ts) => { if (!ts) return ""; const s = Date.now() / 1000 - ts;
    if (s < 60) return t("ago.now"); if (s < 3600) return t("ago.min", { n: Math.floor(s / 60) });
    if (s < 86400) return t("ago.h", { n: Math.floor(s / 3600) }); return new Date(ts * 1000).toLocaleDateString(locale()); };
  const dur = (a, b) => { if (!a) return ""; const s = Math.max(0, (b || Date.now() / 1000) - a);
    const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? `${h}h ${m % 60}m` : m ? `${m}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`; };
  const tr = (prefix, value) => window.i18n.has(prefix + value) ? t(prefix + value) : value;   // translate known labels

  const isActive = (j) => j.status === "running" || j.status === "queued";
  const endedAt = (j) => j.finished || j.started || j.created || 0;

  // The Studio keeps finished runs from the last day, at least the latest 3 and at most 5.
  // Everything else is one click away on the History page.
  const RECENT_MIN = 3, RECENT_MAX = 5, RECENT_HOURS = 24;
  function split(jobs, now = Date.now() / 1000) {
    const active = jobs.filter(isActive);
    const done = jobs.filter((j) => !isActive(j)).sort((a, b) => endedAt(b) - endedAt(a));
    let n = done.filter((j) => now - endedAt(j) < RECENT_HOURS * 3600).length;
    n = Math.min(RECENT_MAX, Math.max(RECENT_MIN, n));
    return { active, recent: done.slice(0, n), older: done.slice(n) };
  }

  function splatTitle(p) {
    const parts = p.split("/"), file = parts[parts.length - 1] || "";
    const cleaned = /_clean\.ply$/.test(file), isolated = /_object\.ply$/.test(file);
    const part = (file.match(/_part-(.+)\.ply$/) || [])[1];          // one object of several
    const q = file.replace(/_part-.+\.ply$|_clean\.ply$|_object\.ply$|\.ply$/g, "").replace(/_\d+$/, "");
    // English output stays exactly "<name> · preview"; other languages translate the quality word
    const base = parts[0] !== "workspace" ? file : `${parts[1]} · ${tr("quality.", q).toLowerCase()}`;
    if (parts[0] !== "workspace") return base;
    if (part) return `${base} · ${part.replace(/-/g, " ")}`;
    return cleaned ? `${base} · ${t("lib.cleaned")}` : isolated ? `${base} · ${t("lib.isolated")}` : base;
  }
  function runTitle(j) {
    if (j.type === "setup") return t("run.setup");
    if (j.type === "clean") return t("run.clean", { name: splatTitle((j.options || {}).splat || "") });
    if (j.type === "isolate") return t("run.isolate", { name: splatTitle((j.options || {}).splat || "") });
    const base = (j.video || j.run_dir || "").split("/").pop();
    return j.type === "retrain" ? t("run.retrain", { name: (j.run_dir || "").split("/")[1] || "" }) : base;
  }
  function stepper(j) {
    if (j.type === "setup" || j.type === "clean" || j.type === "isolate") return "";
    const st = j.stage ?? -1, first = j.type === "retrain" ? 3 : 0;
    return `<div class="steps">${[0, 1, 2, 3].map((i) => {
      const label = t("stage." + i);
      if (i < first) return `<span class="done" style="opacity:.45">${label}</span>`;
      let c = "";
      if (j.status === "done" || i < st) c = "done";
      else if (i === st) c = j.status === "running" ? "now" : ["failed", "interrupted", "cancelled"].includes(j.status) ? "fail" : "";
      return `<span class="${c}">${label}</span>`; }).join("")}</div>`;
  }
  // time left: measured for this step, from this computer's history for the whole run; never a guess
  function etaHTML(j) {
    const step = j.eta ? t("run.stepLeft", { t: j.eta })
      : j.eta_note === "finishing" ? t("run.finishing") : t("run.estimating");
    const total = j.eta_total ? t("run.totalLeft", { t: j.eta_total }) : j.eta_note === "first_run" ? t("run.firstRun") : "";
    const more = j.eta_total && j.eta_note === "more_attempts" ? ` title="${esc(t("run.moreAttempts"))}"` : "";
    return `${esc(step)}${total ? `<br><span${more}>${esc(total)}${more ? " *" : ""}</span>` : ""}`;
  }
  function attemptText(a) {                       // "2/3 - wider overlap ..." -> translated label
    const m = /^(\d+\/\d+) - (.+)$/.exec(a || ""); return m ? `${m[1]} - ${tr("attempt.", m[2])}` : (a || "");
  }
  const adviceText = (a) => (a.code && window.i18n.has("advice." + a.code) ? t("advice." + a.code, a.params || {}) : a.text);
  const adviceHTML = (items) => (items || []).map((a) => `<li class="${esc(a.level)}">${esc(adviceText(a))}</li>`).join("");

  // where the cameras were around the object, seen from above: dots by angle (higher cameras
  // nearer the middle), the largest empty arc in red when it's wide enough to leave a thin side.
  // With several objects, one ring per object (its own centre, the frames it's visible in).
  function ringSVG(c, size, label) {
    const R = 25, cx = 32, cy = 32, els = c.elevations || [];
    const lo = Math.min(...els), hi = Math.max(...els), span = Math.max(1, hi - lo);
    const pt = (az, r) => [cx + r * Math.cos(az * Math.PI / 180), cy - r * Math.sin(az * Math.PI / 180)];
    const dots = (c.azimuths || []).map((az, i) => { const [x, y] = pt(az, R - 9 * ((els[i] ?? lo) - lo) / span);
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.8" style="fill:var(--ink-2)"/>`; }).join("");
    const g = c.gap_open || c.gap || {};          // a side something stands in front of isn't a gap (#36)
    const arcPath = (from, to, style) => { const d = (to - from + 360) % 360 || 360, [x1, y1] = pt(from, R + 4), [x2, y2] = pt(to, R + 4);
      return `<path d="M${x1.toFixed(1)} ${y1.toFixed(1)}A${R + 4} ${R + 4} 0 ${d > 180 ? 1 : 0} 0 ${x2.toFixed(1)} ${y2.toFixed(1)}" style="${style}"/>`; };
    let arc = (c.blocked || []).map(([a, z]) => arcPath(a, z, "fill:none;stroke:var(--ink-3);stroke-width:3;stroke-dasharray:2 3")).join("");
    if ((g.degrees || 0) > 60) arc += arcPath(g.from, g.to, "fill:none;stroke:var(--bad);stroke-width:3;stroke-linecap:round");
    return `<svg viewBox="0 0 64 64" width="${size}" height="${size}" role="img" aria-label="${esc(label)}">
      <circle cx="${cx}" cy="${cy}" r="${R}" style="fill:none;stroke:var(--rule)"/><circle cx="${cx}" cy="${cy}" r="2.5" style="fill:var(--signal)"/>${arc}${dots}</svg>`;
  }
  const facts = (c) => t("coverage.facts", { n: c.cameras, gap: Math.round((c.gap_open || c.gap || {}).degrees || 0), h: Math.round((c.height || {}).spread || 0) });
  function coverageHTML(c, advice, withOk) {
    if (!c || !c.azimuths) return "";
    const tips = (advice || []).filter((a) => withOk || a.level !== "ok");
    const objs = c.objects || [];
    const rings = objs.length
      ? `<div class="cov-rings">${objs.map((o) => `<figure class="cov-ring" data-testid="coverage-object">${ringSVG(o, 56, o.name)}
          <figcaption><b>${esc(o.name)}</b><span class="meta">${esc(facts(o))}</span></figcaption></figure>`).join("")}</div>`
      : `<div class="meta">${esc(facts(c))}</div>`;
    return `<div class="coverage" data-testid="coverage">
      ${objs.length ? "" : ringSVG(c, 64, t("coverage.title"))}
      <div><div class="label">${esc(t("coverage.title"))}</div>${rings}
        ${tips.length ? `<ul class="advice">${adviceHTML(tips)}</ul>` : ""}</div></div>`;
  }

  const open = new Set();
  const hooks = { stop: null, retrain: null, changed: null };

  function parts(j) {
    const running = j.status === "running", qd = j.status === "queued";
    const o = j.options || {};
    const tags = [j.source === "cli" ? t("tag.cli") : null, o.quality ? t("quality." + o.quality).toLowerCase() : null,
                  o.detail && o.detail !== "auto" ? t("tag.detail", { d: t("detail." + o.detail) }) : null,
                  o.eval ? t("tag.eval") : null, o.remove_background ? t("tag.removeBg") : null, o.masks ? t("tag.masks") : null,
                  o.masks && o.split_objects ? t("tag.split") : null].filter(Boolean).map((x) => `<span class="tag">${esc(x)}</span>`).join("");
    const timing = running ? t("run.running", { t: dur(j.started) }) : qd ? t("run.queued", { ago: ago(j.created) })
      : !j.started ? t("run.stoppedEarly", { ago: ago(j.finished || j.created) })
      : j.finished ? t("run.took", { t: dur(j.started, j.finished), ago: ago(j.finished) }) : ago(j.created);
    const facts = [j.frames ? t("run.frames", { n: j.frames }) : null, j.registered ? t("run.registered", { r: j.registered }) : null].filter(Boolean).join(" · ");
    const best = j.clean_splat || j.splat, title = best ? splatTitle(best) : "";   // fog removed when there is a copy
    const canRetrain = !running && !qd && j.type !== "clean" && j.type !== "isolate" && j.run_dir && (j.status === "done" || (j.summary?.result || "").startsWith("OK"));
    const s = j.summary || {};
    const breaks = (j.track_breaks || []).map((b) => `${b.when ? b.when + " (" : ""}${t("run.frames", { n: b.count })}${b.when ? ")" : ""}`).join(", ")
                   || (s.track_breaks || []).join("; ");
    const kv = [[t("kv.folder"), j.run_dir], [t("kv.best"), s.best], ...(s.attempts || []).map((a, i) => [t("kv.attempt", { n: i + 1 }), a]),
                [t("kv.breaks"), breaks], [t("kv.brush"), s["brush settings"]], [t("kv.splats"), (j.splats || []).map((x) => x.split("/").pop()).join(", ")]]
      .filter(([, v]) => v).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(v)}</div>`).join("");
    const view = best ? `<a class="btn small primary" href="${viewerUrl(best, title)}">${esc(t("btn.viewSplat"))}</a>` : "";
    const extra = [
      j.splat && j.splat !== best ? `<a class="btn small" data-testid="view-original" href="${viewerUrl(j.splat, splatTitle(j.splat))}">${esc(t("btn.viewOriginal"))}</a>` : "",
      j.object_splat && j.object_splat !== best ? `<a class="btn small" data-testid="view-object" href="${viewerUrl(j.object_splat, splatTitle(j.object_splat))}">${esc(t("btn.viewObject"))}</a>` : ""].join("");
    // several objects: one splat each, right on the run's line
    const objs = (j.parts || []).length ? `<div class="run-parts" data-testid="run-parts"><span class="meta">${esc(t("run.parts"))}</span>${j.parts.map((x) =>
      `<a class="btn small" data-testid="view-part" href="${viewerUrl(x.splat, splatTitle(x.splat))}">${esc(x.name)}</a>`).join("")}</div>` : "";
    const guide = (j.advice || []).some((a) => a.level === "warn") ? `<div class="meta" style="margin-top:6px"><a class="link" href="/guide">${esc(t("btn.guide"))}</a></div>` : "";
    return { running, qd, tags, timing, facts, view, extra, canRetrain, kv, guide, objs,
             err: j.error && j.status !== "done" ? `<div class="err">${esc(j.error)}</div>` : "",
             // stopped itself before training (#34): say why, and offer to train with what there is
             stopped: j.status === "stopped" ? `<div class="run-stopped" data-testid="run-stopped"><b>${esc(t("run.stopped.title"))}</b>
               <span>${esc(t("run.stopped.help"))}</span>
               <div class="row"><button class="small primary" data-testid="train-anyway" onclick="runs.trainAnyway('${esc(j.id)}')">${esc(t("btn.trainAnyway"))}</button>
               <a class="btn small ghost" href="/guide">${esc(t("btn.guide"))}</a></div></div>` : "",
             noSplat: j.status === "done" && j.type === "video" && !j.splat ? `<div class="meta">${esc(t("run.noSplat"))}</div>` : "",
             thumb: `<div class="thumb" style="${j.thumb ? `background-image:url('${fileUrl(j.thumb)}')` : ""}"></div>`,
             head: `<div class="head"><span class="name" title="${esc(runTitle(j))}">${esc(runTitle(j))}</span>
               <span class="pill ${esc(j.status)}">${esc(tr("status.", j.status))}</span>` };
  }
  const more = (j, p) => `<div class="more">${p.kv ? `<div class="kv">${p.kv}</div>` : ""}<pre class="log" id="log-${esc(j.id)}">${esc(t("log.loading"))}</pre></div>`;

  // a run in progress: everything at once
  function activeHTML(j) {
    const p = parts(j);
    const pct = j.step_pct ?? null;
    const now = p.running ? `<div class="bar ${pct == null ? "indet" : ""}" style="margin-top:4px"><i style="width:${pct ?? 0}%"></i></div>
      <div class="now-line"><span>${esc(j.stage != null ? t("stage." + j.stage) : t("run.starting"))}${j.step_label ? " · " + esc(tr("step.", j.step_label)) : ""}${pct != null ? " · " + pct + "%" : ""}
      ${j.stage === 2 && j.attempt ? `<br>${esc(t("run.attempt", { a: attemptText(j.attempt) }))}` : ""}</span>
      <span class="eta" data-testid="eta">${etaHTML(j)}</span></div>` : "";
    return `<div class="run ${open.has(j.id) ? "open" : ""}" data-id="${esc(j.id)}" data-testid="run-active">
      ${p.thumb}
      <div style="min-width:0">
        ${p.head}${p.tags}</div>
        <div class="meta">${esc(p.timing)}${p.facts ? " · " + esc(p.facts) : ""}</div>
        ${stepper(j)}${now}${coverageHTML(j.coverage, j.coverage_advice, false)}${p.err}
      </div>
      <div class="actions">${p.view}<div class="row">
        <button class="small" onclick="runs.stop('${esc(j.id)}')">${esc(t("btn.stop"))}</button>
        ${j.source === "ui" ? `<button class="small ghost" onclick="runs.toggle('${esc(j.id)}')">${esc(t("btn.details"))}</button>` : ""}
      </div></div>
      ${more(j, p)}
    </div>`;
  }

  // a finished run: one line, opens for the rest
  function doneHTML(j, opts) {
    const p = parts(j), isOpen = open.has(j.id);
    // cleaning and background removal report their result as advice ("kept 5,699 of 18,000 splats"): keep it
    const warn = (j.advice || []).filter((a) => a.level === "warn" || j.type === "clean" || j.type === "isolate");
    const removable = opts.remove && j.source === "ui";
    return `<div class="run compact ${isOpen ? "open" : ""}" data-id="${esc(j.id)}" data-testid="run-done">
      ${p.thumb}
      <div style="min-width:0">
        ${p.head}</div>
        <div class="meta">${esc(p.timing)}${p.facts ? " · " + esc(p.facts) : ""}</div>
        ${p.objs}${p.err}${!isOpen && warn.length ? `<ul class="advice" data-testid="run-warnings">${adviceHTML(warn)}</ul>` : ""}
        ${p.stopped}${p.stopped && !isOpen ? coverageHTML(j.coverage, [], false) : ""}
      </div>
      <div class="actions"><div class="row">${p.view}
        <button class="small ghost" data-testid="run-details" aria-expanded="${isOpen}" onclick="runs.toggle('${esc(j.id)}')">${esc(t(isOpen ? "btn.less" : "btn.details"))}</button></div></div>
      <div class="more">
        ${p.tags ? `<div class="head">${p.tags}</div>` : ""}
        ${stepper(j)}${coverageHTML(j.coverage, (j.coverage_advice || []).filter((a) => a.level !== "warn"), true)}
        ${(j.advice || []).length ? `<ul class="advice" data-testid="run-advice">${adviceHTML(j.advice)}</ul>` : ""}${p.guide}${p.noSplat}
        ${p.extra || p.canRetrain || removable ? `<div class="more-actions">${p.extra}
          ${p.canRetrain ? `<button class="small" onclick="runs.retrain('${esc(j.run_dir)}')">${esc(t("btn.trainAgain"))}</button>` : ""}
          ${removable ? `<button class="small ghost" data-testid="run-remove" title="${esc(t("btn.remove.title"))}" onclick="runs.remove('${esc(j.id)}')">${esc(t("btn.remove"))}</button>` : ""}</div>` : ""}
        ${p.kv || j.source === "ui" ? `<div class="kv-log">${p.kv ? `<div class="kv">${p.kv}</div>` : ""}${j.source === "ui" ? `<pre class="log" id="log-${esc(j.id)}">${esc(t("log.loading"))}</pre>` : ""}</div>` : ""}
      </div>
    </div>`;
  }

  const html = (j, opts = {}) => isActive(j) ? activeHTML(j) : doneHTML(j, opts);

  const byId = new Map();             // the rows on screen, for buttons that need a run's settings
  function render(el, list, opts = {}) {
    list.forEach((j) => byId.set(j.id, j));
    const logs = {}; el.querySelectorAll("pre.log").forEach((p) => logs[p.id] = [p.textContent, p.scrollTop]);
    el.innerHTML = list.map((j) => html(j, opts)).join("");
    for (const [id, [txt, s]] of Object.entries(logs)) { const p = document.getElementById(id); if (p) { p.textContent = txt; p.scrollTop = s; } }
  }

  async function loadLog(id) {
    const el = document.getElementById("log-" + id); if (!el) return;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    const d = await fetch(`/api/jobs/${encodeURIComponent(id)}/log`).then((r) => r.json()).catch(() => ({ log: "" }));
    el.textContent = d.log || t("log.empty");
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
  function toggle(id) {
    open.has(id) ? open.delete(id) : open.add(id);
    hooks.changed && hooks.changed();
    if (open.has(id)) loadLog(id);
  }
  function stop(id) { hooks.stop && hooks.stop(id); }
  // a run stopped for coverage: train from its camera poses with the settings it was started with
  async function trainAnyway(id) {
    const j = byId.get(id); if (!j) return;
    const o = j.options || {};
    const body = { type: "retrain", run_dir: j.run_dir, quality: o.quality, detail: o.detail, eval: o.eval,
                   remove_background: o.remove_background, masks: o.masks, split_objects: o.split_objects, brush_args: o.brush_args };
    const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then((x) => x.json()).catch(() => ({ error: t("err.network") }));
    if (r.error) { (window.toast || alert)(r.code && window.i18n.has("err." + r.code) ? t("err." + r.code) : r.error); return; }
    hooks.changed && hooks.changed(true);
  }
  function retrain(dir) { hooks.retrain ? hooks.retrain(dir) : (location.href = "/?" + new URLSearchParams({ retrain: dir })); }
  // drops the record and its log from the list; splats and run folders stay on disk
  async function remove(id) {
    if (!confirm(t("confirm.removeRun"))) return;
    await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
    open.delete(id);
    hooks.changed && hooks.changed(true);
  }

  window.runs = { split, render, html, isActive, endedAt, open, hooks, toggle, stop, retrain, trainAnyway, remove, loadLog,
                  esc, viewerUrl, fileUrl, ago, dur, tr, splatTitle, runTitle, adviceHTML, RECENT_MIN, RECENT_MAX };
})();
