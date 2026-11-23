/**
 * editor.js — «Монтажний стіл»: step rail on the left (Крок 1 / Крок 2), player and side panel, clip strip at the bottom.
 * Watch a clip, set its status (Нове → В роботі → Виконано, or Не беру) from the status menu, move on.
 * The four status tiles at the top count the clips and choose which «shelf» the strip at the bottom shows. Right under the video sits the clip track:
 * the words laid out on time, draggable bracket edges, drag anywhere else to scrub, and a word menu
 * (start / end / listen). The playhead never leaves the clip: to hear more, move a bracket.
 * Clips are numbered by their place in the video (№1 = earliest), the same in the list and the strip.
 * Depends on app.js globals: resetStudioState, loadRecentProjects, retryAnalysisForCurrentProject,
 * escapeHtml; and on theme.js (StudioTheme).
 */
const Editor = (() => {
  const $ = (id) => document.getElementById(id);
  const st = {
    project: null, clips: [], cur: -1, filter: "new", anchor: 0, tab: "text", sort: "score", mode: "table",
    words: [], wordsFor: null, stopAt: null, backToStart: false, saveTimer: null,
    zoom: { lo: 0, hi: 1 }, zoomManual: false, levels: null, restored: [], drag: null, scrub: null, mapDrag: null, pop: null,
  };
  const MIN_LEN = 3;

  // ─── Helpers ───────────────────────────────────────────────────────────────
  const fmt = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); const x = s - m * 60; return `${m}:${x < 10 ? "0" : ""}${x.toFixed(1).replace(".", ",")}`; }; // Ukrainian decimal comma
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
  /** The score shown everywhere: the clip's place among this video's clips (99 best … 40 worst), not the raw AI number. */
  const scoreOf = (c) => (c.rank_score ?? c.virality_score ?? 0);
  const byScore = () => [...st.clips.keys()].sort((a, b) => scoreOf(st.clips[b]) - scoreOf(st.clips[a]) || st.clips[a].num - st.clips[b].num);
  const byTime = () => [...st.clips.keys()].sort((a, b) => st.clips[a].num - st.clips[b].num);
  const ordered = () => (st.sort === "time" ? byTime() : byScore());
  /** Clip statuses, like a todo list. '' = Нове, keep = В роботі, done = Виконано, drop = Не беру. */
  const STATUS = { "": "Нове", keep: "В роботі", done: "Виконано", drop: "Не беру" };
  const SHELVES = ["new", "keep", "done", "drop", "reserve"];
  /** «Запас»: weaker clips the AI found; they wait there so «Нові» shows only the best ones. */
  const shelfOf = (c) => c.review_status || (c.reserve ? "reserve" : "new");
  const TIER = { top: '<span class="ed-tier top" title="Серед найкращих кліпів цього відео">🔥 Топ</span>', good: '<span class="ed-tier good" title="Краще за половину кліпів цього відео">Добре</span>' };
  const matches = (c) => shelfOf(c) === st.filter;
  const kb = (n) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} КБ` : n < 1024 ** 3 ? `${Math.round(n / 1024 ** 2)} МБ` : `${(n / 1024 ** 3).toFixed(1).replace(".", ",")} ГБ`);
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
          <span class="ed-num">№${c.num}</span><span class="ed-dur">${Math.round(len(c))} с</span>${TIER[c.tier] || ""}${r === "keep" ? '<span class="ed-ok" title="Беру">✓</span>' : ""}</div>
        <div class="ed-rb"><div class="ed-rt">${esc(c.title)}</div>
        <div class="ed-rm"><span>${fmtS(c.start_sec)}${r === "drop" ? " · не беру" : ""}</span><span class="s" title="Місце серед кліпів цього відео (ШІ дав ${esc(c.virality_score)})">★ ${esc(scoreOf(c))}</span></div></div>`;
      li.addEventListener("click", () => select(i));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") select(i); });
      L.appendChild(li);
    });
    if (!vis.length) {
      const empty = { new: "Нових кліпів немає: усі розібрано.", keep: "У роботі поки нічого. Познач кліп «В роботі» в меню статусу.",
        done: "Виконаних ще немає.", drop: "Відкинутих кліпів немає.", reserve: "У запасі порожньо: усі кліпи на полиці «Нові»." };
      L.innerHTML = `<li class="ed-empty">${st.mode === "vx" ? "Тут поки порожньо." : empty[st.filter]}</li>`;
    }
    const anyKept = st.clips.some((c) => c.review_status === "keep");
    $("edStripTitle").textContent = st.mode === "vx" && anyKept ? "Кліпи в роботі" : "Кліпи";
    const note = document.querySelector("#editor .ed-vx-note");
    note.textContent = anyKept ? "Тут лише кліпи «В роботі». Щоб додати ще, повернись на крок 1." : "У роботі поки нічого: познач кліпи «В роботі» на кроці 1, тоді тут будуть лише вони.";

    SHELVES.forEach((f) => {
      $(`edCnt_${f}`).textContent = st.clips.filter((c) => shelfOf(c) === f).length;
      document.querySelector(`#edShelves [data-f="${f}"]`).setAttribute("aria-pressed", String(st.filter === f));
    });
    const cur = clip();
    $("edShelfNote").textContent = cur && !matches(cur) && st.mode !== "vx" ? `№${cur.num} тепер на полиці «${shelfName(shelfOf(cur))}»` : "";
    renderProgress();
    updateScroll();
  }

  /** Header bar and the step counters: how many clips are reviewed and kept. */
  function renderProgress() {
    const kept = st.clips.filter((c) => c.review_status === "keep").length;
    const seen = st.clips.filter((c) => c.review_status).length;
    $("edCntPick").textContent = `${kept} ${kept === 1 ? "кліп" : kept > 1 && kept < 5 ? "кліпи" : "кліпів"}`;
    const mini = $("editor").classList.contains("rail-mini");
    const allSeen = seen === st.clips.length && st.clips.length > 0;
    const b1 = document.querySelector('#edMode [data-m="table"]');
    b1.classList.toggle("done", allSeen && st.mode === "vx");
    $("edStep1").textContent = allSeen && st.mode === "vx" ? (mini ? `✓ ${kept}` : `✓ в роботі ${kept}`) : (mini ? `${seen}/${st.clips.length}` : `переглянуто ${seen}/${st.clips.length}`);
    $("edStep2").textContent = mini ? String(kept) : `в роботі ${kept}`;
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

  /** Clips the map shows: those of the open shelf, plus the open clip (it may have just left the shelf). */
  const mapClips = () => [...st.clips.keys()].filter((i) => matches(st.clips[i]) || i === st.cur);

  /** Thin map of the whole video under the shelf tabs: the clips of the open shelf, numbered by their place in the video. */
  function renderOverview() {
    const el = $("edOv");
    const dur = st.project.duration || 1;
    const W = el.clientWidth || 600;
    let h = "";
    let free = -Infinity; // where the last number ends: a number is shown only where it fits and does not overlap
    mapClips().sort((a, b) => st.clips[a].start_sec - st.clips[b].start_sec).forEach((i) => {
      const c = st.clips[i];
      const w = Math.max(0.4, (len(c) / dur) * 100);
      const x = (c.start_sec / dur) * W, px = (w / 100) * W;
      const need = String(c.num).length * 7.5 + 6;
      const fits = px >= need && x >= free;
      if (fits) free = x + px + 1;
      h += `<div class="c${c.review_status ? " " + c.review_status : c.reserve ? " reserve" : ""}${i === st.cur ? " sel" : ""}${fits ? "" : " slim"}" data-i="${i}"
        style="left:${(c.start_sec / dur) * 100}%;width:${w}%" title="№${c.num} · ${esc(c.title)} (${fmtS(c.start_sec)})">${fits ? `<span class="lab">${c.num}</span>` : ""}</div>`;
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
    $("edScore").innerHTML = c.virality_score != null ? `<span title="Місце серед кліпів цього відео: 99 найкращий, 40 найслабший. ШІ дав ${esc(c.virality_score)}">оцінка ШІ <b>★ ${esc(scoreOf(c))}</b></span>` : "";
    $("edPromote").hidden = !(c.reserve && !c.review_status);
    $("vxNo").textContent = `№${c.num} · ${Math.round(len(c))} с`;
    $("vxClipNo").textContent = c.title || "";
    ["edStatus", "vxStatus"].forEach((id) => {
      const b = $(id);
      b.dataset.s = c.review_status || "";
      b.querySelector(".ed-sdot").dataset.s = c.review_status || "";
      b.querySelector(".ed-sbtn-l").textContent = STATUS[c.review_status || ""];
    });
    $("edWhy").innerHTML = c.reason ? `<b>Чому:</b> ${esc(c.reason)}` : "";
    $("edStartTc").textContent = fmt(c.start_sec);
    $("edEndTc").textContent = fmt(c.end_sec);
    renderPauses();
    if (document.activeElement !== $("edTitle")) $("edTitle").value = c.title || "";
    if (document.activeElement !== $("edCaption")) $("edCaption").value = c.caption || "";
    if (document.activeElement !== $("edTags")) $("edTags").value = (c.hashtags || []).join(" ");

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
    if (vx) {
      // step 2 works on the clips «В роботі»: open the first of them unless one is open already
      const list = st.project ? stripClips() : [];
      const c = clip();
      if (list.length && (!c || c.review_status !== "keep") && st.clips[list[0]].review_status === "keep") select(list[0]);
      if (typeof Vertical !== "undefined") Vertical.onShow();
    }
    else if (st.project && clip()) { renderOverview(); renderTrack(); }
  }

  // ─── Trim track ────────────────────────────────────────────────────────────
  /** The track never goes further than this from the clip: no endless grey (dragging a yellow edge moves it along). */
  const ROOM = 10;
  function computeZoom() {
    const c = clip();
    return { lo: Math.max(0, c.start_sec - ROOM), hi: Math.min(st.project.duration || c.end_sec + ROOM, c.end_sec + ROOM) };
  }
  /** What the track shows when a clip opens (and on ⤢): the clip with a little room, so the scrollbar shows there is more. */
  function defaultView() {
    const c = clip(), R = computeZoom();
    const pad = Math.min(ROOM, Math.max(4, len(c) * 0.25));
    return { lo: Math.max(R.lo, c.start_sec - pad), hi: Math.min(R.hi, c.end_sec + pad) };
  }

  /** Splits the words into phrases at pauses and sentence ends (a phrase stays under ~6 s, a short sentence joins the next), as [first, last] word indexes. */
  function phrasesOf(words) {
    const out = [];
    let j0 = 0;
    for (let j = 1; j <= words.length; j++) {
      const w = words[j], prev = words[j - 1];
      const brk = !w || w.start - prev.end > 0.5 || w.start - words[j0].start > 6
        || (/[.!?…]$/.test(prev.word.trim()) && prev.end - words[j0].start > 1.5);
      if (brk) { out.push([j0, j - 1]); j0 = j; }
    }
    return words.length ? out : [];
  }

  function renderTrack() {
    const c = clip();
    if (!c) return;
    if (!st.drag && !st.zoomManual) st.zoom = defaultView();
    const { lo, hi } = st.zoom;
    const T = $("edTrack");
    const W = T.clientWidth || 600;
    const p = (t) => ((t - lo) / (hi - lo)) * 100;
    let h = `<canvas class="wave" aria-hidden="true"></canvas>`;
    // phrase cards (split at pauses), each laid out on time: the text, then its start time; every word is clickable
    phrasesOf(st.words).forEach(([j0, j1]) => {
      const a = st.words[j0].start, b = st.words[j1].end;
      if (b < lo || a > hi) return;
      const left = p(a), width = Math.max(0.4, p(b) - left);
      const px = (width / 100) * W;
      const inside = a < c.end_sec && b > c.start_sec;
      let inner = "";
      // the first word must fit whole (≈7.5 px a letter + padding); otherwise «…» instead of a chopped word
      const firstFits = px >= 40 && st.words[j0].word.trim().length * 7.5 + 18 <= px;
      if (firstFits) {
        for (let j = j0; j <= j1; j++) inner += `<span class="w" data-j="${j}">${esc(st.words[j].word)}</span> `;
        inner = `<div class="tx">${inner}</div>` + (px >= 56 ? `<small>${fmt(a)}</small>` : "");
      } else if (px >= 26) {
        inner = `<div class="tx dots">…</div>`;
      }
      h += `<div class="phr${inside ? " in" : ""}${!firstFits ? (px < 26 ? " tiny" : " more") : px < 56 ? " notime" : ""}" data-a="${a}" data-b="${b}"
        style="left:${left}%;width:calc(${width}% - 3px)" title="${fmt(a)} – ${fmt(b)}">${inner}</div>`;
    });
    // a cut pause: a small square with ✂ between the phrases, and a hatched band on the wave
    const gapMark = (a, b, cls, data, title) => {
      const mid = (p(a) + p(b)) / 2, wpx = Math.max(18, ((p(b) - p(a)) / 100) * W - 4);
      return `<div class="cut${cls}" ${data} style="left:calc(${mid}% - ${wpx / 2}px);width:${wpx}px" title="${title}">✂</div>`
        + `<div class="cutw" style="left:${p(a)}%;width:${p(b) - p(a)}%"></div>`;
    };
    cutsOf(c).forEach(([a, b], k) => {
      if (b < lo || a > hi) return;
      h += gapMark(a, b, "", `data-k="${k}"`, `Пауза ${fmt1(b - a + 0.2)} с вирізана. Клік: повернути`);
    });
    st.restored.forEach(([a, b], k) => {
      if (b < lo || a > hi || a < c.start_sec || b > c.end_sec) return;
      h += gapMark(a, b, " ghost", `data-r="${k}"`, "Пауза повернута. Клік: вирізати знову").replace('<div class="cutw"', '<div class="cutw" hidden');
    });
    h += `<div class="shade" style="left:0;width:${p(c.start_sec)}%"></div><div class="shade" style="left:${p(c.end_sec)}%;right:0"></div>`;
    h += `<div class="hd" data-h="start" style="left:${p(c.start_sec)}%" title="Тягни: початок кліпу"><em>${fmt(c.start_sec)}</em></div>`;
    h += `<div class="hd" data-h="end" style="left:${p(c.end_sec)}%" title="Тягни: кінець кліпу"><em>${fmt(c.end_sec)}</em></div>`;
    h += `<div class="zph" id="edZph"></div>`;
    hidePhraseCard(true);
    T.innerHTML = h;
    // a phrase whose text does not fit gets the full text in a card above the track on hover
    T.querySelectorAll(".phr .tx").forEach((el) => {
      if (el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1) el.parentElement.classList.add("more");
    });
    renderMap();
    drawWave();
    renderRuler();
    placePlayhead(video().currentTime || 0);
  }

  // ─── Moving around the track: wheel scrolls, Ctrl+wheel and − + zoom, the map above shows where you are ───
  const MIN_SPAN = 3;
  /** What the map above the track shows: the whole clip with some room around it (and the view, if it is wider). */
  function mapRange() {
    const d = computeZoom();
    return { lo: Math.min(d.lo, st.zoom.lo), hi: Math.max(d.hi, st.zoom.hi) };  // wider only while an edge is dragged out
  }
  function setZoom(lo, span) {
    const d = computeZoom();
    span = Math.max(MIN_SPAN, Math.min(d.hi - d.lo, span));
    lo = Math.max(d.lo, Math.min(d.hi - span, lo));
    st.zoom = { lo, hi: lo + span };
    const v = defaultView();
    st.zoomManual = Math.abs(lo - v.lo) > 0.01 || Math.abs(lo + span - v.hi) > 0.01;
    renderTrack();
  }
  function resetZoom() { st.zoomManual = false; renderTrack(); }
  /** Zoom keeps the white playhead in place (or the middle, when the playhead is out of view). */
  function zoomBy(f) {
    const { lo, hi } = st.zoom;
    const t = video().currentTime || 0;
    const a = t >= lo && t <= hi ? t : (lo + hi) / 2;
    const dur = st.project.duration || hi;
    const span = Math.max(MIN_SPAN, Math.min(dur, (hi - lo) * f));
    setZoom(a - (a - lo) * (span / (hi - lo)), span);
  }
  function panBy(dt) { setZoom(st.zoom.lo + dt, st.zoom.hi - st.zoom.lo); }

  /** Zoom slider: left = the whole clip, right = 3 seconds (log scale, so each step feels the same). */
  const zoomFull = () => computeZoom().hi - computeZoom().lo;
  function spanToSlider(span) {
    const full = zoomFull();
    if (full <= MIN_SPAN) return 0;
    return Math.round(100 * Math.max(0, Math.min(1, Math.log(full / span) / Math.log(full / MIN_SPAN))));
  }
  function sliderToSpan(v) { const full = zoomFull(); return full * Math.pow(MIN_SPAN / full, v / 100); }

  function renderMap() {
    const M = $("edMap");
    const c = clip();
    if (!M || !c) return;
    const { lo, hi } = mapRange();
    const p = (t) => ((t - lo) / (hi - lo)) * 100;
    if (!M.querySelector("canvas")) M.innerHTML = `<canvas aria-hidden="true"></canvas><div class="clip"></div><div class="win" title="Тягни: рухати доріжку. Тягни краї: масштаб"></div><div class="ph"></div>`;
    const cl = M.querySelector(".clip"), win = M.querySelector(".win");
    cl.style.left = `${p(c.start_sec)}%`; cl.style.width = `${p(c.end_sec) - p(c.start_sec)}%`;
    win.style.left = `${p(st.zoom.lo)}%`; win.style.width = `${p(st.zoom.hi) - p(st.zoom.lo)}%`;
    M.classList.toggle("zoomed", st.zoomManual);
    const sl = $("edZoomRange");
    if (sl) sl.value = spanToSlider(st.zoom.hi - st.zoom.lo);
    const all = $("edZoomAll");
    if (all) all.disabled = !st.zoomManual;
  }

  function onMapDown(e) {
    if (e.button > 0 || !clip()) return;
    e.preventDefault();
    const M = $("edMap");
    const r = M.getBoundingClientRect();
    const { lo, hi } = mapRange();
    const tAt = (x) => lo + ((x - r.left) / r.width) * (hi - lo);
    const win = M.querySelector(".win").getBoundingClientRect();
    const edge = 8;
    let mode = "move";
    if (Math.abs(e.clientX - win.left) <= edge) mode = "left";
    else if (Math.abs(e.clientX - win.right) <= edge) mode = "right";
    else if (e.clientX < win.left || e.clientX > win.right) { // jump: centre the view where you clicked
      const span = st.zoom.hi - st.zoom.lo;
      setZoom(tAt(e.clientX) - span / 2, span);
    }
    M.setPointerCapture(e.pointerId);
    st.mapDrag = { mode, x: e.clientX, z: { ...st.zoom }, tAt, range: { lo, hi } };
  }
  function onMapMove(e) {
    const d = st.mapDrag;
    if (!d) return;
    const r = $("edMap").getBoundingClientRect();
    const dt = ((e.clientX - d.x) / r.width) * (d.range.hi - d.range.lo);
    if (d.mode === "move") setZoom(d.z.lo + dt, d.z.hi - d.z.lo);
    else if (d.mode === "left") { const nlo = Math.min(d.z.hi - MIN_SPAN, d.z.lo + dt); setZoom(nlo, d.z.hi - nlo); }
    else { const nhi = Math.max(d.z.lo + MIN_SPAN, d.z.hi + dt); setZoom(d.z.lo, nhi - d.z.lo); }
  }
  function onMapUp() { st.mapDrag = null; }

  /** While playing a zoomed-in track, it turns the page when the playhead reaches the right edge. */
  function followPlayhead(t) {
    if (!st.zoomManual || st.drag || st.scrub || st.mapDrag || video().paused) return;
    const { lo, hi } = st.zoom;
    const span = hi - lo;
    if (t > hi - span * 0.04 || t < lo) setZoom(t - span * 0.1, span);
  }

  /** Dragging a yellow edge to the side of a zoomed-in track scrolls it further. */
  function edgeScroll() {
    if (!st.drag || !st.drag.dir) { if (st.drag) st.drag.raf = null; return; }
    const span = st.zoom.hi - st.zoom.lo;
    const before = st.zoom.lo;
    st.zoomManual = true;
    const R = computeZoom();  // follows the dragged edge, so the clip can still grow
    const lo = Math.max(R.lo, Math.min(R.hi - span, before + st.drag.dir * span * 0.012));
    st.zoom = { lo, hi: lo + span };
    if (lo !== before) moveBound(st.drag.lastX);
    renderTrack();
    st.drag.raf = requestAnimationFrame(edgeScroll);
  }

  /** Time ruler above the track: a tick every second (or more when zoomed out), labels every 5 s or more. */
  function renderRuler() {
    const { lo, hi } = st.zoom;
    const span = hi - lo;
    const [minor, major] = span <= 70 ? [1, 5] : span <= 200 ? [5, 30] : [10, 60];
    const p = (t) => ((t - lo) / span) * 100;
    let h = "";
    for (let t = Math.ceil(lo / minor) * minor; t <= hi; t += minor) {
      const big = t % major === 0;
      h += `<i class="${big ? "big" : ""}" style="left:${p(t)}%"></i>`;
      if (big) h += `<b style="left:${p(t)}%"${p(t) < 3 ? ' class="l"' : p(t) > 97 ? ' class="r"' : ""}>${fmtS(t)}</b>`;
    }
    $("edRuler").innerHTML = h;
  }

  /** Sound wave behind the words: where people talk, laugh and where it is quiet. */
  function drawWave() {
    drawLevels($("edTrack").querySelector("canvas.wave"), st.zoom.lo, st.zoom.hi);
    drawLevels($("edMap").querySelector("canvas"), mapRange().lo, mapRange().hi);
  }
  function drawLevels(cv, lo, hi) {
    const L = st.levels;
    if (!cv || !L || !L.db.length) return;
    const c = clip();
    const W = cv.clientWidth, H = cv.clientHeight;
    if (!W || !H) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    const css = getComputedStyle($("editor"));
    const colIn = css.getPropertyValue("--ed-accent").trim() || "#d4a944";
    const colOut = css.getPropertyValue("--ed-faint").trim() || "#6b665e";
    const floor = L.floor, top = L.top;
    const step = 3;
    for (let x = 0; x < W; x += step) {
      const t0 = lo + (x / W) * (hi - lo), t1 = lo + ((x + step) / W) * (hi - lo);
      const i0 = Math.floor((t0 - L.start) * L.rate), i1 = Math.max(i0 + 1, Math.ceil((t1 - L.start) * L.rate));
      if (i1 <= 0 || i0 >= L.db.length) continue;
      let m = -120;
      for (let i = Math.max(0, i0); i < Math.min(L.db.length, i1); i++) if (L.db[i] > m) m = L.db[i];
      const a = Math.max(0, Math.min(1, (m - floor) / (top - floor || 1)));
      const bh = Math.max(1.5, a * (H - 2));
      const tm = (t0 + t1) / 2;
      const inClip = tm >= c.start_sec && tm <= c.end_sec;
      g.globalAlpha = inClip ? 0.7 : 0.45;
      g.fillStyle = inClip ? colIn : colOut;
      g.fillRect(x, (H - bh) / 2, step - 1, bh);
    }
  }

  async function loadLevels() {
    const c = clip();
    if (!c) return;
    const key = `${c.clip_index}:${c.start_sec}:${c.end_sec}`;
    if (st.levels && st.levels.key === key) return;
    try {
      const d = await api(`/api/projects/${pid()}/clips/${c.clip_index}/levels?pad=30`);
      if (clip() !== c || !d.db || !d.db.length) return;
      const sorted = [...d.db].sort((a, b) => a - b);
      const q = (f) => sorted[Math.min(sorted.length - 1, Math.floor(f * sorted.length))];
      st.levels = { key, start: d.start, rate: d.rate, db: d.db, floor: q(0.1) - 3, top: q(0.97) };
      drawWave();
    } catch (_) { /* no sound wave: words still work */ }
  }

  function placePlayhead(t) {
    const mp = $("edMap")?.querySelector(".ph");
    if (mp) { const { lo, hi } = mapRange(); mp.style.left = `${((t - lo) / (hi - lo)) * 100}%`; }
    const ph = $("edPh");
    if (ph) ph.style.left = `${(t / (st.project.duration || 1)) * 100}%`;
    const T = $("edTrack");
    if (T) T.querySelectorAll(".phr").forEach((el) => el.classList.toggle("now", t >= Number(el.dataset.a) - 0.05 && t < Number(el.dataset.b) + 0.15));
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
    st.zoomManual = false;
    st.restored = [];
    st.levels = null;
    if (st.mode !== "vx" && !matches(st.clips[i])) st.filter = shelfOf(st.clips[i]); // a clip picked on the map opens its own shelf
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
    loadLevels();
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
    skipCuts();
    followPlayhead(t);
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
    if (!paused) requestAnimationFrame(skipLoop); else restoreVolume();
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
    const cutEl = e.target.closest(".cut");
    if (cutEl) {
      const c = clip();
      const cuts = cutsOf(c);
      if (cutEl.dataset.k != null) {
        const k = Number(cutEl.dataset.k);
        st.restored.push(cuts[k]);
        savePauses(c, cuts.filter((_, i) => i !== k), "Паузу повернуто");
      } else {
        const r = st.restored.splice(Number(cutEl.dataset.r), 1)[0];
        savePauses(c, [...cuts, r].sort((a, b) => a[0] - b[0]), "Паузу знову вирізано");
      }
      return;
    }
    closePop();
    hidePhraseCard(true);
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
    moveBound(e.clientX);
    const r = $("edTrack").getBoundingClientRect();
    const dir = e.clientX > r.right - 24 ? 1 : e.clientX < r.left + 24 ? -1 : 0;
    st.drag.dir = dir;
    if (dir && !st.drag.raf) st.drag.raf = requestAnimationFrame(edgeScroll);
  }
  function moveBound(x) {
    const c = clip();
    if (!st.drag || !c) return;
    st.drag.lastX = x;
    const t = trackTime({ clientX: x });
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
    if (st.drag.raf) cancelAnimationFrame(st.drag.raf);
    st.drag = null;
    if (which === "start") setBounds(snap("start", c.start_sec), c.end_sec);
    else setBounds(c.start_sec, snap("end", c.end_sec));
  }

  // ─── Card with the full phrase above the track (for phrases too narrow for their text) ───
  function phraseRange(el) {
    const a = Number(el.dataset.a), b = Number(el.dataset.b);
    let j0 = -1, j1 = -1;
    st.words.forEach((w, j) => { if (w.start >= a - 0.001 && w.end <= b + 0.001) { if (j0 < 0) j0 = j; j1 = j; } });
    return [j0, j1];
  }
  function showPhraseCard(el) {
    if (st.scrub || st.drag || st.pop) return;
    clearTimeout(st.cardHide);
    if (st.card && st.card.phr === el) return;
    hidePhraseCard(true);
    const [j0, j1] = phraseRange(el);
    if (j0 < 0) return;
    const c = clip();
    let inner = "";
    for (let j = j0; j <= j1; j++) {
      const w = st.words[j];
      const wIn = w.start >= c.start_sec - 0.05 && w.start < c.end_sec;
      inner += `<span class="w${wIn ? "" : " out"}" data-j="${j}">${esc(w.word)}</span> `;
    }
    const card = document.createElement("div");
    card.className = "ed-phrcard";
    card.innerHTML = `<small>${fmt(st.words[j0].start)} – ${fmt(st.words[j1].end)} · клік по слову</small>${inner}`;
    $("editor").appendChild(card);
    const tr = $("edTrack").getBoundingClientRect(), pr = el.getBoundingClientRect();
    const cw = Math.min(card.offsetWidth, tr.width);
    card.style.maxWidth = `${Math.min(440, tr.width)}px`;
    card.style.left = `${Math.max(tr.left, Math.min(pr.left, tr.right - cw))}px`;
    card.style.top = `${Math.max(4, tr.top - card.offsetHeight - 6)}px`;
    card.addEventListener("mouseenter", () => clearTimeout(st.cardHide));
    card.addEventListener("mouseleave", () => hidePhraseCard());
    card.addEventListener("click", (ev) => {
      const w = ev.target.closest(".w");
      if (!w) return;
      hidePhraseCard(true);
      openWordMenu(Number(w.dataset.j), ev.clientX, ev.clientY);
    });
    st.card = { el: card, phr: el };
  }
  function hidePhraseCard(now = false) {
    clearTimeout(st.cardHide);
    const go = () => { if (st.card) { st.card.el.remove(); st.card = null; } };
    if (now) go(); else st.cardHide = setTimeout(go, 160);
  }
  function onTrackOver(e) {
    const el = e.target.closest(".phr");
    if (el && (el.classList.contains("more") || el.classList.contains("tiny"))) showPhraseCard(el);
    else if (st.card) hidePhraseCard();
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

  const shelfName = (f) => ({ new: "Нові", keep: "В роботі", done: "Виконані", drop: "Не беру", reserve: "Запас" })[f];

  /** «⬆ Перенести в Нові»: a clip from «Запас» joins the main shelf. */
  async function promote(c = clip()) {
    closeStatusMenu();
    if (!c || !c.reserve) return;
    const vis = stripClips();
    if (c === clip() && vis.includes(st.cur)) st.anchor = vis.indexOf(st.cur);
    c.reserve = 0;
    renderList(); renderOverview(); renderDetails();
    try {
      await api(`/api/projects/${pid()}/clips/${c.clip_index}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reserve: false }),
      });
      toast("⬆ Перенесено в «Нові»");
    } catch (e) {
      c.reserve = 1;
      renderList(); renderOverview(); renderDetails();
      toast("Не вдалося перенести: " + e.message, true);
    }
  }

  /**
   * Sets the clip status. A clip that leaves the open shelf makes way: the next clip of that shelf opens. «Виконано» and «Не беру» delete the clip's
   * rendered files on the server; its bounds, text and style stay, so «Скачати» makes them again.
   */
  async function mark(status, c = clip(), { undo = true } = {}) {
    closeStatusMenu();
    if (!c) return;
    const prev = c.review_status || "";
    if (prev === status) return;
    const vis = stripClips();
    if (c === clip() && vis.includes(st.cur)) st.anchor = vis.indexOf(st.cur);
    const prevReserve = c.reserve;
    c.review_status = status;
    if (status) c.reserve = 0; // a reviewed clip leaves «Запас» (the server does the same)
    renderList(); renderOverview(); renderDetails();
    const left = c === clip() && st.mode !== "vx" && !matches(c);
    if (left) { const rest = stripClips(); if (rest.length) select(rest[Math.min(st.anchor, rest.length - 1)]); }
    try {
      const data = await api(`/api/projects/${pid()}/clips/${c.clip_index}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_status: status }),
      });
      const freed = data.freed_bytes || 0;
      const msg = { "": "Знову «Нове»", keep: "✓ В роботі", done: "✓ Виконано", drop: "✕ Не беру" }[status]
        + (freed ? `. Звільнено ${kb(freed)}` : "");
      const back = () => { const i = st.clips.indexOf(c); mark(prev, c, { undo: false }); if (st.mode !== "vx" && i >= 0) select(i); };
      if (undo && prev !== status) toastUndo(left ? `${msg} · №${c.num} → «${shelfName(shelfOf(c))}»` : msg, back);
      else toast(msg);
    } catch (e) {
      c.review_status = prev;
      c.reserve = prevReserve;
      renderList(); renderOverview(); renderDetails();
      toast("Не вдалося змінити статус: " + e.message, true);
    }
    renderList();
  }

  function toastUndo(msg, onUndo) {
    const t = $("edToast");
    t.innerHTML = `<span></span><button type="button" class="ed-toast-undo">↶ Повернути</button>`;
    t.firstChild.textContent = msg;
    t.classList.remove("err");
    t.classList.add("on", "act");
    t.querySelector("button").addEventListener("click", () => { t.classList.remove("on", "act"); onUndo(); }, { once: true });
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("on", "act"), 5000);
  }

  // ─── Status menu (one drop-down shared by step 1 and step 2) ──────────────
  function openStatusMenu(btn) {
    const m = $("edStatusMenu");
    const c = clip();
    if (!c) return;
    m.querySelectorAll("[data-s]").forEach((b) => { if (b.tagName === "BUTTON") b.setAttribute("aria-checked", String(b.dataset.s === (c.review_status || ""))); });
    m.hidden = false;
    const r = btn.getBoundingClientRect();
    const h = m.offsetHeight, w = m.offsetWidth;
    const top = r.top - h - 8 > 8 ? r.top - h - 8 : Math.min(window.innerHeight - h - 8, r.bottom + 8);
    m.style.top = `${Math.max(8, top)}px`;
    m.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w))}px`;
    btn.setAttribute("aria-expanded", "true");
    st.menuBtn = btn;
    (m.querySelector('[aria-checked="true"]') || m.querySelector("button")).focus();
  }
  function closeStatusMenu() {
    const m = $("edStatusMenu");
    if (!m || m.hidden) return;
    m.hidden = true;
    if (st.menuBtn) { st.menuBtn.setAttribute("aria-expanded", "false"); st.menuBtn = null; }
  }

  // ─── «Прибрати паузи» (jump cut) ───────────────────────────────────────────
  const fmt1 = (x) => (Math.round(x * 10) / 10).toFixed(1).replace(".", ",");
  /** The clip's cut pauses that still lie inside its bounds. */
  const cutsOf = (c) => (c?.cuts || []).filter(([a, b]) => a >= c.start_sec && b <= c.end_sec && b > a);
  const removedOf = (c) => cutsOf(c).reduce((s, [a, b]) => s + (b - a), 0);
  /** How hard «Прибрати паузи» cuts: which pauses count and how much silence each one keeps. */
  const PAUSE_MODES = {
    calm: { label: "Спокійний", gap: 1.0, keep: 0.3, hint: "лише паузи довші за 1 с, лишає 0,3 с тиші: звучить природно" },
    normal: { label: "Звичайний", gap: 0.6, keep: 0.2, hint: "паузи довші за 0,6 с, лишає 0,2 с тиші" },
    dense: { label: "Щільний", gap: 0.4, keep: 0.1, hint: "паузи довші за 0,4 с, лишає 0,1 с тиші: швидкий темп, як у TikTok" },
  };
  function pauseMode() {
    let m = null;
    try { m = localStorage.getItem("studio-pause-mode"); } catch (_) {}
    return PAUSE_MODES[m] ? m : "normal";
  }

  function renderPauses() {
    const c = clip();
    if (!c) return;
    const cuts = cutsOf(c);
    const rm = removedOf(c);
    $("edLen").textContent = cuts.length ? `${Math.round(len(c))} с → ${fmt1(len(c) - rm)} с` : `${Math.round(len(c))} с`;
    const btn = $("edPauses");
    btn.classList.toggle("on", cuts.length > 0);
    btn.textContent = cuts.length ? `✓ Паузи прибрано · −${fmt1(rm)} с` : "✂ Прибрати паузи";
    const bar = $("edPauseBar");
    const cut = cuts.length > 0 || st.restored.length > 0;
    const mode = pauseMode();
    bar.hidden = false;
    const pv = st.pausePreview && st.pausePreview.key === previewKey(c) ? st.pausePreview.tempos : null;
    if (!pv) loadPausePreview(c);
    const none = !cut && pv && Object.values(pv).every((r) => !r.count);
    btn.disabled = !!none || cuts.length > 0; // once cut, the button only shows the result; «↶ Повернути все» undoes it
    btn.classList.toggle("none", !!none);
    btn.title = none ? "У цьому кліпі довгих пауз немає" : "Вирізати довгі паузи всередині кліпу (jump cut). Чистий оригінал лишається для скачування";
    if (none) { bar.innerHTML = '<span class="hint">Довгих пауз немає: кліп і так іде щільно.</span>'; }
    else bar.innerHTML = `<span>Темп:</span><span class="seg" role="group" aria-label="Темп: наскільки різати паузи">${Object.entries(PAUSE_MODES).map(([k, m]) => {
        const r = pv && pv[k];
        const res = r ? (r.count ? ` · −${fmt1(r.removed_sec)} с` : " · 0 с") : "";
        return `<button type="button" data-m="${k}" aria-pressed="${k === mode}" title="${m.label}: ${m.hint}">${m.label}${res}</button>`;
      }).join("")}</span>`
      + (cut
        ? `<span class="res">прибрано <b>${cuts.length} ${cuts.length === 1 ? "паузу" : cuts.length > 1 && cuts.length < 5 ? "паузи" : "пауз"} · −${fmt1(rm)} с</b></span>
           <span class="hint">· клік по ✂ повертає паузу</span><button type="button" class="link" data-a="all">↶ Повернути все</button>`
        : `<span class="hint">${PAUSE_MODES[mode].hint}</span>`);
    $("edMp4Lab").textContent = cuts.length ? "⬇ Кліп 16:9 · без пауз" : "⬇ Кліп 16:9 · MP4";
    $("edMp4Info").innerHTML = cuts.length ? `${fmt1(len(c) - rm)} с` : `${Math.round(len(c))} с<span class="q"> · оригінальна якість</span>`;
    $("edMp4Raw").hidden = !cuts.length;
  }

  /** What each tempo would cut in this clip (one request, nothing saved): shown on the tempo buttons. */
  const previewKey = (c) => `${pid()}:${c.clip_index}:${c.start_sec}:${c.end_sec}`;
  async function loadPausePreview(c) {
    const key = previewKey(c);
    if (st.pausePreviewLoading === key) return;
    st.pausePreviewLoading = key;
    clearTimeout(st.pausePreviewTimer);
    st.pausePreviewTimer = setTimeout(async () => {
      try {
        const d = await api(`/api/projects/${pid()}/clips/${c.clip_index}/pauses/preview`);
        st.pausePreview = { key, tempos: d.tempos };
        if (clip() === c && previewKey(c) === key) renderPauses();
      } catch (_) { /* the buttons just show no numbers */ }
      finally { if (st.pausePreviewLoading === key) st.pausePreviewLoading = null; }
    }, 400);
  }

  async function savePauses(c, cuts, msg) {
    const prev = c.cuts;
    c.cuts = cuts;
    if (clip() === c) { renderTrack(); renderPauses(); }
    try {
      const d = await api(`/api/projects/${pid()}/clips/${c.clip_index}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cuts }),
      });
      c.cuts = d.clip.cuts;
      if (msg) toast(msg);
      if (typeof Vertical !== "undefined" && clip() === c) Vertical.onClip();
    } catch (e) {
      c.cuts = prev;
      toast("Не вдалося зберегти паузи: " + e.message, true);
    }
    if (clip() === c) { renderTrack(); renderPauses(); }
  }

  async function findPauses(mode = pauseMode()) {
    const { gap, keep } = PAUSE_MODES[mode];
    const c = clip();
    if (!c) return;
    const btn = $("edPauses");
    btn.disabled = true;
    btn.textContent = "Шукаю паузи…";
    try {
      const d = await api(`/api/projects/${pid()}/clips/${c.clip_index}/pauses`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ min_gap: gap, keep }),
      });
      c.cuts = d.cuts;
      st.restored = [];
      toast(d.count ? `✂ Прибрано ${d.count} ${d.count === 1 ? "паузу" : d.count < 5 ? "паузи" : "пауз"} · −${fmt1(d.removed_sec)} с` : `Пауз, довших за ${fmt1(gap)} с, немає`);
      if (typeof Vertical !== "undefined" && clip() === c) Vertical.onClip();
    } catch (e) {
      toast("Не вдалося знайти паузи: " + e.message, true);
    } finally {
      btn.disabled = false;
      if (clip() === c) { renderTrack(); renderPauses(); }
    }
  }

  /** While playing, jump over the cut pauses, so you hear the clip as it will be. */
  const FADE = 0.08;
  function skipCuts() {
    const v = video();
    const c = clip();
    if (!c || v.paused || st.drag || st.scrub) { restoreVolume(); return; }
    const t = v.currentTime;
    const cuts = cutsOf(c);
    const cut = cuts.find(([a, b]) => t >= a && t < b - 0.03);
    if (cut) {
      try { v.currentTime = cut[1]; } catch (_) {}
      v.classList.remove("cutdip"); void v.offsetWidth; v.classList.add("cutdip");
      st.fadeIn = performance.now();
      setFade(0);
      return;
    }
    // volume goes down just before a cut and comes back up right after it (≈80 ms each way)
    const ahead = cuts.find(([a]) => a > t && a - t < FADE);
    let g = ahead ? (ahead[0] - t) / FADE : 1;
    if (st.fadeIn != null) {
      const k = (performance.now() - st.fadeIn) / (FADE * 1000);
      if (k >= 1) st.fadeIn = null; else g = Math.min(g, k);
    }
    setFade(g);
  }
  /** Scales the volume the person chose, without losing it. */
  function setFade(g) {
    const v = video();
    if (g >= 1) { restoreVolume(); return; }
    if (st.userVol == null) st.userVol = v.volume;
    v.volume = Math.max(0, Math.min(1, st.userVol * g));
  }
  function restoreVolume() {
    if (st.userVol == null) return;
    video().volume = st.userVol;
    st.userVol = null;
    st.fadeIn = null;
  }
  function skipLoop() { skipCuts(); if (!video().paused) requestAnimationFrame(skipLoop); }

  function step(dir) {
    const vis = stripClips();
    if (!vis.length) return;
    let pos = vis.indexOf(st.cur);
    // the open clip may have just left this shelf: carry on from the place where it was
    if (pos < 0) pos = Math.max(0, Math.min(vis.length - 1, dir > 0 ? st.anchor : st.anchor - 1));
    else pos = Math.max(0, Math.min(vis.length - 1, pos + dir));
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
    if (e.key === "Escape") { closePop(); closeStatusMenu(); return; }
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest(".theme-menu")) return; // the theme drop-down handles its own keys
    const k = e.key;
    if (k === " ") { e.preventDefault(); togglePlay(); }
    else if (k === "ArrowDown" || k === "j" || k === "о") { e.preventDefault(); step(1); }
    else if (k === "ArrowUp" || k === "k" || k === "л") { e.preventDefault(); step(-1); }
    else if (k === "ArrowLeft") { e.preventDefault(); seek(video().currentTime - 2); }
    else if (k === "ArrowRight") { e.preventDefault(); seek(video().currentTime + 2); }
    else if (/^[yYнН]$/.test(k)) mark("keep");
    else if (/^[uUгГ]$/.test(k)) mark("done");
    else if (/^[nNтТ]$/.test(k)) mark("drop");
    else if (k === "0") mark("");
    else if (k === "[" || k === "х") boundToPlayhead("start");
    else if (k === "]" || k === "ї") boundToPlayhead("end");
  }

  // ─── Open / close ──────────────────────────────────────────────────────────
  function open(project, clips) {
    st.project = project;
    st.clips = clips.map((c) => ({ ...c, review_status: c.review_status || "" }));
    assignNumbers();
    // open on the first shelf that has clips: new ones first, then the ones in work
    st.filter = SHELVES.find((f) => st.clips.some((c) => shelfOf(c) === f)) || "new";
    st.anchor = 0;
    st.tab = "text";
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
    select((st.mode === "vx" ? stripClips()[0] : visible()[0]) ?? ordered()[0] ?? 0);
  }

  function close() {
    const v = video();
    v.pause();
    v.removeAttribute("src");
    v.load();
    closePop();
    hidePhraseCard(true);
    closeStatusMenu();
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
    ["edStatus", "vxStatus"].forEach((id) => $(id).addEventListener("click", (e) => {
      e.stopPropagation();
      if (st.menuBtn === e.currentTarget) closeStatusMenu(); else { closeStatusMenu(); openStatusMenu(e.currentTarget); }
    }));
    $("edStatusMenu").addEventListener("click", (e) => {
      if (e.target.closest("#edPromote")) { promote(); return; }
      const b = e.target.closest("button[data-s]");
      if (b) mark(b.dataset.s);
    });
    $("edStatusMenu").addEventListener("keydown", (e) => {
      const items = [...$("edStatusMenu").querySelectorAll("button:not([hidden])")];
      const i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus(); }
      else if (e.key === "Tab") closeStatusMenu();
    });
    document.addEventListener("click", (e) => { if (!e.target.closest("#edStatusMenu")) closeStatusMenu(); });
    $("edPauses").addEventListener("click", () => findPauses());
    $("edPauseBar").addEventListener("click", (e) => {
      const g = e.target.closest("button[data-m]");
      if (g) { try { localStorage.setItem("studio-pause-mode", g.dataset.m); } catch (_) {} findPauses(g.dataset.m); return; }
      if (e.target.closest("button[data-a='all']")) { const c = clip(); st.restored = []; savePauses(c, [], "Усі паузи повернуто"); }
    });
    $("edMp4Raw").addEventListener("click", () => { toast("Готуємо відео…"); download(`/api/projects/${pid()}/clips/${clip().clip_index}/download?raw=1`); });
    // mouse wheel over the track scrolls it sideways; Ctrl+wheel (or a trackpad pinch) zooms around the playhead
    const onWheel = (e) => {
      if (!clip()) return;
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) { zoomBy(e.deltaY > 0 ? 1.25 : 0.8); return; }
      const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      const px = e.deltaMode === 1 ? d * 30 : d; // lines → pixels (Firefox)
      panBy((px / 100) * (st.zoom.hi - st.zoom.lo) * 0.15);
    };
    $("edTrack").addEventListener("wheel", onWheel, { passive: false });
    $("edMap").addEventListener("wheel", onWheel, { passive: false });
    $("edMap").addEventListener("pointerdown", onMapDown);
    $("edMap").addEventListener("pointermove", onMapMove);
    $("edMap").addEventListener("pointerup", onMapUp);
    $("edMap").addEventListener("pointercancel", onMapUp);
    $("edMap").addEventListener("dblclick", resetZoom);
    $("edZoomOut").addEventListener("click", () => zoomBy(1.5));
    $("edZoomIn").addEventListener("click", () => zoomBy(1 / 1.5));
    $("edZoomAll").addEventListener("click", resetZoom);
    $("edZoomRange").addEventListener("input", (e) => zoomBy(sliderToSpan(Number(e.target.value)) / (st.zoom.hi - st.zoom.lo)));
    const T = $("edTrack");
    T.addEventListener("pointerdown", onTrackDown);
    T.addEventListener("pointermove", onTrackMove);
    T.addEventListener("pointerup", onTrackUp);
    T.addEventListener("pointercancel", onTrackUp);
    T.addEventListener("mouseover", onTrackOver);
    T.addEventListener("mouseleave", () => hidePhraseCard());
    $("edOv").addEventListener("click", (e) => {
      const hit = e.target.closest("[data-i]");
      if (hit) { select(Number(hit.dataset.i)); return; }
      const r = e.currentTarget.getBoundingClientRect();
      const t = ((e.clientX - r.left) / r.width) * (st.project.duration || 0);
      let best = 0, bd = Infinity;
      mapClips().forEach((i) => { const c = st.clips[i]; const d = Math.min(Math.abs(c.start_sec - t), Math.abs(c.end_sec - t)); if (d < bd) { bd = d; best = i; } });
      select(best);
    });
    document.querySelectorAll("#edShelves button").forEach((b) => b.addEventListener("click", () => {
      st.filter = b.dataset.f;
      const vis = visible();
      st.anchor = 0;
      renderList();
      renderOverview();
      $("edList").scrollLeft = 0;
      if (vis.length && !vis.includes(st.cur)) select(vis[0]); // open the first clip of the shelf
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
    document.querySelectorAll("#edSort button").forEach((b) => b.addEventListener("click", () => setSort(b.dataset.s)));
    $("edGoVx").addEventListener("click", () => setMode("vx"));
    $("vxMp4").addEventListener("click", () => $("edMp4").click());
    $("vxSrt").addEventListener("click", () => $("edSrt").click());
    $("vxCopyPost").addEventListener("click", () => copy(postText()));
    // «Вертикальні кліпи»: one settings tab at a time (remembered), «Ще ▾» downloads, previous / next kept clip
    const vxTab = (t) => {
      document.querySelectorAll("#vxTabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.t === t)));
      document.querySelectorAll("#edVx .vx-pane").forEach((p) => { p.hidden = p.dataset.t !== t; });
      try { localStorage.setItem("studio-vx-tab", t); } catch (_) {}
    };
    let savedTab = "style";
    try { savedTab = localStorage.getItem("studio-vx-tab") || "style"; } catch (_) {}
    vxTab(document.querySelector(`#vxTabs button[data-t="${savedTab}"]`) ? savedTab : "style");
    $("vxTabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-t]"); if (b) vxTab(b.dataset.t); });
    const closeVxMore = () => { $("vxMorePop").hidden = true; $("vxMoreBtn").setAttribute("aria-expanded", "false"); };
    $("vxMoreBtn").addEventListener("click", (e) => { e.stopPropagation(); const on = $("vxMorePop").hidden; $("vxMorePop").hidden = !on; $("vxMoreBtn").setAttribute("aria-expanded", String(on)); });
    $("vxMorePop").addEventListener("click", (e) => { if (e.target.closest("button")) closeVxMore(); });
    document.addEventListener("click", (e) => { if (!e.target.closest(".vx-more")) closeVxMore(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeVxMore(); });
    $("vxPrev").addEventListener("click", () => step(-1));
    $("vxNext").addEventListener("click", () => step(1));
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
