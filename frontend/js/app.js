/**
 * frontend/js/app.js — AI Video Moments Studio Frontend Controller
 * Fast, interactive, zero-rendering moments exploration & metadata management.
 */

let currentProject = null;
let currentClips = [];
let activeEventSource = null;

document.addEventListener("DOMContentLoaded", () => {
  initDropzone();
  loadRecentProjects();
});

// ─── Recent Sessions / History ───────────────────────────────────────────────

let isHistoryCollapsed = false;

async function loadRecentProjects() {
  const historyCard = document.getElementById("historyCard");
  const historyList = document.getElementById("historyList");
  const historyCount = document.getElementById("historyCount");

  try {
    const res = await fetch("/api/projects");
    if (!res.ok) return;
    const data = await res.json();
    const projects = data.projects || [];

    if (projects.length === 0) {
      if (historyCard) historyCard.style.display = "none";
      return;
    }

    if (historyCount) historyCount.textContent = projects.length;
    if (historyCard) historyCard.style.display = "block";

    historyList.innerHTML = "";
    projects.forEach((p) => {
      const item = document.createElement("div");
      item.className = "history-item";
      if (currentProject && currentProject.id === p.id) {
        item.classList.add("active");
      }

      const total = p.clips_count || 0;
      const reviewed = p.reviewed_count || 0;
      const kept = p.kept_count || 0;
      const pct = total ? Math.round((reviewed / total) * 100) : 0;
      const busy = p.status === "analyzing" || p.status === "transcribing" || p.status === "downloading";
      const statusText = busy ? "Обробляється…" : total ? `${total} кліпів · переглянуто ${pct}%${kept ? ` · обрано ${kept}` : ""}` : p.status === "failed" ? "Не вдалося · клік, щоб спробувати ще" : "Кліпів ще немає · клік, щоб знайти";
      const encodedId = encodeURIComponent(p.id);
      item.classList.add("video-card");

      item.innerHTML = `
        <div class="history-item-left" role="button" tabindex="0" title="Відкрити">
          <div class="history-thumb-wrap">
            <img src="/api/projects/${encodedId}/thumbnail" class="history-thumb-img" alt="">
            <span class="history-fallback-icon" style="display: none;">🎬</span>
            <span class="video-card-dur">${escapeHtml(p.duration_formatted || "00:00")}</span>
          </div>
          <div class="history-details">
            <span class="history-item-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>
            <span class="video-card-meta">${escapeHtml(statusText)}</span>
            <span class="video-card-prog${busy ? " busy" : ""}"><i style="width:${busy ? 40 : pct}%"></i></span>
          </div>
        </div>
        <button class="btn-history-delete" type="button" title="Видалити відео зі списку" aria-label="Видалити">✕</button>
      `;

      const thumbImg = item.querySelector(".history-thumb-img");
      if (thumbImg) {
        thumbImg.addEventListener("error", () => {
          thumbImg.style.display = "none";
          if (thumbImg.nextElementSibling) thumbImg.nextElementSibling.style.display = "flex";
        });
      }
      const openOrQueue = () => {
        if (!total && busy) {
          showToast("Це відео ще обробляється, дивись чергу вгорі.", "info");
          return;
        }
        if (!total && !busy && typeof Queue !== "undefined") {
          if (confirm(`У «${p.title}» ще немає кліпів. Поставити в чергу на пошук моментів?`)) Queue.addProject(p.id);
          return;
        }
        openProject(p.id);
      };
      item.querySelector(".history-item-left").addEventListener("click", openOrQueue);
      item.querySelector(".history-item-left").addEventListener("keydown", (ev) => { if (ev.key === "Enter") openOrQueue(); });
      item.querySelector(".btn-history-delete").addEventListener("click", (ev) => deleteProject(p.id, ev));

      historyList.appendChild(item);
    });
  } catch (err) {
    console.warn("Could not load recent projects:", err);
  }
}

function toggleHistoryCollapse() {
  const content = document.getElementById("historyContent");
  const arrow = document.getElementById("historyArrow");
  isHistoryCollapsed = !isHistoryCollapsed;
  if (isHistoryCollapsed) {
    content.style.display = "none";
    arrow.textContent = "▶";
  } else {
    content.style.display = "block";
    arrow.textContent = "▼";
  }
}

async function openProject(projectId) {
  if (currentProject && currentProject.id === projectId) {
    resetStudioState();
    showToast("Вибір сесії знято.");
    return;
  }
  await loadProjectResults(projectId);
  await loadRecentProjects();
}

