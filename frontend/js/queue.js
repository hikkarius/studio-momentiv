/**
 * queue.js — «Черга» on the home page: several links / files are downloaded and analysed one by one.
 * The server does the work (backend/queue_runner.py); this file only adds items and shows progress.
 * Depends on app.js globals: showToast, escapeHtml, loadRecentProjects, loadProjectResults.
 */
const Queue = (() => {
  const POLL_MS = 1500;
  const IDLE_POLL_MS = 15000;
  let items = [];
  let uploads = [];          // files still being sent to the server: {key, name, pct, error}
  let timer = null;
  let autoOpen = new Set();  // queue item ids to open as soon as they are done
  let lastStatus = {};       // id → status, to notice when something finishes
  let disk = null;           // free space: {level: ok|low|full, free, free_text, drive, video_hours}

  const $ = (id) => document.getElementById(id);

  // ── adding ────────────────────────────────────────────────────────────────

  function parseLinks(text) {
    // A pasted list may lose its line breaks, so split before every http(s)://
    return text
      .split(/\s+|(?=https?:\/\/)/)
      .map((s) => s.trim())
      .filter((s) => /^https?:\/\/\S+\.\S+/.test(s));
  }

  function options() {
    return {
      clips_count: parseInt($("heroClips").value, 10) || 0,
      filter_mode: $("heroFilter").value,
      search_mode: ($("heroSearch") && $("heroSearch").value) || "",
      stories: ($("heroStories") && $("heroStories").value) || "",
      language: ($("heroLang") && $("heroLang").value) || "",
    };
  }

  async function add(body, { open = false } = {}) {
    const res = await fetch("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...options(), ...body }),
    });
    if (!res.ok) throw new Error(await errorText(res));
    const data = await res.json();
    if (open) data.added.forEach((i) => autoOpen.add(i.id));
    await refresh();
    return data.added;
  }

  async function submitHero() {
    const input = $("urlInput");
    const raw = input.value.trim();
    if (!raw) {
      showToast("Встав посилання або обери файли.", "info");
      input.focus();
      return;
    }
    const links = parseLinks(raw);
    if (!links.length) {
      showToast("Не бачу тут посилання. Воно має починатися з https://", "error");
      return;
    }
    const btn = $("urlDownloadBtn");
    btn.disabled = true;
    try {
      const added = await add({ urls: links }, { open: links.length === 1 });
      input.value = "";
      autoGrow();
      showToast(added.length === 1 ? "Додано в чергу." : `Додано в чергу: ${added.length}.`);
      $("historyCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  /** Re-analyse one video that is already in the studio (from «Шукати заново»). */
  async function addProject(projectId, opts = {}) {
    const added = await add({ project_ids: [projectId], ...opts }, { open: true });
    if (!added.length) showToast("Це відео вже в черзі.", "info");
    else showToast("Поставлено в чергу. Кліпи відкриються, щойно будуть готові.");
  }

  async function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const single = files.length === 1;
    let room = await freeRoom();
    for (const file of files) {
      const up = { key: Math.random().toString(36).slice(2), name: file.name, pct: 0, error: "" };
      uploads.push(up);
      if (room != null && file.size > room) { // do not send a file the disk cannot hold
        up.error = `Не вистачає місця на диску. Треба близько ${gb(file.size + reserve)}, а вільно лише ${gb(room + reserve)}.`;
        up.noSpace = true;
        render();
        continue;
      }
      if (room != null) room -= file.size;
      render();
      try {
        const data = await uploadOne(file, up);
        const proj = data.project;
        const hasClips = data.reused && proj.status === "ready";
        if (hasClips) {
          showToast(`«${proj.title}» вже оброблене, кліпи готові.`, "info");
          if (single) await loadProjectResults(proj.id);
        } else {
          await add({ project_ids: [proj.id] }, { open: single });
        }
        uploads = uploads.filter((u) => u !== up);
      } catch (err) {
        up.error = err.message;
        up.noSpace = /Не вистачає місця/.test(err.message);
      }
      render();
    }
    $("fileInput").value = "";
    loadRecentProjects();
  }

  let reserve = 0;
  /** Bytes that can still be taken by new files (free space minus the reserve), or null if unknown. */
  async function freeRoom() {
    try {
      const res = await fetch("/api/disk");
      if (!res.ok) return null;
      const d = await res.json();
      if (d.free == null) return null;
      reserve = d.reserve || 0;
      return Math.max(0, d.free - reserve);
    } catch (_) {
      return null;
    }
  }
  const gb = (n) => n < 1024 ** 3 ? `${Math.max(1, Math.round(n / 1024 ** 2))} МБ` : `${(n / 1024 ** 3).toFixed(1).replace(".", ",")} ГБ`;

  function uploadOne(file, up) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append("file", file);
      xhr.open("POST", "/api/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          up.pct = e.loaded / e.total;
          render();
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); } catch (e) { reject(e); }
        } else {
          let msg = xhr.responseText;
          try { msg = JSON.parse(msg).detail || msg; } catch (_) {}
          reject(new Error(msg || `Помилка ${xhr.status}`));
        }
      };
      xhr.onerror = () => reject(new Error("Студія не відповідає. Вона запущена?"));
      xhr.send(form);
    });
  }

  // ── actions on items ──────────────────────────────────────────────────────

  async function act(url, method = "POST", body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      showToast(await errorText(res), "error");
      return false;
    }
    await refresh();
    return true;
  }

  const remove = (id) => act(`/api/queue/${encodeURIComponent(id)}`, "DELETE");
  const retry = (id) => act(`/api/queue/${encodeURIComponent(id)}/retry`);
  const resume = (id) => act(`/api/queue/${encodeURIComponent(id)}/resume`);
  const move = (id, direction) => act(`/api/queue/${encodeURIComponent(id)}/move`, "POST", { direction });
  const clearFinished = () => act("/api/queue/clear-finished");

  function dismissUpload(key) {
    uploads = uploads.filter((u) => u.key !== key);
    render();
  }

  // ── polling & rendering ───────────────────────────────────────────────────

  async function refresh() {
    clearTimeout(timer);
    let active = false;
    try {
      const res = await fetch("/api/queue");
      if (res.ok) {
        const data = await res.json();
        items = data.items || [];
        active = data.active;
        disk = data.disk || null;
        renderDisk();
        pause = {
          until: data.paused_until || 0, daily: !!data.limit_daily, awake: !!data.keep_awake,
          overload: data.limit_reason === "overload",
        };
        onChanges();
        render();
      }
    } catch (_) {
      /* server restarting: try again later */
    }
    timer = setTimeout(refresh, active || uploads.length ? POLL_MS : IDLE_POLL_MS);
  }

  function onChanges() {
    let refreshVideos = false;
    for (const it of items) {
      const before = lastStatus[it.id];
      if (before && before !== it.status) {
        refreshVideos = true;
        if (it.status === "done") {
          if (autoOpen.has(it.id) && it.project_id && !editorOpen()) {
            autoOpen.delete(it.id);
            showToast(`Готово: «${it.title}». Відкриваю кліпи…`);
            loadProjectResults(it.project_id);
          } else {
            showToast(`Готово: «${it.title}»`);
          }
        } else if (it.status === "failed") {
          showToast(`Не вдалося: «${it.title}»`, "error");
        } else if (it.status === "limit" && before !== "analyzing") {
          /* a video transcribed during the pause: no toast */
        } else if (it.status === "limit") {
          showToast(`${pause.overload ? "Google перевантажений" : "Ліміт Gemini"}: «${it.title}» продовжиться ${whenText(it.resume_at)}`);
        }
      }
      if (!before && it.status !== "waiting") refreshVideos = true;
      lastStatus[it.id] = it.status;
    }
    if (refreshVideos && typeof loadRecentProjects === "function") loadRecentProjects();
  }

  function editorOpen() {
    const ed = document.getElementById("editor");
    return ed && !ed.hidden;
  }

  const STATUS = {
    waiting: "У черзі",
    downloading: "Обробляється",
    analyzing: "Обробляється",
    failed: "Не вдалося",
    limit: "⏸ Чекає на Gemini",
  };
  const SHOWN = ["limit", "waiting", "downloading", "analyzing", "failed"];
  let pause = { until: 0, daily: false, awake: false, overload: false };

  // ── Gemini limit pause ────────────────────────────────────────────────────

  function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
  }
  /** «о 10:00», «завтра о 10:00», «за 2 хв» */
  function whenText(ts) {
    const left = ts - Date.now() / 1000;
    if (left < 90 * 60 && !pause.daily) return `за ${Math.max(1, Math.round(left / 60))} хв`;
    const d = new Date(ts * 1000), today = new Date();
    const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
    const sameDay = (a, b) => a.toDateString() === b.toDateString();
    if (sameDay(d, today)) return `о ${clock(ts)}`;
    if (sameDay(d, tomorrow)) return `завтра о ${clock(ts)}`;
    return `${d.toLocaleDateString("uk-UA", { day: "numeric", month: "long" })} о ${clock(ts)}`;
  }
  function countdown(ts) {
    const left = Math.max(0, Math.round(ts - Date.now() / 1000));
    const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), sec = left % 60;
    const two = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`;
  }
  let tick = null;
  function startTick() {
    if (tick) return;
    tick = setInterval(() => {
      const els = document.querySelectorAll("[data-count-to]");
      if (!els.length) { clearInterval(tick); tick = null; return; }
      els.forEach((el) => { el.textContent = countdown(parseFloat(el.dataset.countTo)); });
    }, 1000);
  }

  function renderBanner(shown) {
    const box = $("queueLimit");
    if (!box) return;
    const paused = shown.filter((i) => i.status === "limit");
    if (!paused.length || !pause.until) { box.hidden = true; box.innerHTML = ""; return; }
    const more = shown.some((i) => i.status === "waiting");
    const when = `<b>${escapeHtml(whenText(pause.until))}</b>`;
    const head = pause.overload ? "Сервери Google перевантажені"
      : pause.daily ? "Безкоштовний ліміт Gemini на сьогодні закінчився" : "Gemini попросив перепочити";
    const why = pause.overload
      ? `Це не через ключ: Google зараз не встигає відповідати всім. Готові частини збережено, студія сама доробить решту ${when}.`
      : pause.daily
        ? `Нічого не втрачено. Студія сама продовжить ${when}, коли Google оновить ліміт.`
        : `Забагато запитів за хвилину (так буває з безкоштовним ключем). Студія сама продовжить ${when}.`;
    box.innerHTML = `
      <span class="ql-ic" aria-hidden="true">⏸</span>
      <div class="ql-text">
        <b class="ql-head">${head}</b>
        <span>${why}${more ? "<br>А поки розпізнає мову в наступних відео, щоб потім усе пішло швидше." : ""}</span>
        ${pause.awake ? `<small>☕ Ноутбук не засне, доки черга не закінчиться. Екран може згаснути.</small>` : ""}
      </div>
      ${pause.overload ? "" : `<button type="button" class="btn btn-secondary btn-sm" onclick="Settings.open('ai')">Замінити ключ</button>`}`;
    box.hidden = false;
  }
  let lastIds = "";

  /** Projects that have a queue card right now (their ordinary card is hidden on the home page). */
  function activeProjectIds() {
    return new Set(items.filter((i) => SHOWN.includes(i.status) && i.project_id).map((i) => i.project_id));
  }

  /** Stage name without its own percent and engine details: the card shows one overall percent. */
  function stageText(msg) {
    const m = String(msg || "").replace(/\s*\([^)]*\)/g, "").replace(/:?\s*\d+(?:[.,]\d+)?\s*%\s*$/, "").trim();
    if (/^Транскрипці/i.test(m)) return "Розпізнаю мову";
    return m || "Обробляю…";
  }

  /** Steps under the progress bar: download → speech recognition → search for moments. */
  function stepOf(it) {
    if (it.status === "downloading") return 0;
    return (it.progress || 0) < 0.6 ? 1 : 2;
  }
  function steps(cur) {
    const done = ["Скачано", "Розпізнано", "Знайдено"], now = ["Скачування", "Розпізнавання", "Пошук"];
    return now.map((t, k) =>
      `<span class="${k < cur ? "ok" : k === cur ? "now" : ""}">${k < cur ? `✓ ${done[k]}` : k === cur ? `● ${t}` : `○ ${t}`}</span>`).join("");
  }

  function render() {
    const list = $("queueList");
    if (!list || list.querySelector(".menu-open")) return; // do not close a «⋯» menu under the cursor
    const shown = items.filter((i) => SHOWN.includes(i.status));
    const waiting = shown.filter((i) => i.status === "waiting");
    const paused = shown.filter((i) => i.status === "limit");
    renderBanner(shown);
    const running = shown.some((i) => i.status === "downloading" || i.status === "analyzing");
    const cards = [];

    for (const u of uploads) {
      const pct = Math.round(u.pct * 100);
      cards.push(`
        <li class="q-item video-card ${u.error ? "failed" : "uploading"}">
          <div class="history-thumb-wrap q-thumb"><span class="q-ico">📁</span>
            ${u.error ? "" : `<div class="q-over"><b>Передаю файл у студію… ${pct}%</b><div class="q-bar"><i style="width:${pct}%"></i></div></div>`}</div>
          <span class="vc-badge${u.error ? " bad" : ""}">${u.error ? (u.noSpace ? "Немає місця" : "Не вдалося") : "📁 Файл"}</span>
          <div class="history-details">
            <span class="history-item-title" title="${escapeHtml(u.name)}">${escapeHtml(u.name)}</span>
            <span class="video-card-meta">${u.error ? (u.noSpace ? errorSummaryHtml(u.error) : escapeHtml(u.error)) : "Не закривай вкладку, поки файл передається"}</span>
            ${u.error ? `<div class="q-actions">${u.noSpace ? FREE_BTN : ""}<button type="button" class="btn btn-secondary btn-sm" data-dismiss="${u.key}">Прибрати</button></div>` : ""}
          </div>
        </li>`);
    }

    for (const it of shown) {
      const pct = Math.round((it.progress || 0) * 100);
      const busy = it.status === "downloading" || it.status === "analyzing";
      const wIndex = waiting.indexOf(it);
      const thumb = it.project_id
        ? `<img src="/api/projects/${encodeURIComponent(it.project_id)}/thumbnail" class="history-thumb-img" alt="" loading="lazy" onerror="this.remove()">`
        : "";
      const title = it.title && it.title !== it.source_url ? it.title : prettyUrl(it.source_url || it.title);
      let badge = STATUS[it.status];
      let meta = "";
      const pIndex = paused.indexOf(it);
      if (it.status === "limit") {
        if (pIndex > 0) badge = `⏸ Після паузи · ${pIndex + 1}-е`;
        meta = it.message || "Чекає на Gemini";
      } else if (it.status === "waiting" && paused.length) {
        badge = `У черзі · ${paused.length + wIndex + 1}-е`;
        meta = "Поки чекаємо на Gemini, студія розпізнає мову";
      } else if (it.status === "waiting") {
        badge = wIndex === 0 ? (running ? "У черзі · наступне" : "Зараз почнемо") : `У черзі · ${wIndex + 1}-е`;
        meta = it.clips_count ? `Чекає своєї черги · ${it.clips_count} кліпів` : "Чекає своєї черги";
      } else if (busy) {
        meta = "Можна закрити вкладку";
      } else if (it.status === "failed") {
        meta = it.error || it.message || "Помилка";
      }
      const limitOver = it.status === "limit" && pIndex === 0 ? `
            <div class="q-over">
              <b>Продовжимо самі ${escapeHtml(whenText(it.resume_at))}</b>
              <span class="q-count" data-count-to="${it.resume_at}">${countdown(it.resume_at)}</span>
              <div class="q-steps"><span class="ok">✓ Скачано</span><span class="ok">✓ Розпізнано</span><span class="now">⏸ Пошук</span></div>
            </div>` : "";
      const over = !busy ? limitOver : `
            <div class="q-over">
              <b>${escapeHtml(stageText(it.message))}${pct ? ` · ${pct}%` : ""}</b>
              <div class="q-bar${it.status === "downloading" && !pct ? " busy" : ""}"><i style="width:${pct}%"></i></div>
              <div class="q-steps">${steps(stepOf(it))}</div>
            </div>`;
      const menu = it.status === "limit" && pIndex > 0 ? `
          <button type="button" class="vc-dots" aria-haspopup="true" aria-expanded="false" title="Дії" aria-label="Дії">⋯</button>
          <div class="vc-menu" role="menu" hidden>
            <button type="button" role="menuitem" data-remove="${it.id}" class="danger">✕ Прибрати з черги</button>
          </div>` : it.status === "waiting" ? `
          <button type="button" class="vc-dots" aria-haspopup="true" aria-expanded="false" title="Дії" aria-label="Дії">⋯</button>
          <div class="vc-menu" role="menu" hidden>
            <button type="button" role="menuitem" data-move="${it.id}:-1" ${wIndex <= 0 ? "disabled" : ""}>↑ Раніше в черзі</button>
            <button type="button" role="menuitem" data-move="${it.id}:1" ${wIndex >= waiting.length - 1 ? "disabled" : ""}>↓ Пізніше в черзі</button>
          </div>` : "";
      if (it.no_space) badge = "Немає місця";
      const actions = it.status === "waiting" ? `
            <div class="q-actions"><button type="button" class="q-unqueue" data-remove="${it.id}">${icon("close")}Прибрати з черги</button></div>` : it.status === "limit" && pIndex === 0 ? `
            <div class="q-actions">
              <button type="button" class="btn btn-primary btn-sm" data-resume="${it.id}">${icon("refresh")}Спробувати зараз</button>
              <button type="button" class="btn btn-secondary btn-sm" data-remove="${it.id}">Прибрати</button>
            </div>` : it.status === "failed" ? `
            <div class="q-actions">${it.no_space ? FREE_BTN : ""}
              <button type="button" class="btn ${it.no_space ? "btn-secondary" : "btn-primary"} btn-sm" data-retry="${it.id}">${icon("refresh")}Ще раз</button>
              <button type="button" class="btn btn-secondary btn-sm" data-remove="${it.id}">Прибрати</button>
            </div>` : "";
      cards.push(`
        <li class="q-item video-card ${it.status}">
          <div class="history-thumb-wrap q-thumb">${over ? "" : `<span class="q-ico">${icon(it.status === "failed" ? "alert" : it.source_url && !it.project_id ? "link" : "play")}</span>`}${thumb}${over}</div>
          <span class="vc-badge${it.status === "failed" ? " bad" : it.status === "limit" ? " warn" : busy ? " info" : " plain"}">${escapeHtml(badge)}</span>${menu}
          <div class="history-details">
            <span class="history-item-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
            <span class="video-card-meta">${it.status === "failed" ? errorSummaryHtml(meta) : escapeHtml(meta)}</span>${actions}
          </div>
        </li>`);
    }
    list.innerHTML = cards.join("");
    if (paused.length) startTick();
    if (typeof Home !== "undefined") {
      const ids = [...activeProjectIds()].sort().join(",");
      if (ids !== lastIds) { lastIds = ids; Home.render(); } else Home.sync();
    }
  }

  const FREE_BTN = `<button type="button" class="btn btn-primary btn-sm" data-free-space="1">Звільнити місце</button>`;

  /** The notice under «Додай відео»: yellow when space runs low, red when new videos will not fit. */
  function renderDisk() {
    const box = $("diskNotice");
    if (!box) return;
    const level = disk && disk.level;
    box.hidden = level !== "low" && level !== "full";
    if (box.hidden) return;
    const where = disk.drive ? `На диску ${disk.drive}` : "На диску";
    const full = level === "full";
    box.classList.toggle("full", full);
    box.querySelector(".disk-notice-ic").textContent = full ? "⛔" : "💾";
    $("diskNoticeHead").textContent = full ? `${where} лишилось ${disk.free_text}. Нові відео не влізуть.` : `${where} лишилось ${disk.free_text}.`;
    const h = disk.video_hours;
    $("diskNoticeSub").textContent = full
      ? "Студія не почне качати нове відео, поки не звільниш місце."
      : `Цього вистачить приблизно на ${hoursText(h)} відео з YouTube. Можна звільнити місце від відео, з якими ти закінчив.`;
    $("diskNoticeBtn").className = `btn btn-sm ${full ? "btn-primary" : "btn-secondary"}`;
  }
  function hoursText(h) {
    if (h < 1) return "пів години";
    const n = Math.floor(h);
    const word = n % 10 === 1 && n % 100 !== 11 ? "годину" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "години" : "годин";
    return `${n} ${word}`;
  }

  function prettyUrl(u) {
    try {
      const url = new URL(u);
      return url.hostname.replace(/^www\./, "") + url.pathname + (url.search.length > 1 ? url.search : "");
    } catch (_) {
      return u || "";
    }
  }

  async function errorText(res) {
    const t = await res.text();
    try {
      const d = JSON.parse(t).detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d) && d[0] && d[0].msg) return d[0].msg;
    } catch (_) {}
    return t || `Помилка ${res.status}`;
  }

  function autoGrow() {
    const ta = $("urlInput");
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
  }

  function init() {
    const list = $("queueList");
    list.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.classList.contains("vc-dots")) { e.stopPropagation(); Home.toggleMenu(b.closest(".q-item")); return; }
      Home.closeMenus();
      if (b.dataset.remove) remove(b.dataset.remove);
      else if (b.dataset.retry) retry(b.dataset.retry);
      else if (b.dataset.resume) resume(b.dataset.resume);
      else if (b.dataset.dismiss) dismissUpload(b.dataset.dismiss);
      else if (b.dataset.freeSpace) Settings.open("disk");
      else if (b.dataset.open) loadProjectResults(b.dataset.open);
      else if (b.dataset.move) {
        const [id, dir] = b.dataset.move.split(":");
        move(id, parseInt(dir, 10));
      }
    });
    const ta = $("urlInput");
    ta.addEventListener("input", autoGrow);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitHero();
      }
    });
    // Seed «already seen» states so old finished items do not pop toasts on page load
    fetch("/api/queue").then((r) => r.ok && r.json()).then((d) => {
      (d && d.items || []).forEach((i) => { lastStatus[i.id] = i.status; });
    }).catch(() => {}).finally(refresh);
  }

  document.addEventListener("DOMContentLoaded", init);

  return { submitHero, addFiles, addProject, remove, retry, resume, move, clearFinished, refresh, parseLinks, activeProjectIds };
})();
