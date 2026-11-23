/**
 * home.js — the home page: the «Додай відео» block, the search line («Шукаю: …»), the video cards with
 * their state badge and «⋯» menu, search and sort, the first-run look (no videos yet) and drag-and-drop
 * of files anywhere in the window. Queue cards are drawn by queue.js into the same grid.
 * Depends on app.js globals: escapeHtml, showToast, openProject, deleteProject, currentProject.
 */
const Home = (() => {
  const $ = (id) => document.getElementById(id);
  let projects = [];
  let loaded = false;
  let sort = "new";
  try { sort = localStorage.getItem("studio-home-sort") === "old" ? "old" : "new"; } catch (_) {}
  const defaults = { lang: "українська", min: 15, max: 60 };

  // ── state of a video ────────────────────────────────────────────────────────
  const BUSY = ["analyzing", "transcribing", "downloading"];

  function stateOf(p) {
    const total = p.clips_count || 0;
    const reviewed = p.reviewed_count || 0;
    if (BUSY.includes(p.status) && !total) return "busy";
    if (!total) return p.status === "failed" ? "failed" : "empty";
    if (!reviewed) return "new";
    return reviewed >= total ? "done" : "work";
  }

  const BADGE = {
    busy: ["⏳ Обробляється", ""],
    failed: ["Не вдалося", "bad"],
    empty: ["Без кліпів", ""],
    new: ["● Нове", "new"],
    work: ["✂ В роботі", ""],
    done: ["✓ Готово", "done"],
  };

  function metaOf(p, st) {
    const total = p.clips_count || 0;
    const reviewed = p.reviewed_count || 0;
    const kept = p.kept_count || 0;
    switch (st) {
      case "busy": return "Обробляється…";
      case "failed": return "Клік, щоб спробувати ще раз";
      case "empty": return "Клік, щоб знайти кліпи";
      case "new": return `<b>${total} кліпів</b> · ще не дивився`;
      case "done": return `<b>${kept} в роботі</b> з ${total} · усі переглянуто`;
      default: return `<b>${kept} в роботі</b> з ${total} · переглянуто ${reviewed}`;
    }
  }

  // ── loading & drawing ───────────────────────────────────────────────────────
  async function load() {
    try {
      const res = await fetch("/api/projects");
      if (!res.ok) return;
      projects = (await res.json()).projects || [];
      loaded = true;
      render();
    } catch (err) {
      console.warn("Could not load recent projects:", err);
    }
  }

  /** Videos the queue is working on are shown by queue.js, so their project card stays hidden. */
  function queuedIds() {
    return typeof Queue !== "undefined" && Queue.activeProjectIds ? Queue.activeProjectIds() : new Set();
  }

  function render() {
    const list = $("historyList");
    if (!list) return;
    const q = ($("homeSearch")?.value || "").trim().toLowerCase();
    const hide = queuedIds();
    let shown = projects.filter((p) => !hide.has(p.id));
    if (q) shown = shown.filter((p) => (p.title || "").toLowerCase().includes(q));
    shown.sort((a, b) => {
      const d = String(b.created_at || "").localeCompare(String(a.created_at || ""));
      return sort === "old" ? -d : d;
    });
    list.innerHTML = "";
    shown.forEach((p) => list.appendChild(card(p)));
    $("homeNothing").hidden = !(q && !shown.length);
    $("historyCount").textContent = projects.length;
    document.querySelectorAll("#homeSort button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.s === sort)));
    sync();
  }

  function card(p) {
    const st = stateOf(p);
    const total = p.clips_count || 0;
    const pct = total ? Math.round(((p.reviewed_count || 0) / total) * 100) : 0;
    const [badge, badgeCls] = BADGE[st];
    const id = encodeURIComponent(p.id);
    const el = document.createElement("div");
    el.className = `history-item video-card st-${st}`;
    if (typeof currentProject !== "undefined" && currentProject && currentProject.id === p.id) el.classList.add("active");
    el.innerHTML = `
      <div class="history-item-left" role="button" tabindex="0" title="Відкрити">
        <div class="history-thumb-wrap">
          <img src="/api/projects/${id}/thumbnail" class="history-thumb-img" alt="" loading="lazy">
          <span class="history-fallback-icon" style="display:none">🎬</span>
          <span class="video-card-dur">${escapeHtml(p.duration_formatted || "00:00")}</span>
        </div>
        <div class="history-details">
          <span class="history-item-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>
          <span class="video-card-meta">${metaOf(p, st)}</span>
          ${total ? `<span class="video-card-prog"><i style="width:${pct}%"></i></span>` : ""}
        </div>
      </div>
      <span class="vc-badge ${badgeCls}">${badge}</span>
      <button type="button" class="vc-dots" aria-haspopup="true" aria-expanded="false" title="Дії з відео" aria-label="Дії з відео">⋯</button>
      <div class="vc-menu" role="menu" hidden>
        ${total ? `<button type="button" role="menuitem" data-act="open">Відкрити</button>` : ""}
        <button type="button" role="menuitem" data-act="again">↻ ${total ? "Шукати кліпи заново" : "Знайти кліпи"}</button>
        <button type="button" role="menuitem" data-act="delete" class="danger">🗑 Видалити</button>
      </div>`;
    const img = el.querySelector(".history-thumb-img");
    img.addEventListener("error", () => { img.style.display = "none"; img.nextElementSibling.style.display = "flex"; });
    const left = el.querySelector(".history-item-left");
    const go = () => openCard(p, st);
    left.addEventListener("click", go);
    left.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    el.querySelector(".vc-dots").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(el); });
    el.querySelector(".vc-menu").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      closeMenus();
      if (b.dataset.act === "open") openCard(p, st);
      else if (b.dataset.act === "again") again(p);
      else if (b.dataset.act === "delete") deleteProject(p.id, e);
    });
    return el;
  }

  function openCard(p, st) {
    if (st === "busy") { showToast("Це відео ще обробляється.", "info"); return; }
    if (st === "failed" || st === "empty") { again(p); return; }
    openProject(p.id);
  }

  async function again(p) {
    const marked = p.reviewed_count || 0;
    const ask = p.clips_count
      ? `Шукати кліпи в «${p.title}» заново?${marked ? ` Позначки «Беру» / «Не беру» (${marked}) зникнуть.` : ""}`
      : `Поставити «${p.title}» в чергу на пошук моментів?`;
    if (!confirm(ask)) return;
    try { await Queue.addProject(p.id); } catch (err) { showToast(err.message, "error"); }
  }

  // ── «⋯» menus (video cards and queue cards share them) ──────────────────────
  function toggleMenu(cardEl) {
    const menu = cardEl.querySelector(".vc-menu");
    const on = menu.hidden;
    closeMenus();
    menu.hidden = !on;
    cardEl.querySelector(".vc-dots").setAttribute("aria-expanded", String(on));
    cardEl.classList.toggle("menu-open", on);
    if (on) menu.querySelector("button")?.focus();
  }
  function closeMenus() {
    document.querySelectorAll(".vc-menu").forEach((m) => { m.hidden = true; });
    document.querySelectorAll(".vc-dots").forEach((b) => b.setAttribute("aria-expanded", "false"));
    document.querySelectorAll(".menu-open").forEach((c) => c.classList.remove("menu-open"));
  }

  /** First run (no videos, empty queue): the big hero with three steps. Otherwise the compact block. */
  function sync() {
    const queueCount = $("queueList") ? $("queueList").children.length : 0;
    const any = projects.length + queueCount > 0;
    document.body.classList.toggle("home-empty", loaded && !any);
    $("historyCard").hidden = !any;
    const tools = projects.length > 6;
    document.querySelector(".home-search").hidden = !tools;
    $("homeSort").hidden = !tools;
  }

  // ── «Шукаю: …» line ────────────────────────────────────────────────────────
  function setDefaults(d) {
    Object.assign(defaults, d);
    renderOpts();
  }

  function renderOpts() {
    const f = $("heroFilter"), c = $("heroClips");
    const filter = f.options[f.selectedIndex]?.text.toLowerCase() || "";
    const count = c.value === "0" ? "кількість: авто" : `кліпів: ${c.value}`;
    const l = $("heroLang");
    const lang = (l && l.options[l.selectedIndex]?.text.toLowerCase()) || defaults.lang;
    const chips = [filter, `кліпи ${defaults.min}–${defaults.max} с`, count, `мова: ${lang}`];
    $("heroHint").innerHTML = chips.map((t) => `<span class="home-chip">${escapeHtml(t)}</span>`).join("");
  }

  // ── drag files anywhere ─────────────────────────────────────────────────────
  function initDrop() {
    const ov = $("dropOverlay");
    let depth = 0;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    const editorOpen = () => { const ed = $("editor"); return ed && !ed.hidden; };
    window.addEventListener("dragenter", (e) => {
      if (!hasFiles(e) || editorOpen()) return;
      e.preventDefault();
      depth++;
      ov.hidden = false;
    });
    window.addEventListener("dragover", (e) => { if (hasFiles(e) && !editorOpen()) e.preventDefault(); });
    window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) ov.hidden = true; });
    window.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      ov.hidden = true;
      if (!editorOpen() && e.dataTransfer.files.length) Queue.addFiles(e.dataTransfer.files);
    });
    $("fileInput").addEventListener("change", (e) => {
      if (e.target.files && e.target.files.length) Queue.addFiles(e.target.files);
    });
  }

  function init() {
    initDrop();
    renderOpts();
    ["heroFilter", "heroClips", "heroLang"].forEach((id) => $(id).addEventListener("change", renderOpts));
    $("homeOptsEdit").addEventListener("click", () => {
      const panel = $("homeOptsPanel");
      panel.hidden = !panel.hidden;
      $("homeOptsEdit").setAttribute("aria-expanded", String(!panel.hidden));
      $("homeOptsEdit").textContent = panel.hidden ? "Змінити" : "Готово";
    });
    $("homeSearch").addEventListener("input", render);
    document.querySelectorAll("#homeSort button").forEach((b) => b.addEventListener("click", () => {
      sort = b.dataset.s;
      try { localStorage.setItem("studio-home-sort", sort); } catch (_) {}
      render();
    }));
    document.addEventListener("click", (e) => { if (!e.target.closest(".vc-menu, .vc-dots")) closeMenus(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
    load();
  }

  document.addEventListener("DOMContentLoaded", init);
  return { load, render, sync, setDefaults, renderOpts, toggleMenu, closeMenus };
})();