async function deleteProject(projectId, event) {
  if (event) event.stopPropagation();
  if (!confirm("Видалити цю сесію з історії?")) return;

  try {
    const res = await fetch(`/api/projects/${projectId}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await res.text());
    showToast("Сесію видалено.");
    if (currentProject && currentProject.id === projectId) {
      resetStudioState();
    }
    await loadRecentProjects();
  } catch (err) {
    alert("Помилка видалення: " + err.message);
  }
}

// ─── Studio Reset & State Management ─────────────────────────────────────────

function resetStudioState() {
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }

  // Clear project state
  currentProject = null;
  currentClips = [];

  // Stop video playback and free resources
  const video = document.getElementById("sourceVideo");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  // Clear moments showcase and hide Step 2
  const momentsList = document.getElementById("momentsList");
  if (momentsList) momentsList.innerHTML = "";
  const step2Card = document.getElementById("step2Card");
  if (step2Card) step2Card.style.display = "none";

  // Hide active project banner
  const activeBar = document.getElementById("activeProjectBar");
  if (activeBar) activeBar.style.display = "none";

  // Reset inputs
  const fileInput = document.getElementById("fileInput");
  if (fileInput) fileInput.value = "";
  const urlInput = document.getElementById("urlInput");
  if (urlInput) urlInput.value = "";

  const videoInfoBox = document.getElementById("videoInfoBox");
  if (videoInfoBox) videoInfoBox.style.display = "none";
  const step1Actions = document.getElementById("step1Actions");
  if (step1Actions) step1Actions.style.display = "none";
  const progressBox = document.getElementById("analyzeProgressBox");
  if (progressBox) progressBox.style.display = "none";

  // Reset analyze button
  const btnAnalyze = document.getElementById("btnAnalyze");
  if (btnAnalyze) btnAnalyze.disabled = false;

  // Show Step 1 Source Card cleanly
  const step1Card = document.getElementById("step1Card");
  // "" lets the stylesheet's flex layout apply; "block" broke the centred hero.
  if (step1Card) step1Card.style.display = "";
  window.scrollTo(0, 0);

  // Unmark any active history items
  document.querySelectorAll(".history-item.active").forEach((el) => {
    el.classList.remove("active");
  });
}

// ─── Source Tabs ─────────────────────────────────────────────────────────────

function switchSourceTab(tab) {
  const tabFileBtn = document.getElementById("tabFileBtn");
  const tabUrlBtn = document.getElementById("tabUrlBtn");
  const tabFileContent = document.getElementById("tabFileContent");
  const tabUrlContent = document.getElementById("tabUrlContent");

  if (tab === "file") {
    tabFileBtn.classList.add("active");
    tabUrlBtn.classList.remove("active");
    tabFileContent.classList.add("active");
    tabUrlContent.classList.remove("active");
  } else {
    tabUrlBtn.classList.add("active");
    tabFileBtn.classList.remove("active");
    tabUrlContent.classList.add("active");
    tabFileContent.classList.remove("active");
  }
}

// ─── Drag & Drop Upload ──────────────────────────────────────────────────────

function initDropzone() {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("hover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("hover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      Queue.addFiles(e.dataTransfer.files);
    }
  });

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      Queue.addFiles(e.target.files);
    }
  });
}

async function uploadFile(file) {
  // Clear any existing moments/player from previous session immediately
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
  currentProject = null;
  currentClips = [];
  const video = document.getElementById("sourceVideo");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  const momentsList = document.getElementById("momentsList");
  if (momentsList) momentsList.innerHTML = "";
  const step2Card = document.getElementById("step2Card");
  if (step2Card) step2Card.style.display = "none";
  const activeBar = document.getElementById("activeProjectBar");
  if (activeBar) activeBar.style.display = "none";
  document.querySelectorAll(".history-item.active").forEach((el) => el.classList.remove("active"));

  const formData = new FormData();
  formData.append("file", file);

  const videoInfoBox = document.getElementById("videoInfoBox");
  const infoTitle = document.getElementById("infoTitle");
  const infoDuration = document.getElementById("infoDuration");
  const infoClips = document.getElementById("infoClips");
  const step1Actions = document.getElementById("step1Actions");

  infoTitle.textContent = `Завантаження "${file.name}"...`;
  infoDuration.textContent = "Обробка...";
  infoClips.textContent = "Зачекайте...";
  videoInfoBox.style.display = "block";

  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    currentProject = data.project;
    infoTitle.textContent = currentProject.title;
    infoDuration.textContent = `⏱ ${data.duration_formatted}`;

    const recClips = Math.min(100, Math.max(5, Math.round(currentProject.duration / 25)));
    infoClips.textContent = `Рекомендовано: ~${recClips} моментів`;
    syncClipsCount(recClips);

    step1Actions.style.display = "flex";
    if (data.reused) {
      showToast("Це відео вже збережено! Відкриваємо результати...");
      if (currentProject.status === "ready") {
        await loadProjectResults(currentProject.id);
        await loadRecentProjects();
        return;
      }
    } else {
      showToast("Відео успішно завантажено!");
    }
    await loadRecentProjects();
  } catch (err) {
    alert("Помилка завантаження файлу: " + err.message);
    videoInfoBox.style.display = "none";
  }
}

async function submitUrl() {
  const urlInput = document.getElementById("urlInput");
  const url = urlInput.value.trim();
  if (!url) {
    alert("Введіть посилання на відео.");
    return;
  }

  // Clear any existing moments/player from previous session immediately
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
  currentProject = null;
  currentClips = [];
  const video = document.getElementById("sourceVideo");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  const momentsList = document.getElementById("momentsList");
  if (momentsList) momentsList.innerHTML = "";
  const step2Card = document.getElementById("step2Card");
  if (step2Card) step2Card.style.display = "none";
  const activeBar = document.getElementById("activeProjectBar");
  if (activeBar) activeBar.style.display = "none";
  document.querySelectorAll(".history-item.active").forEach((el) => el.classList.remove("active"));

  const downloadBtn = document.getElementById("urlDownloadBtn");
  downloadBtn.disabled = true;
  downloadBtn.textContent = "⏳ Завантаження...";

  try {
    const res = await fetch("/api/upload-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    currentProject = data.project;
    document.getElementById("infoTitle").textContent = currentProject.title;
    document.getElementById("infoDuration").textContent = `⏱ ${data.duration_formatted}`;

    const recClips = Math.min(100, Math.max(5, Math.round(currentProject.duration / 25)));
    document.getElementById("infoClips").textContent = `Рекомендовано: ~${recClips} моментів`;
    syncClipsCount(recClips);

    document.getElementById("videoInfoBox").style.display = "block";
    document.getElementById("step1Actions").style.display = "flex";

    if (data.reused) {
      showToast("Це відео вже збережено! Відкриваємо результати...");
      if (currentProject.status === "ready") {
        await loadProjectResults(currentProject.id);
        await loadRecentProjects();
        return;
      }
    } else {
      showToast("Відео за посиланням завантажено!");
    }
    await loadRecentProjects();
  } catch (err) {
    alert("Не вдалося завантажити за посиланням: " + err.message);
  } finally {
    downloadBtn.disabled = false;
    downloadBtn.textContent = "⬇️ Завантажити";
  }
}

function syncClipsCount(val) {
  let num = parseInt(val, 10);
  if (isNaN(num)) num = 10;
  num = Math.max(1, Math.min(100, num));
  const slider = document.getElementById("clipsCountSlider");
  const numberInput = document.getElementById("clipsCountNumber");
  if (slider) slider.value = num;
  if (numberInput) numberInput.value = num;
}

// ─── AI Analysis ─────────────────────────────────────────────────────────────

async function startAnalysis() {
  if (!currentProject) {
    alert("Спочатку завантажте відео.");
    return;
  }
  // Everything goes through the processing queue now (one video at a time on the GPU)
  if (typeof Queue !== "undefined") {
    const clipsCount = parseInt(document.getElementById("clipsCountNumber").value, 10) || 0;
    const filterMode = document.getElementById("filterModeSelect").value;
    const id = currentProject.id;
    resetStudioState();
    try {
      await Queue.addProject(id, { clips_count: clipsCount, filter_mode: filterMode });
    } catch (err) {
      showToast(err.message, "error");
    }
    return;
  }

  const clipsCount = parseInt(document.getElementById("clipsCountNumber").value, 10) ||
                     parseInt(document.getElementById("clipsCountSlider").value, 10) || 10;
  const filterMode = document.getElementById("filterModeSelect").value;
  const btnAnalyze = document.getElementById("btnAnalyze");
  const progressBox = document.getElementById("analyzeProgressBox");
  const progressFill = document.getElementById("analyzeProgressFill");
  const progressMsg = document.getElementById("analyzeProgressMsg");
  const progressPct = document.getElementById("analyzeProgressPct");

  const errorActions = document.getElementById("analyzeErrorActions");
  if (errorActions) errorActions.style.display = "none";
  progressBox.classList.remove("progress-error");

  btnAnalyze.disabled = true;
  progressBox.style.display = "block";
  progressFill.style.width = "5%";
  progressMsg.textContent = "Ініціалізація...";
  progressPct.textContent = "5%";

  try {
    const res = await fetch(`/api/projects/${currentProject.id}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clips_count: clipsCount, filter_mode: filterMode, engine: "whisper_local" }),
    });

    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const jobId = data.job_id;

    // Connect to SSE stream
    if (activeEventSource) activeEventSource.close();
    activeEventSource = new EventSource(`/api/jobs/${jobId}/stream`);

    activeEventSource.onerror = (err) => {
      console.warn("SSE connection interrupted:", err);
      if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
      }
      btnAnalyze.disabled = false;
      progressMsg.textContent = "З'єднання перервано або завершено. Оновіть результати.";
    };

    activeEventSource.onmessage = async (e) => {
      try {
        const update = JSON.parse(e.data);
        const pct = Math.round((update.progress || 0) * 100);
        progressFill.style.width = `${pct}%`;
        progressPct.textContent = `${pct}%`;
        if (update.message) progressMsg.textContent = update.message;

        if (update.status === "completed") {
          activeEventSource.close();
          activeEventSource = null;
          btnAnalyze.disabled = false;
          showToast("Аналіз завершено! Завантажуємо результати...");
          await loadProjectResults(currentProject.id);
          await loadRecentProjects();
        } else if (update.status === "failed") {
          activeEventSource.close();
          activeEventSource = null;
          btnAnalyze.disabled = false;
          progressBox.classList.add("progress-error");
          progressMsg.textContent = "❌ " + (update.error || update.message || "Невідома помилка аналізу.");
          if (errorActions) errorActions.style.display = "flex";
          showToast("Аналіз не вдався. Можна спробувати ще раз.");
          await loadRecentProjects();
        }
      } catch (err) {
        console.error("SSE parse error", err);
      }
    };
  } catch (err) {
    btnAnalyze.disabled = false;
    alert("Не вдалося запустити аналіз: " + err.message);
  }
}

