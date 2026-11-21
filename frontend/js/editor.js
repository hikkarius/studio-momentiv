/**
 * editor.js — «Монтажний стіл»: step rail on the left (Крок 1 / Крок 2), player and side panel, clip strip at the bottom.
 * Watch a clip, press «Беру» / «Не беру», move on. Right under the video sits the clip track:
 * the words laid out on time, draggable bracket edges, drag anywhere else to scrub, and a word menu
 * (start / end / listen). The playhead never leaves the clip: to hear more, move a bracket.
 * Clips are numbered by their place in the video (№1 = earliest), the same in the list and the strip.
 * Depends on app.js globals: resetStudioState, loadRecentProjects, retryAnalysisForCurrentProject,
 * escapeHtml; and on theme.js (StudioTheme).
 */
const Editor = (() => {
  const $ = (id) => document.getElementById(id);
  const st = {
    project: null, clips: [], cur: -1, filter: "all", tab: "text", sort: "score", mode: "table",
    words: [], wordsFor: null, stopAt: null, backToStart: false, saveTimer: null,
    zoom: { lo: 0, hi: 1 }, drag: null, scrub: null, pop: null,
  };
  const MIN_LEN = 3;

  // ─── Helpers ───────────────────────────────────────────────────────────────
  const fmt = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); const x = s - m * 60; return `${m}:${x < 10 ? "0" : ""}${x.toFixed(1)}`; };
  const fmtS = (s) => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const esc = (s) => (typeof escapeHtml === "function" ? escapeHtml(String(s ?? "")) : String(s ?? ""));
  const pid = () => encodeURIComponent(st.project.id);
  const clip = () => st.clips[st.cur];
  const video = () => $("edVideo");
  const len = (c) => c.end_sec - c.start_sec;

  function toast(msg, err = false) {
    const t = $("edToast");
    t.textContent = msg;
    t.classList.toggle("err", !!err);
    t.classList.add("on");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("on"), err ? 4000 : 1800);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  // ─── Numbering & ordering ──────────────────────────────────────────────────
  /** №N = place in the video. Computed once per open, so trimming never renumbers clips. */
  function assignNumbers() {
    [...st.clips.keys()].sort((a, b) => st.clips[a].start_sec - st.clips[b].start_sec).forEach((i, k) => { st.clips[i].num = k + 1; });
  }
  const byScore = () => [...st.clips.keys()].sort((a, b) => (st.clips[b].virality_score || 0) - (st.clips[a].virality_score || 0) || st.clips[a].num - st.clips[b].num);
  const byTime = () => [...st.clips.keys()].sort((a, b) => st.clips[a].num - st.clips[b].num);
  const ordered = () => (st.sort === "time" ? byTime() : byScore());
  const matches = (c) => st.filter === "all" || (st.filter === "new" ? !c.review_status : c.review_status === st.filter);
  const visible = () => ordered().filter((i) => matches(st.clips[i]));

  // ─── Rendering ─────────────────────────────────────────────────────────────
  /** On step 2 the strip lists the kept clips (all of them while nothing is kept yet). */
  function stripClips() {
    if (st.mode !== "vx") return visible();
    const kept = byTime().filter((i) => st.clips[i].review_status === "keep");
    return kept.length ? kept : byTime();
  }

  function renderList() {
    const L = $("edList");
    L.innerHTML = "";
    const vis = stripClips();
    vis.forEach((i) => {
      const c = st.clips[i];
      const r = c.review_status || "";
      const li = document.createElement("li");
      li.className = "ed-row" + (r === "drop" ? " dropped" : "");
      li.tabIndex = 0;
      li.dataset.i = i;
      li.setAttribute("aria-current", String(i === st.cur));
      li.title = c.title || "";
      li.innerHTML = `<div class="ed-th"><img src="/api/projects/${pid()}/clips/${c.clip_index}/thumbnail?s=${Math.round(c.start_sec * 10)}" alt="" draggable="false" loading="lazy" onerror="this.remove()">
          <span class="ed-num">№${c.num}</span><span class="ed-dur">${Math.round(len(c))} с</span>${r === "keep" ? '<span class="ed-ok" title="Беру">✓</span>' : ""}</div>
        <div class="ed-rb"><div class="ed-rt">${esc(c.title)}</div>
        <div class="ed-rm"><span>${fmtS(c.start_sec)}${r === "drop" ? " · не беру" : ""}</span><span class="s">★ ${esc(c.virality_score)}</span></div></div>`;
      li.addEventListener("click", () => select(i));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") select(i); });
      L.appendChild(li);
    });
    if (!vis.length) L.innerHTML = `<li class="ed-empty">Тут поки порожньо.</li>`;
    const anyKept = st.clips.some((c) => c.review_status === "keep");
    $("edStripTitle").textContent = st.mode === "vx" && anyKept ? "Обрані кліпи" : "Кліпи";
    const note = document.querySelector("#editor .ed-vx-note");
    note.textContent = anyKept ? "Тут лише кліпи з позначкою «Беру». Щоб додати ще, повернись на крок 1." : "Ще нічого не обрано: познач кліпи «Беру» на кроці 1, тоді тут будуть лише вони.";

    const count = (f) => st.clips.filter((c) => (f === "all" ? true : f === "new" ? !c.review_status : c.review_status === f)).length;
    ["all", "new", "keep", "drop"].forEach((f) => { $(`edCnt_${f}`).textContent = count(f); });
    renderProgress();
    updateScroll();
  }

  /** Header bar and the step counters: how many clips are reviewed and kept. */
  function renderProgress() {
    const n = st.clips.length || 1;
    const kept = st.clips.filter((c) => c.review_status === "keep").length;
    const seen = st.clips.filter((c) => c.review_status).length;
    $("edProgKeep").style.width = `${(kept / n) * 100}%`;
    $("edProgSeen").style.width = `${((seen - kept) / n) * 100}%`;
    $("edProgTxt").textContent = `${seen} з ${st.clips.length}`;
    $("edCntKeep").textContent = String(kept);
    $("edCntDrop").textContent = String(seen - kept);
    $("edCntLeft").textContent = String(st.clips.length - seen);
    const mini = $("editor").classList.contains("rail-mini");
    const allSeen = seen === st.clips.length && st.clips.length > 0;
    const b1 = document.querySelector('#edMode [data-m="table"]');
    b1.classList.toggle("done", allSeen && st.mode === "vx");
    $("edStep1").textContent = allSeen && st.mode === "vx" ? (mini ? `✓ ${kept}` : `✓ обрано ${kept}`) : (mini ? `${seen}/${st.clips.length}` : `переглянуто ${seen}/${st.clips.length}`);
    $("edStep2").textContent = mini ? String(kept) : st.mode === "vx" ? `${kept} ${kept === 1 ? "кліп" : kept > 1 && kept < 5 ? "кліпи" : "кліпів"}` : `обрано ${kept}`;
  }

  function updateScroll() {
    const L = $("edList");
    const more = L.scrollWidth > L.clientWidth + 4;
    $("edScrollPrev").hidden = !more || L.scrollLeft < 8;
    $("edScrollNext").hidden = !more || L.scrollLeft + L.clientWidth >= L.scrollWidth - 8;
  }

  function scrollToCurrent() {
    const row = $("edList").querySelector('[aria-current="true"]');
    if (row) row.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  /** Thin map of the whole video next to the strip filters: every clip numbered by its place in the video. */
  function renderOverview() {
    const el = $("edOv");
    const dur = st.project.duration || 1;
    let h = "";
    [...st.clips.keys()].sort((a, b) => st.clips[a].start_sec - st.clips[b].start_sec).forEach((i) => {
      const c = st.clips[i];
      const w = Math.max(0.6, (len(c) / dur) * 100);
      h += `<div class="c${c.review_status ? " " + c.review_status : ""}${i === st.cur ? " sel" : ""}" data-i="${i}"
        style="left:${(c.start_sec / dur) * 100}%;width:${w}%" title="№${c.num} ${esc(c.title)} (${fmtS(c.start_sec)})"><span class="lab">${c.num}</span></div>`;
    });
    h += `<div class="pl" id="edPh"></div>`;
    el.innerHTML = h;
    placePlayhead(video().currentTime || 0);
  }

  function renderDetails() {
    const c = clip();
    if (!c) return;
    $("edNo").textContent = `№${c.num}`;
    if (document.activeElement !== $("edClipTitle")) $("edClipTitle").textContent = c.title || "";
    $("edScore").innerHTML = c.virality_score != null ? `оцінка ШІ <b>★ ${esc(c.virality_score)}</b>` : "";
    $("vxClipNo").textContent = `№${c.num} · ${c.title || ""}`;
    $("edKeep").setAttribute("aria-pressed", String(c.review_status === "keep"));
    $("edDrop").setAttribute("aria-pressed", String(c.review_status === "drop"));
    $("vxKeep").setAttribute("aria-pressed", String(c.review_status === "keep"));
    $("vxDrop").setAttribute("aria-pressed", String(c.review_status === "drop"));
    ["edDecide", "vxDecide"].forEach((id) => {
      const d = $(id);
      d.classList.toggle("is-keep", c.review_status === "keep");
      d.classList.toggle("is-drop", c.review_status === "drop");
      d.querySelector(".ed-verdict-b").textContent = c.review_status === "keep" ? "✓ Беру · у добірці" : c.review_status === "drop" ? "✕ Не беру" : "";
    });
    $("edWhy").innerHTML = c.reason ? `<b>Чому:</b> ${esc(c.reason)}` : "";
    $("edStartTc").textContent = fmt(c.start_sec);
    $("edEndTc").textContent = fmt(c.end_sec);
    $("edLen").textContent = `${Math.round(len(c))} с`;
    if (document.activeElement !== $("edTitle")) $("edTitle").value = c.title || "";
    if (document.activeElement !== $("edCaption")) $("edCaption").value = c.caption || "";
    if (document.activeElement !== $("edTags")) $("edTags").value = (c.hashtags || []).join(" ");
    $("edMp4Info").innerHTML = `${Math.round(len(c))} с<span class="q"> · оригінальна якість</span>`;
    renderTc();
  }

  function renderTc(t = video().currentTime) {
    const c = clip();
    if (!c) return;
    const inClip = t >= c.start_sec - 0.05 && t <= c.end_sec + 0.05;
    $("edTc").innerHTML = inClip ? `<b>${fmt(Math.max(0, t - c.start_sec))}</b> / ${fmt(len(c))}` : `<b>${fmt(t)}</b> поза кліпом`;
  }

  function renderWords() {
    const W = $("edWords");
    const c = clip();
    if (!st.words.length) {
      W.innerHTML = `<span class="ed-small">Тексту для цього відео не знайдено. Межі можна змінити жовтими краями під відео.</span>`;
      return;
    }
    W.innerHTML = "";
    let first = null;
    st.words.forEach((w, j) => {
      const out = w.start < c.start_sec - 0.05 || w.start >= c.end_sec;
      const sp = document.createElement("span");
      sp.className = "ed-w" + (out ? " out" : "");
      sp.textContent = w.word;
      sp.dataset.j = j;
      sp.title = out ? "Поза кліпом" : `Слухати з ${fmt(w.start)}`;
      sp.addEventListener("click", () => wordClick(j));
      W.appendChild(sp);
      W.appendChild(document.createTextNode(" "));
      if (!out && !first) first = sp;
    });
    const body = $("edTabText");
    if (first && !body.hidden) body.scrollTop = Math.max(0, first.offsetTop - body.offsetTop - 120);
  }

  function renderTabs() {
    document.querySelectorAll("#edTabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === st.tab)));
    $("edTabText").hidden = st.tab !== "text";
    $("edTabPost").hidden = st.tab !== "post";
  }

  /** List order: best score first, or as the clips go in the video. Remembered. */
  function setSort(v) {
    st.sort = v === "time" ? "time" : "score";
    try { localStorage.setItem("studio-sort", st.sort); } catch (_) {}
    document.querySelectorAll("#edSort button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.s === st.sort)));
    if (st.project) renderList();
  }

  /** «✂ Монтажний стіл» or «📱 Вертикальні кліпи»: two screens over the same clip list and player. */
  function setMode(m) {
    st.mode = m === "vx" ? "vx" : "table";
    try { localStorage.setItem("studio-mode", st.mode); } catch (_) {}
    document.querySelectorAll("#edMode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.m === st.mode)));
    const vx = st.mode === "vx";
    document.querySelector("#editor .ed-cols").classList.toggle("vx-mode", vx);
    document.getElementById("editor").classList.toggle("is-vx", vx);
    $("edVx").hidden = !vx;
    if (st.project) renderList();
    if (vx) { if (typeof Vertical !== "undefined") Vertical.onShow(); }
    else if (st.project && clip()) { renderOverview(); renderTrack(); }
  }

  // ─── Trim track ────────────────────────────────────────────────────────────
  function computeZoom() {
    const c = clip();
    const pad = Math.max(4, len(c) * 0.25);
    return { lo: Math.max(0, c.start_sec - pad), hi: Math.min(st.project.duration || c.end_sec + pad, c.end_sec + pad) };
  }

  function renderTrack() {
    const c = clip();
    if (!c) return;
    if (!st.drag) st.zoom = computeZoom();
    const { lo, hi } = st.zoom;
    const T = $("edTrack");
    const W = T.clientWidth || 600;
    const p = (t) => ((t - lo) / (hi - lo)) * 100;
    let h = "";
    const end = [-1e9, -1e9];
    st.words.forEach((w, j) => {
      if (w.start < lo || w.start > hi) return;
      const x = (p(w.start) / 100) * W;
      const wd = w.word.length * 7 + 14;
      const row = x >= end[0] + 3 ? 0 : x >= end[1] + 3 ? 1 : -1;
      if (row < 0) { h += `<span class="wt" style="left:${p(w.start)}%"></span>`; return; }
      end[row] = x + wd;
      const inside = w.start >= c.start_sec - 0.05 && w.start < c.end_sec;
      h += `<span class="w${inside ? " in" : ""}${row ? " row2" : ""}" data-j="${j}" style="left:${p(w.start)}%">${esc(w.word)}</span>`;
    });
    h += `<div class="shade" style="left:0;width:${p(c.start_sec)}%"></div><div class="shade" style="left:${p(c.end_sec)}%;right:0"></div>`;
    h += `<div class="rg" style="left:${p(c.start_sec)}%;width:${p(c.end_sec) - p(c.start_sec)}%"></div>`;
    h += `<div class="hd" data-h="start" style="left:${p(c.start_sec)}%" title="Тягни: початок кліпу"></div>`;
    h += `<div class="hd" data-h="end" style="left:${p(c.end_sec)}%" title="Тягни: кінець кліпу"></div>`;
    h += `<div class="zph" id="edZph"></div>`;
    T.innerHTML = h;
    placePlayhead(video().currentTime || 0);
  }

  function placePlayhead(t) {
    const ph = $("edPh");
    if (ph) ph.style.left = `${(t / (st.project.duration || 1)) * 100}%`;
    const z = $("edZph");
    if (z) {
      const { lo, hi } = st.zoom;
      z.style.display = t >= lo && t <= hi ? "" : "none";
      z.style.left = `${((t - lo) / (hi - lo)) * 100}%`;
    }
  }

  // ─── Selection & playback ──────────────────────────────────────────────────
  async function select(i, autoplay = false) {
    if (i < 0 || i >= st.clips.length) return;
    st.cur = i;
    video().pause();
    st.stopAt = null;
    st.words = [];
    closePop();
    renderList();
    renderOverview();
    renderDetails();
    renderTrack();
    scrollToCurrent();
    seek(clip().start_sec);
    if (typeof Vertical !== "undefined") Vertical.onClip();
    await loadWords();
    if (autoplay) playClip();
  }

  /** The playhead always stays inside the clip bounds; moving a bound is the way to reach further. */
  const clamp = (t) => { const c = clip(); return c ? Math.min(Math.max(t, c.start_sec), c.end_sec) : Math.max(0, t); };
  function seek(t) {
    t = clamp(t);
    try { video().currentTime = Math.max(0, t); } catch (_) {}
    placePlayhead(t);
    renderTc(t);
  }

  async function loadWords() {
    const c = clip();
    const key = `${c.clip_index}`;
    st.wordsFor = key;
    $("edWords").textContent = "Завантаження тексту…";
    let words = [];
    try {
      const data = await api(`/api/projects/${pid()}/clips/${c.clip_index}/words?pad=10`);
      words = data.words || [];
    } catch (_) { /* no transcript: the editor still works with the edges */ }
    if (st.wordsFor !== key) return;
    st.words = words;
    renderWords();
    renderTrack();
  }

  /** Play from the playhead to the clip end, then jump back to the start. At the very end: start over. */
  function playClip(from) {
    const v = video();
    const c = clip();
    if (!c) return;
    if (from != null) seek(from);
    else if (v.currentTime >= c.end_sec - 0.1 || v.currentTime < c.start_sec - 0.05) seek(c.start_sec);
    st.stopAt = c.end_sec; st.backToStart = true;
    v.play().catch(() => {});
  }
  function togglePlay() { if (!video().paused) video().pause(); else playClip(); }

  function wordClick(j) {
    const w = st.words[j];
    const c = clip();
    if (!w) return;
    if (w.start < c.start_sec - 0.05 || w.start >= c.end_sec) {
      toast("Це слово поза кліпом. Щоб його додати, потягни жовту дужку під відео.");
      return;
    }
    playClip(Math.max(c.start_sec, w.start - 0.05));
  }

  function onTime() {
    const v = video();
    const c = clip();
    if (!c) return;
    let t = v.currentTime;
    if (!st.drag && (t < c.start_sec - 0.15 || t > c.end_sec + 0.15)) { // never show a playhead outside the clip
      if (!v.paused) v.pause();
      seek(t);
      t = clamp(t);
    }
    placePlayhead(t);
    renderTc(t);
    if (st.stopAt != null && t >= st.stopAt && !v.paused) {
      v.pause();
      const back = st.backToStart;
      st.stopAt = null;
      if (back) setTimeout(() => { if (v.paused && !st.scrub) seek(clip().start_sec); }, 350);
    }
    const W = $("edWords");
    const prev = W.querySelector(".ed-w.now");
    const j = wordAt(t);
    if (prev && prev.dataset.j !== String(j)) prev.classList.remove("now");
    if (j >= 0 && !v.paused) {
      const el = W.querySelector(`.ed-w[data-j="${j}"]`);
      if (el && !el.classList.contains("now")) el.classList.add("now");
    }
  }

  function onPlayState() {
    const paused = video().paused;
    $("edScreen").classList.toggle("paused", paused);
    $("edPlay").textContent = paused ? "▶" : "❚❚";
    $("edPlay").setAttribute("aria-label", paused ? "Грати" : "Пауза");
    if (paused) { const n = $("edWords").querySelector(".ed-w.now"); if (n) n.classList.remove("now"); }
  }

  function wordAt(t) {
    let j = -1;
    for (let k = 0; k < st.words.length; k++) { if (st.words[k].start <= t + 0.02) j = k; else break; }
    return j;
  }

  // ─── Editing bounds ────────────────────────────────────────────────────────
  function setBounds(start, end, { save = true } = {}) {
    const c = clip();
    const dur = st.project.duration || end;
    start = Math.max(0, Math.round(start * 100) / 100);
    end = Math.min(dur, Math.round(end * 100) / 100);
    if (end - start < MIN_LEN) { toast(`Кліп має тривати щонайменше ${MIN_LEN} с`, true); return false; }
    c.start_sec = start;
    c.end_sec = end;
    c.duration = end - start;
    if (!st.drag) { const t = video().currentTime; if (t < start || t > end) seek(t); }
    renderDetails();
    renderTrack();
    if (!save) return true;
    renderOverview();
    renderList();
    renderWords();
    clearTimeout(st.saveTimer);
    const idx = c.clip_index;
    st.saveTimer = setTimeout(async () => {
      try {
        await api(`/api/projects/${pid()}/clips/${idx}/trim`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ start_sec: start, end_sec: end }),
        });
        toast(`Межі збережено: ${fmt(start)} – ${fmt(end)}`);
        if (typeof Vertical !== "undefined" && clip() === c) Vertical.onClip();
      } catch (e) { toast("Не вдалося зберегти межі: " + e.message, true); }
    }, 500);
    return true;
  }

  const startAt = (j) => Math.max(st.words[j - 1]?.end ?? 0, st.words[j].start - 0.12);
  const endAt = (j) => (st.words[j + 1] ? Math.min(st.words[j + 1].start, st.words[j].end + 0.3) : st.words[j].end + 0.3);

  /** Snap a dragged edge to a clean word edge when one is within 0.6 s. */
  function snap(which, t) {
    let best = null, bd = 0.6;
    st.words.forEach((w, j) => {
      const d = Math.abs((which === "start" ? w.start : w.end) - t);
      if (d < bd) { bd = d; best = j; }
    });
    if (best == null) return t;
    return which === "start" ? startAt(best) : endAt(best);
  }

  /** [ and ]: the start lands just before the word being spoken, the end just after it. */
  function snapPlayhead(which, t) {
    const j = wordAt(t);
    if (which === "start") {
      const k = st.words[j] && t - st.words[j].start < 0.35 ? j : j + 1;
      return st.words[k] && Math.abs(st.words[k].start - t) <= 0.6 ? startAt(k) : t;
    }
    return st.words[j] && t - st.words[j].end <= 0.6 ? endAt(j) : t;
  }

  function boundToPlayhead(which) {
    const c = clip();
    const t = snapPlayhead(which, video().currentTime);
    if (which === "start") {
      if (t >= c.end_sec - MIN_LEN) { toast("Плеєр стоїть після кінця кліпу. Перемісти його раніше.", true); return; }
      setBounds(t, c.end_sec);
    } else {
      if (t <= c.start_sec + MIN_LEN) { toast("Плеєр стоїть перед початком кліпу. Перемісти його пізніше.", true); return; }
      setBounds(c.start_sec, t);
    }
  }

  function trackTime(e) {
    const r = $("edTrack").getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    return st.zoom.lo + f * (st.zoom.hi - st.zoom.lo);
  }
  /**
   * Pointer on the track: a yellow edge drags the clip bound; anything else scrubs the video while you drag.
   * A word chip that is pressed and released without moving opens the word menu instead.
   */
  function onTrackDown(e) {
    if (e.button > 0) return;
    e.preventDefault();
    closePop();
    $("edTrack").setPointerCapture(e.pointerId);
    const h = e.target.closest(".hd");
    if (h) {
      video().pause();
      st.drag = { which: h.dataset.h };
      $("edTrack").classList.add("dragging");
      return;
    }
    const w = e.target.closest(".w");
    st.scrub = { x: e.clientX, word: w ? Number(w.dataset.j) : null, moved: !w, wasPlaying: !video().paused };
    if (!w) { video().pause(); seek(trackTime(e)); $("edTrack").classList.add("scrubbing"); }
  }
  function onTrackMove(e) {
    if (st.scrub) {
      if (!st.scrub.moved && Math.abs(e.clientX - st.scrub.x) < 5) return;
      if (!st.scrub.moved) { st.scrub.moved = true; video().pause(); $("edTrack").classList.add("scrubbing"); }
      seek(trackTime(e));
      return;
    }
    if (!st.drag) return;
    const c = clip();
    const t = trackTime(e);
    if (st.drag.which === "start") setBounds(Math.min(t, c.end_sec - MIN_LEN), c.end_sec, { save: false });
    else setBounds(c.start_sec, Math.max(t, c.start_sec + MIN_LEN), { save: false });
    seek(st.drag.which === "start" ? c.start_sec : Math.max(c.start_sec, c.end_sec - 1));
  }
  function onTrackUp(e) {
    $("edTrack").classList.remove("scrubbing", "dragging");
    if (st.scrub) {
      const s = st.scrub;
      st.scrub = null;
      if (!s.moved && s.word != null && e.type === "pointerup") openWordMenu(s.word, e.clientX, e.clientY);
      else if (s.wasPlaying) playClip();
      return;
    }
    if (!st.drag) return;
    const c = clip();
    const which = st.drag.which;
    st.drag = null;
    if (which === "start") setBounds(snap("start", c.start_sec), c.end_sec);
    else setBounds(c.start_sec, snap("end", c.end_sec));
  }

  function openWordMenu(j, x, y) {
    closePop();
    const w = st.words[j];
    const c0 = clip();
    const inClip = w.start >= c0.start_sec - 0.05 && w.start < c0.end_sec;
    const pop = document.createElement("div");
    pop.className = "ed-pop";
    pop.id = "edPop";
    pop.setAttribute("role", "menu");
    pop.innerHTML = `<div class="cap">«${esc(w.word)}» · ${fmt(w.start)}</div>
      <button class="ed-btn" type="button" role="menuitem" data-p="start">⇤ Почати з цього слова</button>
      <button class="ed-btn" type="button" role="menuitem" data-p="end">Закінчити цим словом ⇥</button>
      ${inClip ? `<button class="ed-btn ghost" type="button" role="menuitem" data-p="play">▶ Слухати звідси</button>` : `<div class="cap">Поза кліпом. Щоб почути, спершу посунь дужку.</div>`}`;
    pop.style.left = `${Math.max(8, Math.min(x, window.innerWidth - 240))}px`;
    pop.style.top = `${Math.min(y + 12, window.innerHeight - 170)}px`;
    pop.addEventListener("click", (ev) => {
      const a = ev.target.closest("[data-p]")?.dataset.p;
      if (!a) return;
      const c = clip();
      if (a === "start") {
        if (startAt(j) >= c.end_sec - MIN_LEN) toast("Це слово після кінця кліпу. Спершу посунь кінець.", true); else setBounds(startAt(j), c.end_sec);
      } else if (a === "end") {
        if (endAt(j) <= c.start_sec + MIN_LEN) toast("Це слово перед початком кліпу. Спершу посунь початок.", true); else setBounds(c.start_sec, endAt(j));
      } else {
        playClip(Math.max(c.start_sec, w.start - 0.05));
      }
      closePop();
    });
    $("editor").appendChild(pop);
    st.pop = pop;
    pop.querySelector("button").focus();
  }
  function closePop() { if (st.pop) { st.pop.remove(); st.pop = null; } }

  // ─── Post texts & status ───────────────────────────────────────────────────
  async function patch(fields, quiet = false, c = clip()) {
    try {
      const data = await api(`/api/projects/${pid()}/clips/${c.clip_index}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields),
      });
      for (const k of Object.keys(fields)) if (k in data.clip) c[k] = data.clip[k]; // only what we sent: a pending trim must not be undone
      if (!quiet) toast("Збережено");
    } catch (e) {
      toast("Не вдалося зберегти: " + e.message, true);
    }
  }

  async function mark(status) {
    const c = clip();
    const nextStatus = c.review_status === status ? "" : status;
    c.review_status = nextStatus;
    renderList(); renderOverview(); renderDetails();
    toast(nextStatus === "keep" ? "✓ Беру. Наступний кліп: ↓" : nextStatus === "drop" ? "✕ Не беру. Наступний кліп: ↓" : "Позначку знято, обери знову");
    // stay on this clip: the person moves on with ↓ or a click when ready
    await patch({ review_status: nextStatus }, true, c);
    renderList();
  }

  function step(dir) {
    const vis = stripClips();
    if (!vis.length) return;
    let pos = vis.indexOf(st.cur);
    pos = pos < 0 ? 0 : Math.max(0, Math.min(vis.length - 1, pos + dir));
    if (vis[pos] !== st.cur) select(vis[pos]);
  }

  function postText() { return `${$("edTitle").value}\n\n${$("edCaption").value}\n\n${$("edTags").value}`; }

  function copy(text) {
    const done = () => toast("Скопійовано");
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
    } else fallbackCopy(text, done);
  }
  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (_) { toast("Не вдалося скопіювати", true); }
    ta.remove();
  }

  function download(url) {
    const a = document.createElement("a");
    a.href = url; a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  }

  /** The step rail folds to a narrow strip of pictures. Remembered. */
  function setRail(mini, remember = false) {
    $("editor").classList.toggle("rail-mini", mini);
    $("edFold").setAttribute("aria-expanded", String(!mini));
    $("edFold").title = mini ? "Розгорнути меню" : "Згорнути меню";
    $("edFold").querySelector(".ed-fold-ic").textContent = mini ? "»" : "«";
    if (remember) { try { localStorage.setItem("studio-rail", mini ? "mini" : "full"); } catch (_) {} }
    if (st.project) { renderProgress(); renderOverview(); renderTrack(); updateScroll(); }
  }

  // ─── Keyboard ──────────────────────────────────────────────────────────────
  function onKey(e) {
    if ($("editor").hidden) return;
    if (e.key === "Escape") { closePop(); return; }
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest(".theme-menu")) return; // the theme drop-down handles its own keys
    const k = e.key;
    if (k === " ") { e.preventDefault(); togglePlay(); }
    else if (k === "ArrowDown" || k === "j" || k === "о") { e.preventDefault(); step(1); }
    else if (k === "ArrowUp" || k === "k" || k === "л") { e.preventDefault(); step(-1); }
    else if (k === "ArrowLeft") { e.preventDefault(); seek(video().currentTime - 2); }
    else if (k === "ArrowRight") { e.preventDefault(); seek(video().currentTime + 2); }
    else if (/^[yYнН]$/.test(k)) mark("keep");
    else if (/^[nNтТ]$/.test(k)) mark("drop");
    else if (k === "[" || k === "х") boundToPlayhead("start");
    else if (k === "]" || k === "ї") boundToPlayhead("end");
  }

  // ─── Open / close ──────────────────────────────────────────────────────────
  function open(project, clips) {
    st.project = project;
    st.clips = clips.map((c) => ({ ...c, review_status: c.review_status || "" }));
    assignNumbers();
    st.filter = "all";
    st.tab = "text";
    document.querySelectorAll("#edFilters button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === "all")));
    $("edProjTitle").textContent = project.title || "Відео";
    $("edProjMeta").textContent = `${fmtS(project.duration || 0)} · ${clips.length} кліпів`;
    const v = video();
    v.src = `/api/media?path=${encodeURIComponent(project.source_path)}`;
    v.load();
    $("editor").hidden = false;
    document.body.style.overflow = "hidden";
    renderTabs();
    let mode = "table";
    try { mode = localStorage.getItem("studio-mode") || "table"; } catch (_) {}
    setMode(mode);
    let mini = false;
    try { mini = localStorage.getItem("studio-rail") === "mini"; } catch (_) {}
    setRail(mini);
    let sort = "score";
    try { sort = localStorage.getItem("studio-sort") || "score"; } catch (_) {}
    setSort(sort);
    select(ordered()[0] ?? 0);
  }

  function close() {
    const v = video();
    v.pause();
    v.removeAttribute("src");
    v.load();
    closePop();
    $("editor").hidden = true;
    document.body.style.overflow = "";
  }

  function init() {
    if (typeof Vertical !== "undefined") {
      const keptCount = () => st.clips.filter((c) => c.review_status === "keep").length;
      Vertical.attach({ clip, pid, api, patch, toast, download, video, seek, togglePlay, postText, copy, keptCount });
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", (e) => { if (st.pop && !e.target.closest(".ed-pop")) closePop(); });
    window.addEventListener("resize", () => { if (!$("editor").hidden && st.project) { renderOverview(); renderTrack(); updateScroll(); } });
    $("edList").addEventListener("scroll", updateScroll, { passive: true });
    // Grab the strip with the mouse and drag it left or right. A real drag does not open the clip under the cursor.
    const L0 = $("edList");
    let drag = null;
    L0.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" || e.button !== 0) return;
      drag = { x: e.clientX, left: L0.scrollLeft, moved: false, id: e.pointerId };
    });
    L0.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x;
      if (!drag.moved && Math.abs(dx) < 6) return;
      if (!drag.moved) { drag.moved = true; L0.setPointerCapture(e.pointerId); L0.classList.add("dragging"); }
      L0.scrollLeft = drag.left - dx;
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const moved = drag.moved;
      drag = null;
      L0.classList.remove("dragging");
      if (moved) {
        const stop = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        window.addEventListener("click", stop, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", stop, { capture: true }), 0);
      }
    };
    L0.addEventListener("pointerup", endDrag);
    L0.addEventListener("pointercancel", endDrag);
    $("edList").addEventListener("wheel", (e) => { // a mouse wheel scrolls the strip sideways
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.currentTarget.scrollLeft += e.deltaY; e.preventDefault(); }
    }, { passive: false });
    $("edScrollPrev").addEventListener("click", () => { $("edList").scrollLeft -= $("edList").clientWidth * 0.8; });
    $("edScrollNext").addEventListener("click", () => { $("edList").scrollLeft += $("edList").clientWidth * 0.8; });
    $("edFold").addEventListener("click", () => setRail(!$("editor").classList.contains("rail-mini"), true));
    $("edGoNext").addEventListener("click", () => step(1));
    const T0 = $("edClipTitle");
    T0.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); T0.blur(); }
      else if (e.key === "Escape") { T0.textContent = clip()?.title || ""; T0.blur(); }
    });
    T0.addEventListener("blur", () => {
      const c = clip();
      if (!c) return;
      const val = T0.textContent.replace(/\s+/g, " ").trim();
      if (!val) { T0.textContent = c.title || ""; toast("Заголовок не може бути порожнім", true); return; }
      if (val === c.title) return;
      c.title = val; renderList(); renderDetails(); patch({ title: val });
    });
    const v = video();
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("play", onPlayState);
    v.addEventListener("pause", onPlayState);
    $("edScreen").addEventListener("click", togglePlay);
    $("edPlay").addEventListener("click", togglePlay);
    $("edKeep").addEventListener("click", () => mark("keep"));
    $("edDrop").addEventListener("click", () => mark("drop"));
    const T = $("edTrack");
    T.addEventListener("pointerdown", onTrackDown);
    T.addEventListener("pointermove", onTrackMove);
    T.addEventListener("pointerup", onTrackUp);
    T.addEventListener("pointercancel", onTrackUp);
    $("edOv").addEventListener("click", (e) => {
      const hit = e.target.closest("[data-i]");
      if (hit) { select(Number(hit.dataset.i)); return; }
      const r = e.currentTarget.getBoundingClientRect();
      const t = ((e.clientX - r.left) / r.width) * (st.project.duration || 0);
      let best = 0, bd = Infinity;
      st.clips.forEach((c, i) => { const d = Math.min(Math.abs(c.start_sec - t), Math.abs(c.end_sec - t)); if (d < bd) { bd = d; best = i; } });
      select(best);
    });
    document.querySelectorAll("#edFilters button").forEach((b) => b.addEventListener("click", () => {
      st.filter = b.dataset.f;
      document.querySelectorAll("#edFilters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      renderList();
    }));
    document.querySelectorAll("#edTabs button").forEach((b) => b.addEventListener("click", () => {
      st.tab = b.dataset.tab;
      renderTabs();
      if (st.tab === "text") renderWords();
    }));
    $("edTitle").addEventListener("change", (e) => {
      const val = e.target.value.trim();
      if (!val) { e.target.value = clip().title; toast("Заголовок не може бути порожнім", true); return; }
      clip().title = val; renderList(); renderDetails(); patch({ title: val });
    });
    $("edCaption").addEventListener("change", (e) => { clip().caption = e.target.value; patch({ caption: e.target.value }); });
    $("edTags").addEventListener("change", (e) => {
      const tags = e.target.value.split(/[\s,]+/).filter(Boolean).map((t) => (t.startsWith("#") ? t : "#" + t));
      e.target.value = tags.join(" ");
      clip().hashtags = tags; patch({ hashtags: tags });
    });
    $("edCopyTitle").addEventListener("click", () => copy($("edTitle").value));
    $("edCopyCaption").addEventListener("click", () => copy($("edCaption").value));
    $("edCopyTags").addEventListener("click", () => copy($("edTags").value));
    $("edCopyPost").addEventListener("click", () => copy(postText()));
    $("edMp4").addEventListener("click", () => { toast("Готуємо відео…"); download(`/api/projects/${pid()}/clips/${clip().clip_index}/download`); });
    $("edSrt").addEventListener("click", () => download(`/api/projects/${pid()}/clips/${clip().clip_index}/export-srt`));
    document.querySelectorAll("#edMode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.m)));
    $("vxKeep").addEventListener("click", () => mark("keep"));
    $("vxDrop").addEventListener("click", () => mark("drop"));
    document.querySelectorAll(".ed-change").forEach((b) => b.addEventListener("click", () => { const c = clip(); if (c && c.review_status) mark(c.review_status); }));
    document.querySelectorAll("#edSort button").forEach((b) => b.addEventListener("click", () => setSort(b.dataset.s)));
    $("edGoVx").addEventListener("click", () => setMode("vx"));
    $("vxMp4").addEventListener("click", () => $("edMp4").click());
    $("vxSrt").addEventListener("click", () => $("edSrt").click());
    $("vxCopyPost").addEventListener("click", () => copy(postText()));
    $("edBack").addEventListener("click", () => {
      close();
      if (typeof resetStudioState === "function") resetStudioState();
      if (typeof loadRecentProjects === "function") loadRecentProjects();
    });
    $("edReanalyze").addEventListener("click", () => {
      close();
      if (typeof retryAnalysisForCurrentProject === "function") retryAnalysisForCurrentProject();
    });
  }

  /** Read-only snapshot for UI tests and debugging. */
  function state() {
    const c = clip();
    return { cur: st.cur, clipIndex: c?.clip_index, num: c?.num, start: c?.start_sec, end: c?.end_sec, words: st.words.length, time: video().currentTime, paused: video().paused };
  }

  return { init, open, close, state, setMode };
})();

document.addEventListener("DOMContentLoaded", () => Editor.init());
