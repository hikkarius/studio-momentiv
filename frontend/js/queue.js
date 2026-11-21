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
      $("queueSection").scrollIntoView({ behavior: "smooth", block: "nearest" });
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
    for (const file of files) {
      const up = { key: Math.random().toString(36).slice(2), name: file.name, pct: 0, error: "" };
      uploads.push(up);
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
      }
      render();
    }
    $("fileInput").value = "";
    loadRecentProjects();
  }

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
    waiting: "Чекає",
    downloading: "Завантажується",
    analyzing: "Шукаємо моменти",
    done: "Готово",
    failed: "Помилка",
  };

  function render() {
    const section = $("queueSection");
    const list = $("queueList");
    if (!section || !list) return;
    const total = items.length + uploads.length;
    section.hidden = total === 0;
    if (!total) return;

    const working = items.filter((i) => i.status !== "done" && i.status !== "failed").length + uploads.filter((u) => !u.error).length;
    $("queueCount").textContent = working ? `· в роботі ${working}` : "· усе готово";
    $("queueClear").hidden = !items.some((i) => i.status === "done" || i.status === "failed");

    const waiting = items.filter((i) => i.status === "waiting");
    const rows = [];

    for (const u of uploads) {
      const pct = Math.round(u.pct * 100);
      rows.push(`
        <li class="q-item ${u.error ? "failed" : "uploading"}">
          <div class="q-thumb"><span>📁</span></div>
          <div class="q-main">
            <div class="q-title" title="${escapeHtml(u.name)}">${escapeHtml(u.name)}</div>
            <div class="q-msg">${u.error ? escapeHtml(u.error) : `Передаємо файл у студію: ${pct}%`}</div>
            ${u.error ? "" : `<div class="q-bar"><i style="width:${pct}%"></i></div>`}
          </div>
          <span class="q-pill ${u.error ? "failed" : "downloading"}">${u.error ? "Помилка" : "Файл"}</span>
          <div class="q-actions">${u.error ? `<button type="button" class="q-icon" data-dismiss="${u.key}" title="Прибрати">✕</button>` : ""}</div>
        </li>`);
    }

    for (const it of items) {
      const pct = Math.round((it.progress || 0) * 100);
      const busy = it.status === "downloading" || it.status === "analyzing";
      const wIndex = waiting.indexOf(it);
      const thumb = it.project_id
        ? `<img src="/api/projects/${encodeURIComponent(it.project_id)}/thumbnail" alt="" loading="lazy" onerror="this.remove()">`
        : "";
      const title = it.title && it.title !== it.source_url ? it.title : prettyUrl(it.source_url || it.title);
      let msg = it.message || "";
      if (it.status === "waiting") msg = wIndex === 0 && !items.some((i) => i.status === "downloading" || i.status === "analyzing") ? "Зараз почнемо…" : `У черзі: ${wIndex + 1}-е`;
      if (it.status === "failed") msg = it.error || msg;
      const count = it.clips_count ? `${it.clips_count} кліпів` : "кліпів: авто";

      let actions = "";
      if (it.status === "waiting") {
        actions = `
          <button type="button" class="q-icon" data-move="${it.id}:-1" title="Раніше" ${wIndex <= 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="q-icon" data-move="${it.id}:1" title="Пізніше" ${wIndex >= waiting.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="q-icon" data-remove="${it.id}" title="Прибрати з черги">✕</button>`;
      } else if (it.status === "done") {
        actions = `
          ${it.project_id ? `<button type="button" class="btn btn-primary btn-sm" data-open="${escapeHtml(it.project_id)}">Відкрити кліпи →</button>` : ""}
          <button type="button" class="q-icon" data-remove="${it.id}" title="Прибрати зі списку">✕</button>`;
      } else if (it.status === "failed") {
        actions = `
          <button type="button" class="btn btn-secondary btn-sm" data-retry="${it.id}">↻ Ще раз</button>
          <button type="button" class="q-icon" data-remove="${it.id}" title="Прибрати зі списку">✕</button>`;
      }

      rows.push(`
        <li class="q-item ${it.status}">
          <div class="q-thumb">${thumb}<span>${it.source_url && !it.project_id ? "🔗" : "🎬"}</span></div>
          <div class="q-main">
            <div class="q-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
            <div class="q-msg">${escapeHtml(msg)}<span class="q-meta"> · ${escapeHtml(count)}</span></div>
            ${busy ? `<div class="q-bar${it.status === "downloading" && !pct ? " busy" : ""}"><i style="width:${pct}%"></i></div>` : ""}
          </div>
          <span class="q-pill ${it.status}">${STATUS[it.status] || it.status}${busy && pct ? ` ${pct}%` : ""}</span>
          <div class="q-actions">${actions}</div>
        </li>`);
    }
    list.innerHTML = rows.join("");
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
      if (b.dataset.remove) remove(b.dataset.remove);
      else if (b.dataset.retry) retry(b.dataset.retry);
      else if (b.dataset.dismiss) dismissUpload(b.dataset.dismiss);
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

  return { submitHero, addFiles, addProject, remove, retry, move, clearFinished, refresh, parseLinks };
})();