// ─── Load Results & Render Moments ───────────────────────────────────────────

async function loadProjectResults(projectId) {
  try {
    const res = await fetch(`/api/projects/${projectId}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    currentProject = data.project;
    currentClips = data.clips || [];

    // Clips found → open the «Монтажний стіл» review screen instead of the old list
    if (currentClips.length > 0 && typeof Editor !== "undefined") {
      Editor.open(currentProject, currentClips);
      return;
    }

    // Setup Video Player
    const video = document.getElementById("sourceVideo");
    video.src = `/api/media?path=${encodeURIComponent(currentProject.source_path)}`;
    video.load();

    // Render Clips List
    renderMomentsList(currentClips);

    // Hide Step 1 Source Card & Show Active Video Banner
    const step1Card = document.getElementById("step1Card");
    if (step1Card) step1Card.style.display = "none";

    const activeBar = document.getElementById("activeProjectBar");
    if (activeBar) {
      activeBar.style.display = "flex";
      document.getElementById("activeBarTitle").textContent = currentProject.title;
      const durSec = Math.round(currentProject.duration || 0);
      const m = Math.floor(durSec / 60);
      const s = durSec % 60;
      document.getElementById("activeBarMeta").textContent = `⏱ ${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')} · ${currentClips.length} кліпів`;
    }

    // Show Step 2
    const step2Card = document.getElementById("step2Card");
    step2Card.style.display = "block";
    step2Card.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    alert("Помилка завантаження результатів: " + err.message);
  }
}

function showSourceCard() {
  resetStudioState();
}

// Re-runs analysis for the already-loaded project (transcript is cached, so it is cheap)
// Goes through the queue, so it never runs at the same time as another video.
async function retryAnalysisForCurrentProject() {
  if (!currentProject) return;
  const project = currentProject;
  const marked = currentClips.filter((c) => c.review_status).length;
  if (marked && !confirm(`Шукати кліпи заново? Позначки «Беру» / «Не беру» (${marked}) для цього відео зникнуть.`)) {
    if (typeof Editor !== "undefined") Editor.open(project, currentClips);
    return;
  }
  resetStudioState();
  try {
    await Queue.addProject(project.id);
  } catch (err) {
    showToast(err.message, "error");
  }
  const qs = document.getElementById("queueSection");
  if (qs) qs.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderMomentsList(clips) {
  const container = document.getElementById("momentsList");
  const countTitle = document.getElementById("momentsCountTitle");
  container.innerHTML = "";

  countTitle.textContent = `🔥 Знайдено вірусних моментів: ${clips.length}`;

  if (!clips || clips.length === 0) {
    const failed = currentProject && currentProject.status === "failed";
    const msg = failed
      ? "❌ Попередній аналіз цього відео не вдався (наприклад, Gemini був перевантажений). Транскрипцію збережено — повторний аналіз буде швидким."
      : "Вірусних моментів не виявлено.";
    container.innerHTML = `
      <div class="empty-moments">
        <p style="color: var(--text-muted);">${msg}</p>
        <button class="btn btn-primary" onclick="retryAnalysisForCurrentProject()">🔄 Спробувати ще раз</button>
      </div>`;
    return;
  }

  clips.forEach((clip, index) => {
    const card = document.createElement("div");
    card.className = "moment-card";
    card.id = `momentCard_${index}`;

    const tagsHtml = (clip.hashtags || [])
      .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`)
      .join(" ");

    const clipIndex = clip.clip_index || (index + 1);
    const projectId = currentProject ? currentProject.id : "";

    card.innerHTML = `
      <div class="moment-header">
        <div class="moment-pills">
          <span class="moment-index-pill">#${escapeHtml(String(clipIndex))}</span>
          <span class="score-badge">🔥 ${escapeHtml(String(clip.virality_score || 85))}/100</span>
          <span class="info-badge">${escapeHtml(clip.duration_formatted || "")}</span>
        </div>
        <button class="timecode-btn" type="button" title="Клікніть, щоб переглянути цей фрагмент">
          ▶ ⏱️ ${escapeHtml(clip.timecode_formatted || "")}
        </button>
      </div>

      <div class="moment-title">${escapeHtml(clip.title)}</div>

      ${clip.hook_text ? `<div class="moment-hook">🎯 Початковий хук: «${escapeHtml(clip.hook_text)}»</div>` : ""}

      ${clip.reason ? `<div class="moment-reason">💡 <strong>Чому це вірусно:</strong> ${escapeHtml(clip.reason)}</div>` : ""}

      ${clip.caption ? `<div class="moment-caption">📝 <strong>Опис для соцмереж:</strong> ${escapeHtml(clip.caption)}</div>` : ""}

      ${tagsHtml ? `<div class="moment-tags">${tagsHtml}</div>` : ""}

      <div class="moment-actions">
        <button class="btn btn-primary btn-sm btn-slice-download" type="button" title="Миттєво вирізати та скачати цей фрагмент у вихідній якості (lossless MP4)">
          ⬇️ Скачати MP4
        </button>
        <button class="btn btn-secondary btn-sm btn-srt-download" type="button" title="Скачати субтитри .SRT (динамічний стиль Shorts/CapCut)">
          📝 Субтитри (.srt)
        </button>
        <button class="btn btn-secondary btn-sm btn-trim-open" type="button" title="Підкоригувати початок і кінець моменту">
          ✂️ Межі (Trim)
        </button>
        <button class="btn btn-secondary btn-sm btn-copy-short" type="button">
          📋 Заголовок
        </button>
        <button class="btn btn-secondary btn-sm btn-copy-full" type="button">
          📋 Для посту
        </button>
      </div>
    `;

    card.querySelector(".timecode-btn").addEventListener("click", () => seekToMoment(Number(clip.start_sec) || 0, index));
    const dlBtn = card.querySelector(".btn-slice-download");
    dlBtn.addEventListener("click", () => downloadClipSlice(dlBtn, projectId, clipIndex));
    const srtBtn = card.querySelector(".btn-srt-download");
    srtBtn.addEventListener("click", () => downloadClipSrt(srtBtn, projectId, clipIndex));
    const trimBtn = card.querySelector(".btn-trim-open");
    trimBtn.addEventListener("click", () => openTrimModal(clipIndex));
    card.querySelector(".btn-copy-short").addEventListener("click", () => copyClipTimecodeAndTitle(index));
    card.querySelector(".btn-copy-full").addEventListener("click", () => copyClipFull(index));

    container.appendChild(card);
  });
}

// ─── Interactive Player Seek ─────────────────────────────────────────────────

function seekToMoment(startSec, index) {
  const video = document.getElementById("sourceVideo");
  video.currentTime = Math.max(0, startSec);
  video.play().catch(() => {});

  // Visual highlight
  document.querySelectorAll(".moment-card").forEach((c) => (c.style.borderColor = "var(--border-color)"));
  const activeCard = document.getElementById(`momentCard_${index}`);
  if (activeCard) {
    activeCard.style.borderColor = "var(--accent-orange)";
  }
}

// ─── Moment Slice Downloader ────────────────────────────────────────────────

async function downloadClipSlice(btn, projectId, clipIndex) {
  if (btn.classList.contains("loading")) return;
  const origHtml = btn.innerHTML;
  btn.classList.add("loading");
  btn.style.opacity = "0.7";
  btn.style.pointerEvents = "none";
  btn.innerHTML = `⏳ Нарізка MP4...`;
  showToast("⏳ Миттєва нарізка фрагмента без втрати якості...", "info");

  try {
    const url = `/api/projects/${projectId}/clips/${clipIndex}/download`;
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;

    const disposition = res.headers.get("Content-Disposition") || "";
    let filename = `clip_${clipIndex}.mp4`;
    const matchStar = disposition.match(/filename\*=utf-8''([^;]+)/i);
    if (matchStar && matchStar[1]) {
      try { filename = decodeURIComponent(matchStar[1]); } catch(e){}
    } else {
      const matchPlain = disposition.match(/filename="?([^";]+)"?/i);
      if (matchPlain && matchPlain[1]) filename = matchPlain[1];
    }

    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 10000);
    showToast(`✅ Фрагмент «${filename}» збережено!`, "success");
  } catch (err) {
    showToast(`❌ Помилка завантаження кліпу: ${err.message}`, "error");
  } finally {
    btn.classList.remove("loading");
    btn.style.opacity = "";
    btn.style.pointerEvents = "";
    btn.innerHTML = origHtml;
  }
}

// ─── Subtitles (.SRT) Export ────────────────────────────────────────────────

async function downloadClipSrt(btn, projectId, clipIndex) {
  if (btn.classList.contains("loading")) return;
  const origHtml = btn.innerHTML;
  btn.classList.add("loading");
  btn.style.opacity = "0.7";
  btn.style.pointerEvents = "none";
  btn.innerHTML = `⏳ SRT...`;

  try {
    const url = `/api/projects/${projectId}/clips/${clipIndex}/export-srt`;
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    let filename = `clip_${clipIndex}.srt`;
    const disposition = res.headers.get("content-disposition");
    if (disposition && disposition.includes("filename=")) {
      filename = disposition.split("filename=")[1].replace(/["']/g, "").trim();
    }
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 10000);
    showToast(`✅ Субтитри «${filename}» збережено!`, "success");
  } catch (err) {
    showToast(`❌ Помилка експорту субтитрів: ${err.message}`, "error");
  } finally {
    btn.classList.remove("loading");
    btn.style.opacity = "";
    btn.style.pointerEvents = "";
    btn.innerHTML = origHtml;
  }
}

async function exportAllSubtitles() {
  if (!currentProject) {
    alert("Спочатку оберіть або завантажте відео.");
    return;
  }
  showToast("📦 Пакування всіх субтитрів у ZIP...", "info");
  try {
    const url = `/api/projects/${currentProject.id}/export-all-subtitles`;
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    let filename = `${currentProject.title}_subtitles.zip`;
    const disposition = res.headers.get("content-disposition");
    if (disposition && disposition.includes("filename=")) {
      filename = disposition.split("filename=")[1].replace(/["']/g, "").trim();
    }
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 10000);
    showToast("✅ Усі субтитри (.srt) завантажено в ZIP!", "success");
  } catch (err) {
    showToast(`❌ Помилка експорту архіву субтитрів: ${err.message}`, "error");
  }
}

// ─── Interactive Trim Modal ─────────────────────────────────────────────────

let activeTrimClip = null;
let trimTimeUpdateHandler = null;

function openTrimModal(clipIndex) {
  if (!currentProject) return;
  const clip = currentClips.find((c) => c.clip_index === clipIndex);
  if (!clip) return;

  activeTrimClip = clip;
  const modal = document.getElementById("trimModal");
  const modalBadge = document.getElementById("trimModalBadge");
  const modalTitle = document.getElementById("trimModalTitle");
  const startInput = document.getElementById("trimStartInput");
  const endInput = document.getElementById("trimEndInput");
  const trimVideo = document.getElementById("trimVideoPlayer");

  modalBadge.textContent = `КЛІП #${clip.clip_index}`;
  modalTitle.textContent = clip.title;

  const startVal = Number(clip.start_sec) || 0;
  const endVal = Number(clip.end_sec) || (startVal + 30);
  startInput.value = startVal.toFixed(1);
  endInput.value = endVal.toFixed(1);

  trimVideo.src = `/api/media?path=${encodeURIComponent(currentProject.source_path)}`;
  trimVideo.currentTime = startVal;
  updateTrimDurationSummary();

  if (trimTimeUpdateHandler) {
    trimVideo.removeEventListener("timeupdate", trimTimeUpdateHandler);
  }

  trimTimeUpdateHandler = () => {
    const curTime = trimVideo.currentTime;
    const m = Math.floor(curTime / 60);
    const s = curTime % 60;
    const curText = `${m.toString().padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
    const curEl = document.getElementById("trimCurrentTime");
    if (curEl) curEl.textContent = curText;

    const autoStopBox = document.getElementById("trimAutoStopCheck");
    const autoStop = autoStopBox ? autoStopBox.checked : true;
    const currentEnd = parseFloat(document.getElementById("trimEndInput").value) || endVal;
    if (autoStop && curTime >= currentEnd) {
      trimVideo.pause();
      trimVideo.currentTime = currentEnd;
    }
  };

  trimVideo.addEventListener("timeupdate", trimTimeUpdateHandler);
  modal.style.display = "flex";
}

function closeTrimModal() {
  const modal = document.getElementById("trimModal");
  const trimVideo = document.getElementById("trimVideoPlayer");
  if (trimVideo) {
    trimVideo.pause();
    if (trimTimeUpdateHandler) {
      trimVideo.removeEventListener("timeupdate", trimTimeUpdateHandler);
      trimTimeUpdateHandler = null;
    }
    trimVideo.removeAttribute("src");
    trimVideo.load();
  }
  if (modal) modal.style.display = "none";
  activeTrimClip = null;
}

function updateTrimDurationSummary() {
  const start = parseFloat(document.getElementById("trimStartInput").value) || 0;
  const end = parseFloat(document.getElementById("trimEndInput").value) || 0;
  const dur = Math.max(0, end - start);
  const durEl = document.getElementById("trimCalcDuration");
  if (durEl) {
    durEl.textContent = `${dur.toFixed(1)} сек`;
    durEl.style.color = dur >= 5.0 ? "var(--accent-orange)" : "#EF4444";
  }
}

function onTrimInputChange() {
  updateTrimDurationSummary();
}

function setTrimStartToCurrent() {
  const trimVideo = document.getElementById("trimVideoPlayer");
  if (!trimVideo) return;
  const cur = trimVideo.currentTime;
  document.getElementById("trimStartInput").value = cur.toFixed(1);
  updateTrimDurationSummary();
}

function setTrimEndToCurrent() {
  const trimVideo = document.getElementById("trimVideoPlayer");
  if (!trimVideo) return;
  const cur = trimVideo.currentTime;
  document.getElementById("trimEndInput").value = cur.toFixed(1);
  updateTrimDurationSummary();
}

function adjustTrimStart(delta) {
  const input = document.getElementById("trimStartInput");
  const trimVideo = document.getElementById("trimVideoPlayer");
  let val = (parseFloat(input.value) || 0) + delta;
  val = Math.max(0, Math.round(val * 10) / 10);
  input.value = val.toFixed(1);
  if (trimVideo) trimVideo.currentTime = val;
  updateTrimDurationSummary();
}

function adjustTrimEnd(delta) {
  const input = document.getElementById("trimEndInput");
  const trimVideo = document.getElementById("trimVideoPlayer");
  let val = (parseFloat(input.value) || 0) + delta;
  val = Math.max(0, Math.round(val * 10) / 10);
  input.value = val.toFixed(1);
  if (trimVideo) trimVideo.currentTime = val;
  updateTrimDurationSummary();
}

async function saveTrimChanges() {
  if (!activeTrimClip || !currentProject) return;
  const startSec = parseFloat(document.getElementById("trimStartInput").value);
  const endSec = parseFloat(document.getElementById("trimEndInput").value);

  if (isNaN(startSec) || isNaN(endSec)) {
    alert("Будь ласка, введіть коректні числові значення секунд.");
    return;
  }
  if (startSec >= endSec) {
    alert("Початок має бути меншим за кінець.");
    return;
  }
  if (endSec - startSec < 5.0) {
    alert("Мінімальна тривалість кліпу становить 5 секунд.");
    return;
  }

  const btnSave = document.getElementById("btnSaveTrim");
  btnSave.disabled = true;
  btnSave.textContent = "⏳ Збереження...";

  try {
    const res = await fetch(`/api/projects/${currentProject.id}/clips/${activeTrimClip.clip_index}/trim`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_sec: startSec, end_sec: endSec }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    const updated = data.clip;

    // Update in local array
    const targetIdx = currentClips.findIndex((c) => c.clip_index === activeTrimClip.clip_index);
    if (targetIdx !== -1) {
      currentClips[targetIdx] = { ...currentClips[targetIdx], ...updated };
    }

    // Update DOM card
    const card = document.getElementById(`momentCard_${targetIdx}`);
    if (card) {
      const tcBtn = card.querySelector(".timecode-btn");
      if (tcBtn) tcBtn.innerHTML = `▶ ⏱️ ${escapeHtml(updated.timecode_formatted || "")}`;
      const durBadge = card.querySelectorAll(".moment-pills .info-badge")[0];
      if (durBadge) durBadge.textContent = escapeHtml(updated.duration_formatted || "");
    }

    closeTrimModal();
    showToast("✅ Межі моменту успішно оновлено!", "success");
  } catch (err) {
    alert("Не вдалося зберегти зміни: " + err.message);
  } finally {
    btnSave.disabled = false;
    btnSave.textContent = "💾 Зберегти нові межі";
  }
}

// ─── Copy & Export Helpers ───────────────────────────────────────────────────

let toastTimer = null;

function copyClipTimecodeAndTitle(index) {
  const clip = currentClips[index];
  if (!clip) return;
  const text = `${clip.title}\n⏱️ ${clip.timecode_formatted}`;
  copyTextToClipboard(text, "Заголовок та таймкод скопійовано!");
}

function copyClipFull(index) {
  const clip = currentClips[index];
  if (!clip) return;
  const tags = (clip.hashtags || []).join(" ");
  const text = `🎬 ${clip.title}\n⏱️ Таймкод: ${clip.timecode_formatted} (${clip.duration_formatted})\n🔥 Вірусність: ${clip.virality_score}/100\n🎯 Хук: «${clip.hook_text}»\n\n📝 Опис:\n${clip.caption}\n\n🏷️ Хештеги:\n${tags}`;
  copyTextToClipboard(text, "Повні метадані кліпу скопійовано!");
}

function copyAllMoments() {
  if (!currentClips || currentClips.length === 0) return;
  let fullText = `====================================================\nВІРУСНІ МОМЕНТИ: ${currentProject.title}\n====================================================\n\n`;

  currentClips.forEach((clip, i) => {
    const tags = (clip.hashtags || []).join(" ");
    fullText += `--- КЛІП #${i + 1}: ${clip.title} ---\n`;
    fullText += `⏱️ Таймкод: ${clip.timecode_formatted} (${clip.duration_formatted})\n`;
    fullText += `🔥 Вірусність: ${clip.virality_score}/100\n`;
    if (clip.hook_text) fullText += `🎯 Хук: «${clip.hook_text}»\n`;
    if (clip.reason) fullText += `💡 Чому вірусно: ${clip.reason}\n`;
    if (clip.caption) fullText += `📝 Опис: ${clip.caption}\n`;
    if (tags) fullText += `🏷️ Хештеги: ${tags}\n`;
    fullText += `\n`;
  });

  copyTextToClipboard(fullText, "Всі моменти скопійовано в буфер обміну!");
}

function copyTextToClipboard(text, successMsg) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      showToast(successMsg);
    }).catch(() => {
      fallbackCopyText(text, successMsg);
    });
  } else {
    fallbackCopyText(text, successMsg);
  }
}

function fallbackCopyText(text, successMsg) {
  try {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.opacity = "0";
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand("copy");
    document.body.removeChild(textArea);
    showToast(successMsg);
  } catch (err) {
    alert("Не вдалося скопіювати текст у буфер: " + err);
  }
}

function exportTxt() {
  if (!currentProject) return;
  window.open(`/api/projects/${currentProject.id}/export-txt`, "_blank");
}

function exportJson() {
  if (!currentProject) return;
  window.open(`/api/projects/${currentProject.id}/export-json`, "_blank");
}

function showToast(msg, type = "success") {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.remove("toast-success", "toast-info", "toast-error");
  if (type === "error") {
    toast.classList.add("toast-error");
  } else if (type === "info") {
    toast.classList.add("toast-info");
  } else {
    toast.classList.add("toast-success");
  }
  toast.classList.add("show");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("show");
  }, 2800);
}

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
