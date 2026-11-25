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
    new: ["● Нове відео", "new"],
    work: ["✂ Розбираю", ""],
    done: ["✓ Розібрано", "done"],
  };

  /** gemini-4-hydrogen → Gemini 4 Hydrogen (same as backend/gemini_models.pretty_name). */
  function modelName(m) {
    const r = /^gemini-([0-9.]+)-(.+)$/.exec(m || "");
    if (!r) return m || "";
    const rest = r[2].replace("flash-lite", "Flash-Lite").split("-").join(" ")
      .split(" ").map((w) => (w[0] === w[0].toUpperCase() ? w : w[0].toUpperCase() + w.slice(1))).join(" ");
    return `Gemini ${r[1]} ${rest}`;
  }

  /** «🧪 Gemini 4 Hydrogen»: which model chose the clips of an experimental search. */
  function modelOf(p) {
    if (p.search_mode !== "experimental" || !p.ai_model) return "";
    return ` · <span class="vc-model" title="Ця модель ШІ обрала кліпи">🧪 ${escapeHtml(modelName(p.ai_model))}</span>`;
  }

  function metaOf(p, st) {
    return metaBase(p, st) + (st === "busy" || st === "failed" || st === "empty" ? "" : modelOf(p));
  }

  function metaBase(p, st) {
    const total = p.clips_count || 0;
    const reviewed = p.reviewed_count || 0;
    const kept = p.kept_count || 0;
    switch (st) {
      case "busy": return "Обробляється…";
      case "failed": return errorSummaryHtml(p.last_error);
      case "empty": return "Клік, щоб знайти кліпи";
      case "done": return `<b>${kept} в роботі</b> з ${total} · усі розібрано`;
      default: {
        // «Вірусні» first; weaker clips wait in «Запас» (older projects without the counts fall back to the total)
        if (p.new_count == null) return `<b>${total} кліпів</b>${kept ? ` · ${kept} в роботі` : ""}`;
        const parts = [`<b>${p.new_count} ${p.new_count === 1 ? "вірусний" : "вірусних"}</b>`];
        if (kept) parts.push(`${kept} в роботі`);
        if (p.reserve_count) parts.push(`${p.reserve_count} у запасі`);
        return parts.join(" · ");
      }
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
      <div class="history-item-left"${st === "failed" ? "" : ` role="button" tabindex="0" title="Відкрити"`}>
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
      ${st === "failed" ? `<div class="q-actions vc-acts">
        <button type="button" class="btn btn-primary btn-sm" data-act="retry">↻ Ще раз</button>
        <button type="button" class="btn btn-secondary btn-sm" data-act="delete">Видалити</button>
      </div>` : ""}
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
    // a failed card has its own «Ще раз / Видалити» buttons, so it is not a button itself (no nested controls)
    if (st !== "failed") left.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    el.querySelector(".vc-dots").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(el); });
    el.querySelector(".err-more")?.addEventListener("click", (e) => e.stopPropagation()); // «Детальніше» only opens the text
    el.querySelector(".vc-acts")?.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      if (b.dataset.act === "delete") { deleteProject(p.id, e); return; }
      b.disabled = true;
      try { await Queue.addProject(p.id); } catch (err) { showToast(err.message, "error"); b.disabled = false; }
    });
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
    if (p.clips_count) {
      const mode = await askSearchAgain(p, p.reviewed_count || 0);
      if (!mode) return;
      if (mode === "compare") { Compare.open(p, { onClose: () => load() }); return; }
      try { await Queue.addProject(p.id, { search_mode: mode }); } catch (err) { showToast(err.message, "error"); }
      return;
    }
    if (!(await askConfirm(`Поставити «${p.title}» в чергу на пошук моментів?`, { ok: "↻ Так, ще раз" }))) return;
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
    // the keyboard was inside a menu (Esc or a pick): it goes back to that card's «⋯»
    const inside = document.activeElement?.closest?.(".vc-menu");
    const dots = inside?.parentElement.querySelector(".vc-dots");
    document.querySelectorAll(".vc-menu").forEach((m) => { m.hidden = true; });
    if (dots && dots.isConnected) dots.focus({ preventScroll: true });
    document.querySelectorAll(".vc-dots").forEach((b) => b.setAttribute("aria-expanded", "false"));
    document.querySelectorAll(".menu-open").forEach((c) => c.classList.remove("menu-open"));
  }

  /** First run (no videos, empty queue): the big hero with three steps. Otherwise the compact block. */
  function sync() {
    const queueCount = $("queueList") ? $("queueList").children.length : 0;
    const any = projects.length + queueCount > 0;
    // «Твої відео 3 · у черзі 2»: every card in the grid is counted once
    const hide = queuedIds();
    const own = projects.filter((p) => !hide.has(p.id)).length;
    $("historyCount").textContent = queueCount ? `${own} · у черзі ${queueCount}` : String(own);
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
    const s = $("heroSearch");
    const search = s && s.value === "experimental" ? "🧪 експериментальний пошук" : "📼 класичний пошук";
    const chips = [filter, search, `кліпи ${defaults.min}–${defaults.max} с`, count, `мова: ${lang}`];
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
    ["heroFilter", "heroClips", "heroLang", "heroSearch"].forEach((id) => $(id)?.addEventListener("change", renderOpts));
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
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeMenus(); return; }
      const m = e.target.closest?.(".vc-menu");
      if (!m || !["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const items = [...m.querySelectorAll("button:not(:disabled)")];
      const i = items.indexOf(e.target);
      const n = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[n]?.focus();
    });
    // Tab out of an open menu closes it (it is not left hanging over the next card)
    document.addEventListener("focusout", (e) => {
      const m = e.target.closest?.(".vc-menu");
      if (!m) return;
      setTimeout(() => { // the new focus is known only after this event (none when Tab leaves the page)
        const card = m.parentElement;
        const a = document.activeElement;
        if (m.hidden || m.contains(a) || a === card.querySelector(".vc-dots") || m.matches(":hover")) return; // a mouse click inside it
        m.hidden = true;
        card.classList.remove("menu-open");
        card.querySelector(".vc-dots")?.setAttribute("aria-expanded", "false");
      });
    });
    load();
  }

  document.addEventListener("DOMContentLoaded", init);
  const find = (id) => projects.find((p) => p.id === id) || null;
  return { load, render, sync, setDefaults, renderOpts, toggleMenu, closeMenus, find };
})();
