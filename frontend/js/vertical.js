/**
 * vertical.js — the «📱 Вертикальні кліпи» screen of the editor.
 * Live preview: draws the editor's own <video> into a 9:16 canvas with the server's crop plan
 * (face tracking / blurred fit / two people stacked) and lays the caption style over it, synced
 * to the playhead. Settings are saved per clip (vertical_opts) and remembered as the template for
 * clips you haven't touched yet. Rendering itself happens on the server (ffmpeg + .ass).
 * «Стрім: вебка + гра» (frame "stream"): the server finds the webcam rectangle once per video (streamer.py);
 * the «Рамки на кадрі» view shows it over the 16:9 frame, where it and the game area can be dragged.
 * The «look» (layout, sizes, zooms, window, styling) is kept per video on the server; one clip may have its own
 * (opts.stream_look). streamPlan() is the twin of streamer.plan(), so sliders and drags on the phone are live.
 * Wired by editor.js through Vertical.attach(ctx).
 */
const Vertical = (() => {
  const $ = (id) => document.getElementById(id);
  const TEMPLATE_KEY = "studio-vertical-template";
  const STYLES = [
    { id: "none", name: "Без субтитрів", chunk: 0 },
    { id: "hl", name: "Хайлайт", chunk: 3, chars: 16, mode: "word", pos: 66 },
    { id: "kar", name: "Караоке", chunk: 6, chars: 22, mode: "fill", pos: 66 },
    { id: "one", name: "Одне слово", chunk: 1, chars: 14, mode: "word", pos: 66 },
    { id: "box", name: "Блок", chunk: 6, chars: 30, mode: "none", pos: 66 },
    { id: "gold", name: "Чорне золото", chunk: 3, chars: 16, mode: "word", pos: 66 },
    { id: "min", name: "Мінімал", chunk: 7, chars: 34, mode: "none", pos: 72 },
  ];
  let ctx = null;
  const st = { opts: null, plan: null, planKey: "", words: [], loading: false, raf: 0, saveTimer: null, pending: null, selWord: null, zipJob: null, capKey: "",
    stream: null, streamPid: "", autoPid: "", sel: null, view: "phone", drag: null, live: null, gesture: null, suppressClick: false, wheelTimer: null, spMemo: null };

  // What each style tile shows: one short phrase drawn in that style
  const TILE = { none: "<i>без тексту</i>", hl: "ЦЕ <i>ВОНО</i>", kar: "<i>ЦЕ</i> ВОНО", one: "ВОНО", box: "<i>Це воно</i>", gold: "ЦЕ <i>ВОНО</i>", min: "це воно" };
  const styleOf = (id) => STYLES.find((s) => s.id === id) || STYLES[1];
  const ACCENTS = ["FFE14D", "7CFF4F", "FF4D4D", "4DC3FF", "FF5EC4", "E6C068", "FF9F1C"];
  const TEXTS = [["FFFFFF", "Біле"], ["FFE14D", "Жовте"], ["FFF8EC", "Кремове"]];
  const DEFAULT_ACCENT = { hl: "FFE14D", kar: "7CFF4F", one: "FFCF33", gold: "E6C068", box: "FFE14D", min: "FFE14D" };
  const TEMPLATE_FIELDS = ["style", "frame", "pos", "hook", "size", "color", "text", "hook_style", "hook_size", "hook_full",
    "round", "stroke", "hook_round", "hook_stroke", "hook_pos"];
  const LIGHT = ["FFFFFF", "FFF8EC"];
  // Same rule as the renderer: a white edge on white letters would melt them, so they keep the black one.
  const letterEdge = (stroke, text) => (stroke && !(stroke === "FFFFFF" && LIGHT.includes(text)) ? `#${stroke}` : "#000");

  function readTemplate() {
    try { return JSON.parse(localStorage.getItem(TEMPLATE_KEY) || "{}") || {}; } catch (_) { return {}; }
  }
  // «Стрім» is a property of the video, not a taste: it is remembered per video, other videos keep their own frame.
  const STREAM_KEY = "studio-stream-videos";
  function streamVideos() {
    try { return JSON.parse(localStorage.getItem(STREAM_KEY) || "[]") || []; } catch (_) { return []; }
  }
  function markStreamVideo(on) {
    const pid = ctx.pid();
    const list = streamVideos().filter((x) => x !== pid);
    if (on) list.push(pid);
    try { localStorage.setItem(STREAM_KEY, JSON.stringify(list.slice(-200))); } catch (_) {}
  }
  /** «Стрім» on/off for the whole video, on the server too (the zip and clips nobody opened follow it). */
  function setStreamUse(on) {
    markStreamVideo(on);
    ctx.api(`/api/projects/${ctx.pid()}/stream?detect=false`).then((s) => {
      if (!on && !s.known) return;  // never searched: nothing to switch off
      return ctx.api(`/api/projects/${ctx.pid()}/stream`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep_cam: true, use: on }),
      }).then((info) => { if (st.streamPid === ctx.pid() || !st.stream) { st.stream = info; st.streamPid = ctx.pid(); } });
    }).catch(() => {});
  }

  /** A stream recording with a webcam switches clips nobody touched to «Стрім» by itself, and says so in a window
   * («Схоже, це запис стріму») once per video. An older video is searched now, with the window showing the search. */
  const SEEN_KEY = "studio-stream-seen";
  function seenVideos() {
    try { return JSON.parse(localStorage.getItem(SEEN_KEY) || "[]") || []; } catch (_) { return []; }
  }
  function markSeen(pid) {
    try { localStorage.setItem(SEEN_KEY, JSON.stringify([...seenVideos().filter((x) => x !== pid), pid].slice(-300))); } catch (_) {}
  }
  const LAYOUT_NAMES = { top: "вебка зверху", full: "вебка + вся гра", pip: "гра на весь екран" };

  async function autoStream() {
    const pid = ctx.pid();
    if (st.autoPid === pid || $("edVx").hidden) return;
    st.autoPid = pid;
    const seen = seenVideos().includes(pid) || streamVideos().includes(pid);
    let info;
    try {
      info = await ctx.api(`/api/projects/${pid}/stream?detect=false`);
      if (!info.known) {
        if (!seen) modalChecking();
        info = await ctx.api(`/api/projects/${pid}/stream`);  // an older video: search now (5-15 s)
      }
    } catch (_) { closeModal(); return; }
    if (ctx.pid() !== pid || !info.cam || info.use === false || seen) { closeModal(); return; }
    if (info.use !== true) {
      try {
        info = await ctx.api(`/api/projects/${pid}/stream`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep_cam: true, use: true }),
        });
      } catch (_) { closeModal(); return; }
    }
    markStreamVideo(true);
    markSeen(pid);
    if (ctx.pid() !== pid) return;
    st.stream = info;
    st.streamPid = pid;
    const c = ctx.clip();
    if (c && !Object.keys(c.vertical_opts || {}).length && st.opts && st.opts.frame !== "stream") {
      st.opts.frame = "stream";
      loadPlan(true);
      renderControls();
      renderNote();
    }
    if (st.modalSkipped) { st.modalSkipped = false; ctx.toast("Це запис стріму: увімкнула «Стрім: вебка + гра». Повернути можна у вкладці «Кадр»."); return; }
    modalFound(info);
  }

  function showModal(title, steps, mode) {
    const m = $("vxStreamModal");
    $("vxSmTitle").textContent = title;
    $("vxSmSteps").innerHTML = steps.map(([t, wait]) => `<li class="${wait ? "wait" : ""}">${escapeText(t)}</li>`).join("");
    $("vxSmBar").hidden = mode !== "checking";
    $("vxSmThumb").hidden = mode !== "found";
    ["vxSmNo", "vxSmFix", "vxSmOk"].forEach((id) => { $(id).hidden = mode !== "found"; });
    $("vxSmSkip").hidden = mode !== "checking";
    m.dataset.mode = mode;
    if (m.hidden) { st.modalFocus = document.activeElement; m.hidden = false; }
    (mode === "found" ? $("vxSmOk") : $("vxSmSkip")).focus();
  }
  function closeModal() {
    const m = $("vxStreamModal");
    clearInterval(st.modalTimer);
    if (m.hidden) return;
    m.hidden = true;
    if (st.modalFocus && st.modalFocus.focus) st.modalFocus.focus();
  }
  function modalChecking() {
    st.modalSkipped = false;
    showModal("📺 Перевіряю, чи це стрім…", [["Дивлюся кадри з усього відео, шукаю вебку стрімера", true]], "checking");
    const bar = $("vxSmBar").firstElementChild, t0 = Date.now();
    bar.style.width = "4%";
    clearInterval(st.modalTimer);
    st.modalTimer = setInterval(() => { bar.style.width = `${Math.min(95, 4 + ((Date.now() - t0) / 15000) * 91)}%`; }, 500);
  }
  function modalFound(info) {
    clearInterval(st.modalTimer);
    const [x, y, w, h] = info.cam;
    const side = x + w / 2 < 0.4 ? "зліва" : x + w / 2 > 0.6 ? "справа" : "посередині";
    const lk = cleanLook(info.look);
    showModal("📺 Схоже, це запис стріму", [
      ["Переглянула 20 кадрів з усього відео"],
      [`Знайшла вебку стрімера (${side})`],
      [`Вертикальні кліпи: «Стрім: вебка + гра», ${LAYOUT_NAMES[lk.layout]}`],
    ], "found");
    const v = ctx.video(), cv = $("vxSmCanvas");
    if (v.videoWidth) {
      cv.width = 640;
      cv.height = Math.round((640 * v.videoHeight) / v.videoWidth);
      try { cv.getContext("2d").drawImage(v, 0, 0, cv.width, cv.height); } catch (_) {}
    }
    Object.assign($("vxSmBox").style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
  }
  function modalNotStream() {
    closeModal();
    setStreamUse(false);
    const c = ctx.clip();
    if (c && !Object.keys(c.vertical_opts || {}).length && st.opts.frame === "stream") {
      st.opts.frame = "auto";
      loadPlan(true);
      renderControls();
      renderNote();
    }
  }

  function writeTemplate(o) {
    const t = {};
    const old = readTemplate();
    TEMPLATE_FIELDS.forEach((k) => { t[k] = o[k]; });
    if (t.frame === "stream") t.frame = old.frame && old.frame !== "stream" ? old.frame : "auto";
    try { localStorage.setItem(TEMPLATE_KEY, JSON.stringify(t)); } catch (_) {}
  }

  function optsFor(clip) {
    const own = clip.vertical_opts || {};
    const base = { style: "hl", frame: "auto", pos: null, hook: false, edits: {}, size: 1, color: null, text: null,
      hook_text: null, hook_style: "white", hook_size: 1, hook_full: false,
      round: false, stroke: null, hook_round: false, hook_stroke: null, hook_pos: null, stream_look: null };
    const o = Object.keys(own).length ? { ...base, ...own, edits: { ...(own.edits || {}) } } : { ...base, ...readTemplate(), edits: {} };
    if (!Object.keys(own).length && streamVideos().includes(ctx.pid())) o.frame = "stream";
    if (o.frame === "track" || o.frame === "split") o.frame = "auto"; // these now live inside «Авто»
    return o;
  }

  // ─── Controls ──────────────────────────────────────────────────────────────
  function renderControls() {
    const o = st.opts;
    $("vxStyle").innerHTML = STYLES.map((s) => `<button type="button" data-v="${s.id}" aria-pressed="${s.id === o.style}">` +
      `<span class="vx-tile vx-t-${s.id}" aria-hidden="true">${TILE[s.id]}</span><b>${s.name}</b></button>`).join("");
    const frame = ["blur", "stream"].includes(o.frame) ? o.frame : "auto";
    document.querySelectorAll("#vxFrame button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === frame)));
    renderStream();
    const split = st.plan && st.plan.layout.kind === "split";
    const ssp = curSp();
    const streamPos = ssp ? ssp.pos : null;
    $("vxPos").value = o.pos ?? (split ? 50 : streamPos ?? (styleOf(o.style).pos || 66));
    $("vxSize").value = Math.round((o.size || 1) * 100);
    $("vxSizeV").textContent = `${Math.round((o.size || 1) * 100)}%`;
    const acc = o.color || DEFAULT_ACCENT[o.style] || "FFE14D";
    $("vxColor").innerHTML = `<button type="button" class="auto" data-v="" aria-pressed="${!o.color}" title="Колір стилю">Стилю</button>` +
      ACCENTS.map((c) => `<button type="button" data-v="${c}" aria-pressed="${o.color === c}" style="background:#${c}" title="#${c}"></button>`).join("");
    $("vxText").innerHTML = TEXTS.map(([c, n], i) => `<button type="button" data-v="${i ? c : ""}" aria-pressed="${(o.text || "") === (i ? c : "")}" style="background:#${c}" title="${n}"></button>`).join("");
    $("vxHookOn").checked = !!o.hook;
    $("vxHookSet").hidden = !o.hook;
    if (document.activeElement !== $("vxHookText")) $("vxHookText").value = o.hook_text || ctx.clip().title || "";
    document.querySelectorAll("#vxHookStyle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === (o.hook_style || "white"))));
    document.querySelectorAll("#vxHookDur button").forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.v === "all") === !!o.hook_full)));
    $("vxHookSize").value = Math.round((o.hook_size || 1) * 100);
    const streamHook = ssp ? ssp.hook_pos : null;
    const hookPos = o.hook_pos ?? streamHook ?? 9.9;
    $("vxHookPos").value = hookPos;
    const pressed = (id, v) => document.querySelectorAll(`#${id} button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
    pressed("vxRound", o.round ? "1" : "0");
    pressed("vxHookRound", o.hook_round ? "1" : "0");
    pressed("vxStroke", o.stroke || "");
    pressed("vxHookStroke", o.hook_stroke || "");
    const ph = $("vxPhone");
    ph.className = `vx-phone vx-s-${o.style}` + (ph.classList.contains("playing") ? " playing" : "");
    ph.style.setProperty("--vx-s", o.size || 1);
    ph.style.setProperty("--vx-acc", `#${acc}`);
    ph.style.setProperty("--vx-txt", `#${o.text || "FFFFFF"}`);
    ph.style.setProperty("--vx-hs", o.hook_size || 1);
    ph.style.setProperty("--vx-r", o.round ? ".62em" : "0");
    ph.style.setProperty("--vx-hr", o.hook_round ? ".8em" : "0");
    ph.style.setProperty("--vx-edge", o.stroke ? `#${o.stroke}` : "transparent");
    ph.style.setProperty("--vx-hedge", o.hook_stroke ? `#${o.hook_stroke}` : "transparent");
    ph.style.setProperty("--vx-stroke", letterEdge(o.stroke, o.text || "FFFFFF"));
    ph.style.setProperty("--vx-hstroke", letterEdge(o.hook_stroke, o.text || "FFFFFF"));
    $("vxCap").style.top = `${$("vxPos").value}%`;
    const hook = $("vxHook");
    hook.style.top = `${hookPos}%`;
    hook.className = `vx-hook h-${o.hook_style || "white"}`;
    hook.innerHTML = `<span>${escapeText(o.hook_text || ctx.clip().title || "")}</span>`;
    $("vxDlSubs").disabled = o.style === "none";
    st.capKey = "";
  }

  function renderNote() {
    const n = $("vxNote");
    const p = st.plan;
    let text = "";
    if (p) {
      const kind = { track: "Кадр слідкує за обличчям.", split: "У кадрі двоє: ділю екран навпіл.", blur: "Показую весь кадр на розмитому тлі.", center: "Кадр по центру.", stream: "" }[p.layout.kind] || "";
      text = p.layout.note || (st.opts.frame === "auto" ? `Авто: ${kind.toLowerCase()}` : "");
      if (!p.words.length && st.opts.style !== "none") text = (text ? text + " " : "") + "Тексту для цього відео немає, субтитрів не буде.";
    }
    n.textContent = text;
    n.hidden = !text;
  }

  function renderWordChips() {
    const W = $("vxWords");
    const edits = st.opts.edits || {};
    if (!st.words.length) { W.innerHTML = `<span class="ed-small">Тексту для цього кліпу не знайдено.</span>`; return; }
    let html = "", line = "", n = 0, t0 = null;
    const flushLine = () => { if (line) html += `<div class="vx-ln"><span class="t">${fmtLine(t0)}</span><div>${line}</div></div>`; line = ""; n = 0; t0 = null; };
    st.words.forEach((w, i) => {
      const e = edits[w.id] || {};
      const gone = e.w === "";
      const cls = [w.key && !gone ? "key" : "", e.w !== undefined || e.k !== undefined ? "edited" : "", gone ? "gone" : "", st.selWord === w.id ? "sel" : ""].join(" ");
      if (t0 === null) t0 = w.start;
      line += `<button type="button" class="${cls}" data-i="${i}">${escapeText(gone ? w.orig : w.word)}</button>`;
      n++;
      if (/[.!?…]$/.test(w.orig || w.word) || n >= 8) flushLine();
    });
    flushLine();
    W.innerHTML = html;
    const tab = document.getElementById("vxTabText");
    if (tab) tab.classList.toggle("dot", Object.keys(edits).length > 0);
  }

  function fmtLine(t) { t = Math.max(0, t || 0); return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`; }
  function escapeText(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  /** Words from the plan exclude deleted ones; keep them visible (struck through) so they can be restored. */
  function mergeWords(planWords) {
    const edits = st.opts.edits || {};
    const list = planWords.map((w) => ({ ...w, orig: w.word }));
    for (const [id, e] of Object.entries(edits)) {
      if (e.w === "" && !list.some((w) => w.id === id)) {
        const t = parseFloat(id) - ctx.clip().start_sec;
        list.push({ id, start: t, end: t + 0.3, word: "", orig: e.orig || "…", key: false });
      }
    }
    return list.sort((a, b) => a.start - b.start);
  }

  function openWordEdit(i) {
    const w = st.words[i];
    st.selWord = w.id;
    const e = (st.opts.edits || {})[w.id] || {};
    $("vxWinput").value = e.w === "" ? w.orig : w.word;
    $("vxWkey").checked = e.k !== undefined ? e.k : w.key;
    $("vxWedit").hidden = false;
    renderWordChips();
    $("vxWinput").focus();
    $("vxWinput").select();
    const c = ctx.clip();
    ctx.seek(c.start_sec + w.start);
  }

  function closeWordEdit() { st.selWord = null; $("vxWedit").hidden = true; renderWordChips(); }

  function applyWordEdit(kind) {
    const w = st.words.find((x) => x.id === st.selWord);
    if (!w) return;
    const edits = { ...(st.opts.edits || {}) };
    if (kind === "reset") delete edits[w.id];
    else if (kind === "del") edits[w.id] = { w: "", orig: w.orig };
    else {
      const text = $("vxWinput").value.trim();
      if (!text) { applyWordEdit("del"); return; }
      edits[w.id] = { w: text, k: $("vxWkey").checked };
    }
    st.opts.edits = edits;
    // reflect locally right away
    const e = edits[w.id];
    w.word = !e ? w.orig : e.w === "" ? "" : e.w;
    if (e && e.k !== undefined) w.key = e.k;
    st.capKey = "";
    closeWordEdit();
    save();
  }

  function setOpt(k, v) {
    st.opts[k] = v;
    if (k === "frame") setStreamUse(v === "stream");
    if (k === "frame") {
      if (k === "frame" && v !== "stream") st.view = "phone";
      loadPlan(true);
    }
    renderControls();
    renderNote();
    writeTemplate(st.opts);
    save();
  }

  function save() {
    const c = ctx.clip();
    const opts = JSON.parse(JSON.stringify(st.opts));
    c.vertical_opts = opts;
    clearTimeout(st.saveTimer);
    st.pending = async () => { st.pending = null; await ctx.patch({ vertical_opts: opts }, true, c); };
    st.saveTimer = setTimeout(() => { st.saveTimer = null; if (st.pending) st.pending(); }, 400);
  }
  /** Sends a pending settings save now (before rendering, or when leaving the clip). */
  async function flush() {
    clearTimeout(st.saveTimer);
    st.saveTimer = null;
    if (st.pending) await st.pending();
  }

  // ─── Plan (faces + words) ──────────────────────────────────────────────────
  async function loadPlan(force = false) {
    const c = ctx.clip();
    if (!c) return;
    const o = st.opts;
    const stream = o.frame === "stream";
    const key = `${c.clip_index}:${c.start_sec}:${c.end_sec}:${o.frame}`;
    if (!force && key === st.planKey && st.plan) return;
    st.planKey = key;
    $("vxBusy").hidden = false;
    $("vxBusy").textContent = stream && !st.stream ? "Шукаю вебку на стрімі… (до 10 с)" : "Шукаю обличчя…";
    try {
      const url = `/api/projects/${ctx.pid()}/clips/${c.clip_index}/vertical-plan?frame=${o.frame}`;
      const plan = await ctx.api(url);
      if (st.planKey !== key) return;
      if (stream && (!st.stream || st.streamPid !== ctx.pid())) {
        st.stream = await ctx.api(`/api/projects/${ctx.pid()}/stream`);
        st.streamPid = ctx.pid();
      }
      st.plan = plan;
      st.words = mergeWords(plan.words || []);
      $("vxBusy").hidden = true;
    } catch (e) {
      if (st.planKey !== key) return;
      st.plan = null;
      st.words = [];
      $("vxBusy").textContent = e.message === "Not Found"
        ? "Студію запущено зі старою версією. Закрий вікно студії, запусти її знову й онови сторінку (Ctrl+F5)."
        : "Попередній вигляд недоступний: " + e.message;
    }
    renderControls();
    renderNote();
    renderWordChips();
  }

  // ─── Preview drawing ───────────────────────────────────────────────────────
  function cropX(keys, t) {
    if (!keys || !keys.length) return 0.5;
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 1; i < keys.length; i++) {
      if (t < keys[i][0]) {
        const [t0, x0] = keys[i - 1], [t1, x1] = keys[i];
        const u = Math.min(1, Math.max(0, (t - t0) / Math.max(0.001, t1 - t0)));
        return x0 + (x1 - x0) * u * u * (3 - 2 * u); // same ease in/out as the render
      }
    }
    return keys[keys.length - 1][1];
  }

  function draw() {
    const cv = $("vxCanvas");
    const g = cv.getContext("2d");
    const v = ctx.video();
    const W = cv.width, H = cv.height;
    const vw = v.videoWidth, vh = v.videoHeight;
    if (!vw || !vh || v.readyState < 2) return;
    const c = ctx.clip();
    const t = Math.max(0, v.currentTime - c.start_sec);
    const lay = st.plan ? st.plan.layout : { kind: "center", keyframes: [[0, 0.5]] };
    g.filter = "none";
    if (lay.kind === "blur") {
      const s = Math.max(W / vw, H / vh);
      g.filter = "blur(8px) brightness(0.85)";
      g.drawImage(v, (W - vw * s) / 2, (H - vh * s) / 2, vw * s, vh * s);
      g.filter = "none";
      const h = (W / vw) * vh;
      g.drawImage(v, 0, (H - h) / 2, W, h);
    } else if (lay.kind === "stream" && curSp()) {
      const sc = sceneAt(t);
      if (sc && sc.kind === "chat") g.drawImage(v, sc.src[0] * vw, 0, sc.src[2] * vw, vh, 0, 0, W, H);
      else drawStream(g, v, sc ? scenePlan(sc) : curSp(), W, H);
    } else if (lay.kind === "split" && lay.split.length === 2) {
      lay.split.forEach(([cx, cy, hf], k) => {
        let ch = vh * hf, cw = Math.min(vw, ch * (W / (H / 2)));
        ch = Math.min(ch, cw * (H / 2) / W);
        const sx = Math.max(0, Math.min(vw - cw, cx * vw - cw / 2));
        const sy = Math.max(0, Math.min(vh - ch, cy * vh - ch * 0.42));
        g.drawImage(v, sx, sy, cw, ch, 0, k * H / 2, W, H / 2);
      });
    } else {
      const cw = Math.min(vw, vh * 9 / 16);
      const sx = Math.max(0, Math.min(vw - cw, cropX(lay.keyframes, t) * vw - cw / 2));
      g.drawImage(v, sx, 0, cw, vh, 0, 0, W, H);
    }
    drawCaption(t, v.paused);
    const tc = `${fmtT(t)} / ${fmtT(c.end_sec - c.start_sec)}`;
    if ($("vxTc").textContent !== tc) $("vxTc").textContent = tc;
    const sc = $("vxScrub");
    if (!st.scrubbing) { sc.max = (c.end_sec - c.start_sec).toFixed(2); sc.value = t.toFixed(2); }
  }
  const fmtT = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); const x = s - m * 60; return `${m}:${x < 10 ? "0" : ""}${x.toFixed(1).replace(".", ",")}`; }; // Ukrainian decimal comma

  function chunks(words, n, maxChars) {
    const out = [];
    let cur = [];
    for (const w of words) {
      if (cur.length) {
        const gap = w.start - cur[cur.length - 1].end;
        const chars = cur.reduce((a, x) => a + x.word.length + 1, 0) + w.word.length;
        if (cur.length >= n || chars > maxChars || gap > 0.7) { out.push(cur); cur = []; }
      }
      cur.push(w);
      if (n > 1 && /[.!?…]$/.test(w.word) && cur.length >= 2) { out.push(cur); cur = []; }
    }
    if (cur.length) out.push(cur);
    return out;
  }

  /** paused: show the nearest subtitle line even in a gap, so a still preview looks like the finished clip. */
  function drawCaption(t, paused = false) {
    const el = $("vxCap");
    const s = styleOf(st.opts.style);
    $("vxHook").hidden = !(st.opts.hook && (st.opts.hook_full || t < 3));
    if (s.id === "none") { el.innerHTML = ""; return; }
    const words = st.words.filter((w) => w.word);
    const cs = chunks(words, s.chunk, s.chars);
    let ci = -1;
    cs.forEach((ch, i) => { if (t >= ch[0].start - 0.02) ci = i; });
    if (ci < 0 && paused && cs.length) ci = 0;
    if (ci < 0) { if (st.capKey !== "") { el.innerHTML = ""; st.capKey = ""; } return; }
    const ch = cs[ci];
    const next = cs[ci + 1];
    const end = Math.min(next ? next[0].start : 1e9, ch[ch.length - 1].end + 0.35);
    if (t > end && !paused) { if (st.capKey !== "") { el.innerHTML = ""; st.capKey = ""; } return; }
    let wi = 0;
    ch.forEach((w, i) => { if (t >= w.start - 0.02) wi = i; });
    const key = `${ci}:${wi}:${s.id}`;
    if (key === st.capKey) return;
    st.capKey = key;
    const up = (x) => (["hl", "one", "gold"].includes(s.id) ? x.toUpperCase() : x);
    if (s.mode === "none") {
      el.innerHTML = s.id === "box" ? `<span class="boxed">${escapeText(ch.map((w) => w.word).join(" "))}</span>` : escapeText(ch.map((w) => w.word).join(" "));
      return;
    }
    el.innerHTML = ch.map((w, i) => {
      let c = "w";
      if (i === wi) c += " on"; else if (i < wi) c += " past";
      if (w.key) c += " k";
      return `<span class="${c}">${escapeText(up(w.word))}</span>`;
    }).join("");
  }

  // ─── «Стрім: вебка + гра» ──────────────────────────────────────────────────
  const OW = 1080, OH = 1920;
  const LOOK_DEFAULT = { layout: "top", share: 0.34, cam_zoom: 1, game_zoom: 1, cam_fx: null, cam_fy: null, game_x: 0.5, game_y: 0.5,
    pip_x: 0.04, pip_y: 0.035, pip_w: 0.48, game_w: null, game_cx: 0.5, game_cy: 0.5, full_win: null, round: false, frame: "white", divider: false,
    nick: "", nick_style: "twitch", nick_x: null, nick_y: null };
  const SIZE_KEYS = ["share", "cam_zoom", "game_zoom", "cam_fx", "cam_fy", "game_x", "game_y", "pip_x", "pip_y", "pip_w", "game_w", "game_cx", "game_cy", "full_win"];
  const FRAME_COLORS = { none: null, white: "FFFFFF", cam: "9146FF", gold: "E6C068" };
  const PIP_BORDER = 6, PIP_RADIUS = 28, NICK_TOP = [36, 48];
  // streamer.NICK_STYLES: canvas font, size, uppercase, padding x, padding y, icon width
  const NICK_STYLES = { twitch: ["900", "VX Rubik Black", 42, false, 18, 8, 44], neon: ["900", "VX Unbounded", 38, true, 12, 10, 0],
    pill: ["900", "VX Montserrat", 40, false, 26, 8, 0], bold: ["900", "VX Rubik Black", 52, true, 8, 4, 0], tape: ["600", "VX Oswald", 48, true, 20, 4, 0] };
  let measureCtx = null;
  function nickSize(text, style) {
    const [wt, font, size, up, px, py, icon] = NICK_STYLES[style] || NICK_STYLES.twitch;
    measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
    measureCtx.font = `${wt} ${size}px "${font}"`;
    return [Math.round(measureCtx.measureText(up ? text.toUpperCase() : text).width + 2 * px + icon), Math.round(size * 1.25) + 2 * py];
  }
  const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const even = (v) => Math.max(2, Math.floor(Math.round(v) / 2) * 2);
  const r4 = (v) => Math.round(v * 10000) / 10000;

  /** Same ranges as streamer.clean_look. */
  const FULL_WIN_RANGES = { cam_w: [0.25, 1], cam_x: [0, 1], cam_y: [0, 1], game_w: [0.2, 4], game_cx: [-1, 2], game_cy: [-1, 2] };
  /** «Вебка + вся гра» windows the user moved (twin of streamer._clean_full_win); nothing → null (the preset). */
  function cleanFullWin(v) {
    if (!v || typeof v !== "object") return null;
    const out = {};
    for (const [k, [lo, hi]] of Object.entries(FULL_WIN_RANGES)) {
      const n = v[k] === null || v[k] === undefined ? NaN : Number(v[k]);
      if (Number.isFinite(n)) out[k] = Math.round(clampN(n, lo, hi) * 1e4) / 1e4;
    }
    return Object.keys(out).length ? out : null;
  }
  function cleanLook(raw) {
    const o = { ...LOOK_DEFAULT, ...(raw || {}) };
    const num = (k, lo, hi, nullable) => {
      if (nullable && (o[k] === null || o[k] === undefined)) { o[k] = null; return; }
      const v = Number(o[k]);
      o[k] = Number.isFinite(v) ? clampN(v, lo, hi) : LOOK_DEFAULT[k];
    };
    if (!["top", "full", "pip"].includes(o.layout)) o.layout = "top";
    num("share", 0.22, 0.6); num("cam_zoom", 1, 3); num("game_zoom", 1, 3);
    num("cam_fx", 0, 1, true); num("cam_fy", 0, 1, true); num("game_x", 0, 1); num("game_y", 0, 1);
    num("pip_w", 0.25, 1); num("pip_x", 0, 1); num("pip_y", 0, 1); num("nick_x", 0, 1, true); num("nick_y", 0, 1, true);
    num("game_w", 0.2, 4, true); num("game_cx", -1, 2); num("game_cy", -1, 2);
    o.full_win = cleanFullWin(o.full_win);
    if (!(o.nick_style in NICK_STYLES)) o.nick_style = "twitch";
    if (!(o.frame in FRAME_COLORS)) o.frame = "white";
    o.round = !!o.round; o.divider = !!o.divider;
    o.nick = String(o.nick || "").replace(/[{}\\\n\r]/g, "").trim().slice(0, 30);
    return o;
  }

  function fit(aspect, box, focus, zoom = 1, bias = 0.45) {
    const [bx, by, bw, bh] = box;
    let w, h;
    if (bw / bh > aspect) { w = bh * aspect; h = bh; } else { w = bw; h = bw / aspect; }
    w /= zoom; h /= zoom;
    return [clampN(focus[0] - w / 2, bx, bx + bw - w), clampN(focus[1] - h * bias, by, by + bh - h), w, h];
  }

  const WIN_KEEP = 120;  // px of the game window that always stay on the phone
  /** Twin of streamer._game_window(): «Гра на весь екран», the game as a 16:9 window (game_w null = the whole phone). */
  function gameWindow(lk, frame, focus) {
    const W = frame[2], H = frame[3];
    const shown = fit(W / H, frame, focus, lk.game_zoom, 0.5);
    const r1 = (v) => Math.round(v * 10) / 10;
    if (lk.game_w === null) {
      const r = fit(OW / OH, frame, focus, lk.game_zoom, 0.5), k = OH / r[3];
      return { src: r, dst: [0, 0, OW, OH], win: [(shown[0] - r[0]) * k, (shown[1] - r[1]) * k, shown[2] * k, shown[3] * k].map(r1), bg: false };
    }
    const ww = lk.game_w * OW, wh = ww * shown[3] / shown[2];
    const cx = clampN(lk.game_cx * OW, WIN_KEEP - ww / 2, OW - WIN_KEEP + ww / 2), cy = clampN(lk.game_cy * OH, WIN_KEEP - wh / 2, OH - WIN_KEEP + wh / 2);
    const wx = cx - ww / 2, wy = cy - wh / 2;
    const x0 = Math.max(0, wx), y0 = Math.max(0, wy), x1 = Math.min(OW, wx + ww), y1 = Math.min(OH, wy + wh);
    const src = [shown[0] + (x0 - wx) / ww * shown[2], shown[1] + (y0 - wy) / wh * shown[3], (x1 - x0) / ww * shown[2], (y1 - y0) / wh * shown[3]];
    const covers = x0 <= 0 && y0 <= 0 && x1 >= OW && y1 >= OH;
    return { src, dst: covers ? [0, 0, OW, OH] : [x0, y0, x1 - x0, y1 - y0], win: [wx, wy, ww, wh].map(r1), bg: !covers };
  }

  /** Twin of streamer.plan(): the same parts for the live preview (the render uses the Python one). */
  function streamPlan(cam, face, W, H, look) {
    const lk = cleanLook(look);
    const layout = cam ? lk.layout : "game";
    const gf = [lk.game_x * W, lk.game_y * H];
    const frame = [0, 0, W, H];
    let cb = null, f = null, m = 0;
    if (cam) {
      cb = [cam[0] * W, cam[1] * H, cam[2] * W, cam[3] * H];
      m = 0.004 * W;
      if (cb[2] > 8 * m && cb[3] > 8 * m) cb = [cb[0] + m, cb[1] + m, cb[2] - 2 * m, cb[3] - 2 * m];
      if (lk.cam_fx !== null && lk.cam_fy !== null) f = [cb[0] + lk.cam_fx * cb[2], cb[1] + lk.cam_fy * cb[3]];
      else if (face) f = [face[0] * W, face[1] * H];
      else f = [cb[0] + cb[2] / 2, cb[1] + cb[3] / 2];
      f = [clampN(f[0], cb[0], cb[0] + cb[2]), clampN(f[1], cb[1], cb[1] + cb[3])];
    }
    const color = FRAME_COLORS[lk.frame];
    const frac = (b) => [b[0] / W, b[1] / H, b[2] / W, b[3] / H];
    const part = (role, src, dst, border = 0, radius = 0) => ({ role, src: frac(src), dst: dst.map((v) => Math.trunc(v)), border, radius, color: border ? color : null });
    const parts = [];
    let bg = false, pos = null, hookPos = null, divider = null, at = null;
    if (layout === "game") {
      parts.push(part("game", fit(OW / OH, frame, gf, lk.game_zoom, 0.5), [0, 0, OW, OH]));
      at = NICK_TOP;
    } else if (layout === "top") {
      const ch = even(OH * lk.share);
      parts.push(part("cam", fit(OW / ch, cb, f, lk.cam_zoom), [0, 0, OW, ch]));
      const gh = OH - ch;
      parts.push(part("game", fit(OW / gh, frame, gf, lk.game_zoom, 0.5), [0, ch, OW, gh]));
      pos = Math.round(1000 * ch / OH) / 10;
      if (lk.divider) divider = [0, ch - 3, OW, 6];
      at = NICK_TOP;
    } else if (layout === "full") {
      // two windows: the webcam (full width on top at first) and the whole game under it, without the webcam's strip
      const fw = lk.full_win || {}, get = (k, d) => (fw[k] === undefined ? d : fw[k]);
      const pw = even(OW * get("cam_w", 1)), ph = even(pw * cb[3] / cb[2]);
      const px = Math.trunc(clampN(get("cam_x", 0) * OW, 0, OW - pw)), py = Math.trunc(clampN(get("cam_y", 0) * OH, 0, OH - ph));
      let gbox = frame;
      const camL = cam[0], camR = cam[0] + cam[2];
      if (camR <= 0.4) gbox = [camR * W + 2, 0, W - camR * W - 2, H];
      else if (camL >= 0.6) gbox = [0, 0, camL * W - 2, H];
      const gw0 = get("game_w", 1), gh0 = OW * gw0 * gbox[3] / gbox[2];
      const gl = { ...lk, game_w: gw0, game_cx: get("game_cx", 0.5), game_cy: get("game_cy", (py + ph + gh0 / 2) / OH) };
      const gw = gameWindow(gl, gbox, gf);
      bg = gw.bg;
      parts.push({ ...part("game", gw.src, gw.dst), win: gw.win });
      parts.push(part("cam", fit(cb[2] / cb[3], cb, f, lk.cam_zoom), [px, py, pw, ph]));
      const gd = gw.dst;
      pos = Math.round(10 * Math.min(85, 100 * (Math.max(py + ph, gd[1] + gd[3]) + 150) / OH)) / 10;
      if (lk.divider) divider = [px, py + ph - 3, pw, 6];
      at = [px + NICK_TOP[0], py + NICK_TOP[1]];
    } else {
      const b = color ? PIP_BORDER : 0;
      const pw = Math.min(even(OW * lk.pip_w), OW - 2 * b), ph = even(pw * cb[3] / cb[2]);
      const ow = pw + 2 * b, oh = ph + 2 * b;
      const px = Math.trunc(clampN(lk.pip_x * OW, 0, OW - ow)), py = Math.trunc(clampN(lk.pip_y * OH, 0, OH - oh));
      const gw = gameWindow(lk, frame, gf);
      bg = gw.bg;
      parts.push({ ...part("game", gw.src, gw.dst), win: gw.win });
      parts.push(part("cam", fit(cb[2] / cb[3], cb, f, lk.cam_zoom), [px, py, ow, oh], b, lk.round ? PIP_RADIUS : 0));
      if (py < OH * 0.3) hookPos = Math.round(10 * Math.min(80, 100 * (py + oh + 40) / OH)) / 10;
      const nh = nickSize(lk.nick || "x", lk.nick_style)[1];
      at = py + oh + 14 + nh < OH - 40 ? [px + 8, py + oh + 14] : [px + 8, Math.max(0, py - 14 - nh)];
    }
    let blur = null;
    if (cam) {
      const mm = m + 4, bx = Math.max(0, cb[0] - mm), by = Math.max(0, cb[1] - mm);
      blur = frac([bx, by, Math.min(W, cb[0] + cb[2] + mm) - bx, Math.min(H, cb[1] + cb[3] + mm) - by]);
    }
    let nick = null;
    if (lk.nick && at) {
      const [nw, nh] = nickSize(lk.nick, lk.nick_style);
      let [x, y] = at;
      if (lk.nick_x !== null && lk.nick_y !== null) { x = lk.nick_x * OW; y = lk.nick_y * OH; }
      nick = { text: lk.nick, style: lk.nick_style, w: nw, h: nh, x: Math.trunc(clampN(x, 0, OW - nw)), y: Math.trunc(clampN(y, 0, OH - nh)) };
    }
    return { layout, bg, parts, blur, pos, hook_pos: hookPos, divider, divider_color: color || "FFFFFF", nick, look: lk };
  }

  const streamOn = () => st.opts && st.opts.frame === "stream" && st.plan && st.plan.layout.kind === "stream" && st.stream;
  /** The look in effect: being dragged → this clip's own → the video's. */
  const look = () => cleanLook(st.live || (st.opts && st.opts.stream_look) || (st.stream && st.stream.look));
  /** The stream plan for the preview (memoised: draw() asks every frame). */
  function curSp() {
    if (!streamOn()) return null;
    const src = st.plan.src || { w: st.stream.w, h: st.stream.h };
    const lk = look();
    const key = JSON.stringify([st.stream.cam, st.stream.face, src.w, src.h, lk]);
    if (!st.spMemo || st.spMemo.key !== key) st.spMemo = { key, sp: streamPlan(st.stream.cam, st.stream.face, src.w, src.h, lk) };
    return st.spMemo.sp;
  }

  /** A part of the clip with its own picture (streamer on the whole screen, webcam moved), from the server. */
  function sceneAt(t) {
    const list = st.live || !st.plan || !st.plan.layout.stream ? null : st.plan.layout.stream.scenes;
    return list ? list.find((sc) => t >= sc.start && t < sc.end) || null : null;
  }
  function scenePlan(sc) {
    const lk = look(), src = st.plan.src;
    const key = JSON.stringify([sc.cam, sc.face, lk]);
    if (!sc.memo || sc.memo.key !== key) sc.memo = { key, sp: streamPlan(sc.cam, sc.face, src.w, src.h, lk) };
    return sc.memo.sp;
  }

  /** Same parts as the renderer: src in fractions of the frame, dst in px of 1080×1920. */
  function drawStream(g, v, sp, W, H) {
    const vw = v.videoWidth, vh = v.videoHeight, k = W / OW;
    g.fillStyle = "#000";
    g.fillRect(0, 0, W, H);
    if (sp.bg) {
      const s = Math.max(W / vw, H / vh);
      g.filter = "blur(10px) brightness(0.8)";
      g.drawImage(v, (W - vw * s) / 2, (H - vh * s) / 2, vw * s, vh * s);
      g.filter = "none";
    }
    for (const p of sp.parts) {
      const [sx, sy, sw, sh] = [p.src[0] * vw, p.src[1] * vh, p.src[2] * vw, p.src[3] * vh];
      const b = (p.border || 0) * k;
      const [dx, dy, dw, dh] = [p.dst[0] * k, p.dst[1] * k, p.dst[2] * k, p.dst[3] * k];
      g.save();
      if (p.radius) { g.beginPath(); g.roundRect(dx, dy, dw, dh, p.radius * k); g.clip(); }
      if (b) { g.fillStyle = `#${p.color || "FFFFFF"}`; g.fillRect(dx, dy, dw, dh); }
      g.drawImage(v, sx, sy, sw, sh, dx + b, dy + b, dw - 2 * b, dh - 2 * b);
      g.restore();
      if (p.role === "game" && sp.blur) {
        // the webcam inside the game picture is blurred, as in the render
        const bx0 = Math.max(p.src[0], sp.blur[0]), by0 = Math.max(p.src[1], sp.blur[1]);
        const bx1 = Math.min(p.src[0] + p.src[2], sp.blur[0] + sp.blur[2]), by1 = Math.min(p.src[1] + p.src[3], sp.blur[1] + sp.blur[3]);
        if (bx1 > bx0 && by1 > by0) {
          const fx = (x) => dx + ((x - p.src[0]) / p.src[2]) * dw, fy = (y) => dy + ((y - p.src[1]) / p.src[3]) * dh;
          g.save();
          g.beginPath();
          g.rect(fx(bx0), fy(by0), fx(bx1) - fx(bx0), fy(by1) - fy(by0));
          g.clip();
          g.filter = "blur(8px)";
          g.drawImage(v, bx0 * vw, by0 * vh, (bx1 - bx0) * vw, (by1 - by0) * vh, fx(bx0), fy(by0), fx(bx1) - fx(bx0), fy(by1) - fy(by0));
          g.restore();
          g.filter = "none";
        }
      }
    }
    if (sp.divider) {
      g.fillStyle = `#${sp.divider_color}`;
      g.fillRect(sp.divider[0] * k, sp.divider[1] * k, sp.divider[2] * k, sp.divider[3] * k);
    }
    if (sp.nick) drawNick(g, sp.nick, k);
  }

  /** The nick as the render draws it (streamer.nick_events). */
  function drawNick(g, nk, k) {
    const [wt, font, size, up, padx, pady, icon] = NICK_STYLES[nk.style] || NICK_STYLES.twitch;
    const text = up ? nk.text.toUpperCase() : nk.text;
    const x = nk.x * k, y = nk.y * k, w = nk.w * k, h = nk.h * k;
    const rr = (x0, y0, ww, hh, r, fill) => { g.fillStyle = fill; g.beginPath(); g.roundRect(x0, y0, ww, hh, r); g.fill(); };
    g.save();
    g.font = `${wt} ${size * k}px "${font}"`;
    g.textBaseline = "middle";
    const ty = y + h / 2;
    const tx = (x0) => g.fillText(text, x0, ty);
    if (nk.style === "twitch") {
      rr(x, y, w, h, 14 * k, "#9146ff");
      const ix = x + padx * k, iy = y + (h - 34 * k) / 2;
      rr(ix, iy, 32 * k, 26 * k, 5 * k, "#fff");
      g.beginPath(); g.moveTo(ix + 6 * k, iy + 25 * k); g.lineTo(ix + 6 * k, iy + 34 * k); g.lineTo(ix + 15 * k, iy + 25 * k); g.fill();
      g.fillStyle = "#9146ff"; g.fillRect(ix + 11 * k, iy + 7 * k, 4 * k, 10 * k); g.fillRect(ix + 20 * k, iy + 7 * k, 4 * k, 10 * k);
      g.fillStyle = "#fff"; tx(x + (padx + icon) * k);
    } else if (nk.style === "neon") {
      g.shadowColor = "#00e5ff"; g.shadowBlur = 16 * k; g.fillStyle = "#fff";
      g.lineWidth = 4 * k; g.strokeStyle = "#00e5ff"; g.strokeText(text, x + padx * k, ty); tx(x + padx * k);
    } else if (nk.style === "pill") {
      rr(x, y + 5 * k, w, h, h / 2, "#000"); rr(x, y, w, h, h / 2, "#fff"); g.fillStyle = "#111"; tx(x + padx * k);
    } else if (nk.style === "bold") {
      g.font = `italic ${wt} ${size * k}px "${font}"`;
      const yy = ty;
      g.fillStyle = "#000"; g.fillText(text, x + (padx + 5) * k, yy + 5 * k);
      g.lineJoin = "round"; g.lineWidth = 8 * k; g.strokeStyle = "#000"; g.strokeText(text, x + padx * k, yy);
      g.fillStyle = "#ffd400"; g.fillText(text, x + padx * k, yy);
    } else {
      g.translate(x + w / 2, y + h / 2); g.rotate(-4 * Math.PI / 180); g.translate(-(x + w / 2), -(y + h / 2));
      rr(x + 7 * k, y + 7 * k, w, h, 2 * k, "#111"); rr(x, y, w, h, 2 * k, "#ffd400"); g.fillStyle = "#111"; tx(x + padx * k);
    }
    g.restore();
  }

  function renderStream() {
    const on = st.opts.frame === "stream";
    $("vxStream").hidden = !on;
    $("vxView").hidden = !on;
    if (!on) st.view = "phone";
    document.querySelectorAll("#vxView button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === st.view)));
    const fed = st.view === "frame";
    $("vxFed").hidden = !fed;
    $("vxPhone").hidden = fed;
    const s = st.stream;
    let html = "";
    if (on && !s) html = "Шукаю вебку на стрімі…";
    else if (s && s.cam) html = s.auto === false ? "<b>✓ Рамку вебки виставлено вручну.</b> Одна на всі кліпи цього відео."
      : "<b>✓ Вебку знайдено.</b> Перевір рамку на кадрі; одна на всі кліпи цього відео.";
    else if (s) html = s.auto === false ? "<b class=\"no\">Вебку вимкнено:</b> на телефоні лише гра."
      : `<b class="no">Вебку не знайдено.</b> ${escapeText(s.note || "")} Натисни «Рамка вебки» і познач її на кадрі.`;
    $("vxCamSt").innerHTML = html;
    $("vxCamOff").disabled = !(s && s.cam);
    renderLook();
    placeBoxes();
  }

  /** Sliders and styling controls show the look in effect (the clip's own or the video's). */
  function renderLook() {
    const lk = look(), hasCam = !!(st.stream && st.stream.cam), pip = lk.layout === "pip";
    $("vxLay").hidden = !hasCam;
    document.querySelectorAll("#vxLay button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === lk.layout)));
    $("vxRoundRow").hidden = !hasCam || !pip;
    $("vxDivRow").hidden = !hasCam || pip;
    $("vxFrameRow").hidden = !hasCam || (!pip && !lk.divider);
    $("vxFrameLbl").textContent = pip ? "Рамка вікна" : "Колір лінії";
    document.querySelectorAll("#vxCamRound button").forEach((b) => b.setAttribute("aria-pressed", String((b.dataset.v === "1") === lk.round)));
    document.querySelectorAll("#vxCamFrame button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === lk.frame)));
    document.querySelectorAll("#vxNickStyle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === lk.nick_style)));
    $("vxCamDiv").checked = lk.divider;
    if (document.activeElement !== $("vxNick")) $("vxNick").value = lk.nick;
    $("vxLookOwn").checked = !!st.opts.stream_look;
    renderSel();
  }

  /** Corner handles of a box on the phone; at the phone's edge they move in, past its rounded corners. */
  const HANDLE_IN = 52;
  function handlePoints([px, py, pw, ph]) {
    const cx = (v) => clampN(v, HANDLE_IN, OW - HANDLE_IN), cy = (v) => clampN(v, HANDLE_IN, OH - HANDLE_IN);
    return [["nw", cx(px), cy(py)], ["ne", cx(px + pw), cy(py)], ["sw", cx(px), cy(py + ph)], ["se", cx(px + pw), cy(py + ph)]];
  }

  /** The box with handles over the selected picture on the phone (click the webcam or the game). */
  function renderSel() {
    const el = $("vxSel");
    const sp = curSp();
    const part = sp && st.sel && st.view === "phone" ? sp.parts.find((p) => p.role === st.sel) : null;
    el.hidden = !part || (!ctx.video().paused && !st.gesture);
    if (el.hidden) return;
    const [x, y, w, h] = part.dst;
    Object.assign(el.style, { left: `${(x / OW) * 100}%`, top: `${(y / OH) * 100}%`, width: `${(w / OW) * 100}%`, height: `${(h / OH) * 100}%` });
    el.classList.toggle("game", part.role === "game");
    el.classList.toggle("attop", y <= 0);  // the chip goes under the top handles, never over them
    for (const [name, hx, hy] of handlePoints(part.dst)) {
      const i = el.querySelector(`i[data-h="${name}"]`);
      i.style.left = `calc(${((hx - x) / Math.max(1, w)) * 100}% - 8px)`;
      i.style.top = `calc(${((hy - y) / Math.max(1, h)) * 100}% - 8px)`;
    }
    const lk = look();
    $("vxSelName").textContent = part.role === "cam" ? (sp.layout === "pip" ? "Вікно з вебкою" : "Вебка") : "Гра";
    let pct = `${Math.round((part.role === "cam" ? lk.cam_zoom : lk.game_zoom) * 100)}%`;
    if (part.role === "cam" && (sp.layout === "pip" || sp.layout === "full")) pct = `${Math.round((w / OW) * 100)}% ширини`;
    else if (part.role === "game" && sp.layout === "full") pct = `${Math.round((part.win[2] / OW) * 100)}% ширини`;
    else if (part.role === "game" && sp.layout === "pip") {
      // the window's width against the phone's; the zoom inside it, if any
      pct = (lk.game_w === null ? "на весь екран" : `${Math.round((part.win[2] / OW) * 100)}% ширини`) + (lk.game_zoom > 1 ? ` · наближення ${Math.round(lk.game_zoom * 100)}%` : "");
    }
    $("vxSelPct").textContent = pct;
  }

  /** «↺» on the box: the selected picture as at first (styling and the other picture stay). */
  function resetSel() {
    const d = LOOK_DEFAULT;
    const sp = curSp();
    if (sp && sp.layout === "full") {
      // that window back where the preset puts it; the other stays
      const fw = { ...(look().full_win || {}) };
      Object.keys(fw).filter((k) => k.startsWith(st.sel === "cam" ? "cam_" : "game_")).forEach((k) => delete fw[k]);
      commitLook(st.sel === "cam" ? { cam_zoom: 1, cam_fx: null, cam_fy: null, full_win: fw } : { game_zoom: 1, game_x: 0.5, game_y: 0.5, full_win: fw });
      return;
    }
    if (st.sel === "cam") commitLook({ cam_zoom: 1, cam_fx: null, cam_fy: null, share: d.share, pip_x: d.pip_x, pip_y: d.pip_y, pip_w: d.pip_w });
    else if (st.sel === "game") commitLook({ game_zoom: 1, game_x: 0.5, game_y: 0.5, game_w: null, game_cx: 0.5, game_cy: 0.5 });
  }

  /** While dragging a slider or a picture: preview only. */
  function liveLook(patch) {
    st.live = { ...look(), ...patch };
    afterLook();
  }
  /** Keeps the change: in this clip (own look) or in the video (every clip without its own). */
  async function commitLook(patch) {
    const lk = cleanLook({ ...look(), ...patch });
    SIZE_KEYS.forEach((k) => { if (typeof lk[k] === "number") lk[k] = r4(lk[k]); });
    st.live = null;
    if (st.opts.stream_look) {
      st.opts.stream_look = lk;
      save();
      afterLook();
      return;
    }
    if (st.stream) st.stream = { ...st.stream, look: lk }; // show it right away; the server answer replaces it
    afterLook();
    const seq = (st.lookSeq = (st.lookSeq || 0) + 1);
    try {
      const got = await ctx.api(`/api/projects/${ctx.pid()}/stream`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep_cam: true, look: lk }),
      });
      if (seq !== st.lookSeq) return; // a newer change is on its way: its answer wins
      st.stream = got;
      st.streamPid = ctx.pid();
    } catch (e) {
      if (seq === st.lookSeq) ctx.toast("Не вдалося зберегти вигляд стріму: " + e.message, true);
    }
    afterLook();
  }
  function afterLook() {
    renderLook();
    const sp = curSp();
    if (sp && st.opts.pos == null && sp.pos != null) { $("vxPos").value = sp.pos; $("vxCap").style.top = `${sp.pos}%`; }
    if (sp && st.opts.hook_pos == null) { const hp = sp.hook_pos ?? 9.9; $("vxHookPos").value = hp; $("vxHook").style.top = `${hp}%`; }
    placeBoxes();
  }

  function setOwnLook(on) {
    st.opts.stream_look = on ? look() : null;
    save();
    renderStream();
  }

  /** Puts the webcam frame and the game area over the 16:9 picture (percentages of the frame). */
  function placeBoxes() {
    if ($("vxFed").hidden) return;
    const s = st.stream, cb = $("vxCamB"), gb = $("vxGameB");
    const put = (el, r) => { el.style.left = `${r[0] * 100}%`; el.style.top = `${r[1] * 100}%`; el.style.width = `${r[2] * 100}%`; el.style.height = `${r[3] * 100}%`; };
    const cam = st.drag && st.drag.cam ? st.drag.cam : s && s.cam;
    cb.hidden = !cam;
    if (cam) { put(cb, cam); cb.classList.toggle("low", cam[1] < 0.08); }
    const sp = curSp();
    const game = sp && sp.parts.find((p) => p.role === "game");
    const whole = !game || (game.src[2] > 0.99 && game.src[3] > 0.99);
    gb.hidden = whole;
    if (!whole) put(gb, game.src);
  }

  function drawSource() {
    const cv = $("vxSrc"), v = ctx.video();
    if (!v.videoWidth || v.readyState < 2) return;
    const g = cv.getContext("2d");
    g.drawImage(v, 0, 0, cv.width, cv.height);
  }

  async function saveStream(body) {
    try {
      st.stream = await ctx.api(`/api/projects/${ctx.pid()}/stream`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      st.streamPid = ctx.pid();
    } catch (e) {
      ctx.toast("Не вдалося зберегти рамку: " + e.message, true);
    }
    st.drag = null;
    await loadPlan(true);
  }

  async function redetect() {
    const btn = $("vxCamFind");
    btn.disabled = true;
    $("vxCamSt").textContent = "Шукаю вебку на стрімі… (до 10 с)";
    try {
      st.stream = await ctx.api(`/api/projects/${ctx.pid()}/stream/redetect`, { method: "POST" });
      st.streamPid = ctx.pid();
    } catch (e) {
      ctx.toast("Не вдалося знайти вебку: " + e.message, true);
    }
    btn.disabled = false;
    await loadPlan(true);
  }

  function setView(v) {
    st.view = v;
    renderStream();
  }

  /** «Рамки на кадрі»: dragging the webcam frame (move or a corner) and the game area. */
  function startDrag(e) {
    const fed = $("vxFed");
    const r = fed.getBoundingClientRect();
    const handle = e.target.closest("i[data-h]");
    const onCam = e.target.closest("#vxCamB");
    const onGame = e.target.closest("#vxGameB");
    if (!onCam && !onGame) return;
    e.preventDefault();
    const x0 = (e.clientX - r.left) / r.width, y0 = (e.clientY - r.top) / r.height;
    const cam0 = st.stream && st.stream.cam ? [...st.stream.cam] : null;
    const sp = curSp();
    const game = sp && sp.parts.find((p) => p.role === "game");
    const g0 = game ? [game.src[0] + game.src[2] / 2, game.src[1] + game.src[3] / 2] : [0.5, 0.5];
    const mode = onCam ? (handle ? handle.dataset.h : "move") : "game";
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const move = (ev) => {
      const dx = (ev.clientX - r.left) / r.width - x0, dy = (ev.clientY - r.top) / r.height - y0;
      if (mode === "game") { liveLook({ game_x: clamp(g0[0] + dx, 0, 1), game_y: clamp(g0[1] + dy, 0, 1) }); return; }
      let [x, y, w, h] = cam0;
      if (mode === "move") { x = clamp(x + dx, 0, 1 - w); y = clamp(y + dy, 0, 1 - h); }
      else {
        let x1 = x + w, y1 = y + h;
        if (mode.includes("w")) x = clamp(x + dx, 0, x1 - 0.04);
        if (mode.includes("e")) x1 = clamp(x1 + dx, x + 0.04, 1);
        if (mode.includes("n")) y = clamp(y + dy, 0, y1 - 0.04);
        if (mode.includes("s")) y1 = clamp(y1 + dy, y + 0.04, 1);
        w = x1 - x; h = y1 - y;
      }
      st.drag = { cam: [x, y, w, h] };
      placeBoxes();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (mode === "game") { if (st.live) commitLook({ game_x: st.live.game_x, game_y: st.live.game_y }); return; }
      const d = st.drag;
      if (d && d.cam) saveStream({ cam: d.cam.map(r4) });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // ─── Direct manipulation on the phone ──────────────────────────────────────
  // Click the webcam or the game → a box with handles. A corner: bigger / smaller (the window in «Гра на весь екран»,
  // the zoom elsewhere); inside: move what is shown; the edge between webcam and game: the webcam's height; the nick: drag it.
  const HANDLE = 48;  // px of the 1080×1920 phone around a corner handle

  /** What is under the pointer on the phone (1080×1920 coordinates). */
  function phoneHit(x, y) {
    const sp = curSp();
    if (!sp || st.view !== "phone") return null;
    const cam = sp.parts.find((p) => p.role === "cam"), game = sp.parts.find((p) => p.role === "game");
    const inside = (r) => r && x >= r[0] && x <= r[0] + r[2] && y >= r[1] && y <= r[1] + r[3];
    const nk = sp.nick;
    if (nk && inside([nk.x - 10, nk.y - 10, nk.w + 20, nk.h + 20])) return { mode: "nick" };
    const sel = st.sel && sp.parts.find((p) => p.role === st.sel);
    if (sel) {
      const [px, py, pw, ph] = sel.dst;
      for (const [h, hx, hy] of handlePoints(sel.dst)) {
        if (Math.abs(x - hx) < HANDLE && Math.abs(y - hy) < HANDLE) return { mode: "corner", h, part: sel };
      }
    }
    const free = sp.layout === "pip" || sp.layout === "full";  // pictures are windows: move them, resize by a corner
    if (cam && sp.layout === "top" && Math.abs(y - cam.dst[3]) < 30) return { mode: "seam", part: cam };
    if (cam && free && inside(cam.dst)) return { mode: "pipmove", part: cam };
    if (inside(cam && cam.dst)) return { mode: "cam", part: cam };
    if (inside(game && game.dst)) return { mode: free ? "gamemove" : "game", part: game };
    return null;
  }
  const CURSOR = { seam: "ns-resize", corner: "nwse-resize", pipmove: "move", gamemove: "move", cam: "grab", game: "grab", nick: "move" };
  function phoneXY(e) {
    const r = $("vxCanvas").getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * OW, ((e.clientY - r.top) / r.height) * OH];
  }

  const fullNowOrPip = (sp) => sp.layout === "pip" || sp.layout === "full";
  /** «Вебка + вся гра»: where both windows are now, as full_win keys (so moving one keeps the other in place). */
  function fullWinOf(sp) {
    const cam = sp.parts.find((p) => p.role === "cam"), game = sp.parts.find((p) => p.role === "game");
    const [wx, wy, ww, wh] = game.win;
    return { cam_w: cam.dst[2] / OW, cam_x: cam.dst[0] / OW, cam_y: cam.dst[1] / OH,
      game_w: ww / OW, game_cx: (wx + ww / 2) / OW, game_cy: (wy + wh / 2) / OH };
  }

  function phoneDown(e) {
    if (e.button !== 0 || !streamOn()) return;
    const [x0, y0] = phoneXY(e);
    const hit = phoneHit(x0, y0);
    const before = st.sel;
    if (!hit) { st.sel = null; renderSel(); return; }
    if (hit.part) st.sel = hit.part.role;
    const picked = st.sel !== before;  // the first click only selects; a click on the selected picture plays/pauses
    renderSel();
    const ph = $("vxPhone");
    try { ph.setPointerCapture(e.pointerId); } catch (_) {}
    const sp = curSp(), lk = look(), cam = st.stream.cam;
    const p = hit.part;
    const start = { dst: p ? [...p.dst] : null, src: p ? [...p.src] : null, nick: sp.nick ? [sp.nick.x, sp.nick.y, sp.nick.w, sp.nick.h] : null,
      zoom: p ? (p.role === "cam" ? lk.cam_zoom : lk.game_zoom) : 1 };
    if (p) {
      const [sx, sy, sw, sh] = p.src;
      start.fx = cam ? (sx + sw / 2 - cam[0]) / cam[2] : 0.5;
      start.fy = cam ? (sy + sh * 0.45 - cam[1]) / cam[3] : 0.5;
      start.gx = sx + sw / 2; start.gy = sy + sh / 2;
    }
    st.gesture = { moved: false };
    // «Вебка + вся гра» keeps its windows apart from «Гра на весь екран»: the same moves, written into full_win
    const fullNow = sp.layout === "full" ? fullWinOf(sp) : null;
    const liveWin = (patch) => {
      if (!fullNow) return liveLook(patch);
      const names = { pip_w: "cam_w", pip_x: "cam_x", pip_y: "cam_y", game_w: "game_w", game_cx: "game_cx", game_cy: "game_cy" };
      const fw = { ...fullNow };
      for (const [k, v] of Object.entries(patch)) fw[names[k]] = v;
      liveLook({ full_win: fw });
    };
    const move = (ev) => {
      const [x, y] = phoneXY(ev);
      const dx = x - x0, dy = y - y0;
      if (!st.gesture.moved && Math.hypot(dx, dy) < 12) return;
      st.gesture.moved = true;
      ph.style.cursor = hit.mode === "cam" || hit.mode === "game" ? "grabbing" : CURSOR[hit.mode];
      const [px, py, pw, phh] = start.dst || [0, 0, 1, 1];
      if (hit.mode === "nick") {
        const [nx, ny, nw, nh] = start.nick;
        liveLook({ nick_x: clampN(nx + dx, 0, OW - nw) / OW, nick_y: clampN(ny + dy, 0, OH - nh) / OH });
      } else if (hit.mode === "seam") liveLook({ share: clampN(y / OH, 0.22, 0.6) });
      else if (hit.mode === "cam") {
        const [, , sw, sh] = start.src;
        liveLook({ cam_fx: clampN(start.fx - (dx / pw) * sw / cam[2], 0, 1), cam_fy: clampN(start.fy - (dy / phh) * sh / cam[3], 0, 1) });
      } else if (hit.mode === "game") {
        const [, , sw, sh] = start.src;
        liveLook({ game_x: clampN(start.gx - (dx / pw) * sw, 0, 1), game_y: clampN(start.gy - (dy / phh) * sh, 0, 1) });
      } else if (hit.mode === "gamemove") {
        const [wx, wy, ww, wh] = p.win;
        liveWin({ game_w: ww / OW, game_cx: (wx + dx + ww / 2) / OW, game_cy: (wy + dy + wh / 2) / OH });
      } else if (hit.mode === "corner" && p.role === "game" && fullNowOrPip(sp)) {
        // the game window grows or shrinks around the opposite corner of what is seen of it
        const [wx, wy, ww, wh] = p.win;
        const ax = hit.h.includes("e") ? px : px + pw, ay = hit.h.includes("s") ? py : py + phh;
        const k = clampN(Math.hypot(x - ax, y - ay) / Math.max(1, Math.hypot(x0 - ax, y0 - ay)), (0.2 * OW) / ww, (4 * OW) / ww);
        const nx = ax + (wx - ax) * k, ny = ay + (wy - ay) * k;
        liveWin({ game_w: (ww * k) / OW, game_cx: (nx + ww * k / 2) / OW, game_cy: (ny + wh * k / 2) / OH });
      } else if (hit.mode === "pipmove") liveWin({ pip_x: clampN(px + dx, 0, OW - pw) / OW, pip_y: clampN(py + dy, 0, OH - phh) / OH });
      else if (hit.mode === "corner" && p.role === "cam" && fullNowOrPip(sp)) {
        const b = (p.border || 0) * 2, east = hit.h.includes("e"), south = hit.h.includes("s");
        const ow = clampN(pw + (east ? dx : -dx), OW * 0.25 + b, OW);
        const oh = b + (ow - b) * (phh - b) / (pw - b);
        liveWin({ pip_w: (ow - b) / OW, pip_x: clampN(east ? px : px + pw - ow, 0, OW - ow) / OW, pip_y: clampN(south ? py : py + phh - oh, 0, OH - oh) / OH });
      } else if (hit.mode === "corner") {
        // a corner pulled away from the middle of the picture zooms in, towards it zooms out
        const cx = px + pw / 2, cy = py + phh / 2;
        const z = clampN(start.zoom * Math.hypot(x - cx, y - cy) / Math.max(1, Math.hypot(x0 - cx, y0 - cy)), 1, 3);
        liveLook({ [p.role === "cam" ? "cam_zoom" : "game_zoom"]: Math.round(z * 100) / 100 });
      }
    };
    const up = () => {
      ph.removeEventListener("pointermove", move);
      ph.removeEventListener("pointerup", up);
      ph.removeEventListener("pointercancel", up);
      ph.style.cursor = "";
      const moved = st.gesture && st.gesture.moved;
      st.gesture = null;
      if (moved || picked) st.suppressClick = true;
      if (moved) {
        const lk2 = st.live;
        st.live = null;
        if (lk2) commitLook(lk2);
      }
      renderSel();
    };
    ph.addEventListener("pointermove", move);
    ph.addEventListener("pointerup", up);
    ph.addEventListener("pointercancel", up);
  }

  function phoneHover(e) {
    if (st.gesture) return;
    const ph = $("vxPhone");
    const hit = streamOn() && e.buttons === 0 ? phoneHit(...phoneXY(e)) : null;
    let c = hit ? CURSOR[hit.mode] : "";
    if (hit && hit.mode === "corner") c = hit.h === "nw" || hit.h === "se" ? "nwse-resize" : "nesw-resize";
    if (hit && (hit.mode === "cam" || hit.mode === "game" || hit.mode === "gamemove") && hit.part.role !== st.sel) c = "pointer";
    ph.style.cursor = c;
  }

  /** Mouse wheel over the webcam or the game: zoom (kept 0.5 s after the last notch). */
  function phoneWheel(e) {
    if (!streamOn()) return;
    const hit = phoneHit(...phoneXY(e));
    if (!hit || !hit.part) return;
    e.preventDefault();
    st.sel = hit.part.role;
    const key = hit.part.role === "cam" ? "cam_zoom" : "game_zoom";
    const lk = look();
    liveLook({ [key]: clampN(Math.round((lk[key] + (e.deltaY < 0 ? 0.1 : -0.1)) * 100) / 100, 1, 3) });
    clearTimeout(st.wheelTimer);
    st.wheelTimer = setTimeout(() => { const l = st.live; st.live = null; if (l) commitLook(l); }, 500);
  }

  function loop() {
    st.raf = 0;
    if ($("edVx").hidden || $("editor").hidden) return;
    if (st.view === "frame") drawSource(); else draw();
    st.raf = requestAnimationFrame(loop);
  }
  function startLoop() { if (!st.raf) st.raf = requestAnimationFrame(loop); }

  // ─── Rendering & downloads ─────────────────────────────────────────────────
  async function renderAndDownload(btn, subs) {
    if (btn.classList.contains("busy")) return;
    const c = ctx.clip();
    const label = btn.innerHTML;
    btn.classList.add("busy");
    const t0 = Date.now();
    const tick = () => { btn.innerHTML = `⏳ Збираю відео… ${Math.round((Date.now() - t0) / 1000)} с`; };
    tick();
    const h = setInterval(tick, 1000);
    try {
      if (!Object.keys(c.vertical_opts || {}).length) save();  // a clip nobody touched: render what the phone shows
      await flush();
      await ctx.api(`/api/projects/${ctx.pid()}/clips/${c.clip_index}/vertical?subs=${subs}`, { method: "POST" });
      ctx.download(`/api/projects/${ctx.pid()}/clips/${c.clip_index}/vertical?subs=${subs}`);
      ctx.toast("Готово, скачую");
    } catch (e) {
      ctx.toast("Не вдалося зібрати відео: " + e.message, true);
    } finally {
      clearInterval(h);
      btn.classList.remove("busy");
      btn.innerHTML = label;
    }
  }

  async function startZip() {
    const btn = $("vxZip");
    if (st.zipJob) return;
    try {
      await flush();
      const job = await ctx.api(`/api/projects/${ctx.pid()}/export-vertical-zip`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subs: true }),
      });
      st.zipJob = job.id;
      btn.classList.add("busy");
      $("vxProg").hidden = false;
      pollZip();
    } catch (e) {
      ctx.toast(e.message, true);
    }
  }

  async function pollZip() {
    const id = st.zipJob;
    if (!id) return;
    try {
      const j = await ctx.api(`/api/vertical-jobs/${id}`);
      $("vxProgBar").style.width = `${Math.round((j.done / Math.max(1, j.total)) * 100)}%`;
      $("vxZipInfo").textContent = `${j.done} з ${j.total}`;
      if (j.status === "running") { setTimeout(pollZip, 1500); return; }
      if (j.status === "completed") { ctx.download(`/api/vertical-jobs/${id}/file`); ctx.toast("Архів готовий"); }
      else ctx.toast("Архів не вдалося зібрати: " + (j.error || ""), true);
    } catch (e) {
      ctx.toast(e.message, true);
    }
    st.zipJob = null;
    $("vxZip").classList.remove("busy");
    setTimeout(() => { $("vxProg").hidden = true; $("vxProgBar").style.width = "0"; $("vxZipInfo").textContent = ""; }, 1500);
  }

  // ─── Public ────────────────────────────────────────────────────────────────
  /** Called when the selected clip changes or its bounds change. */
  function onClip() {
    const c = ctx && ctx.clip();
    if (!c) return;
    flush();
    st.opts = optsFor(c);
    st.plan = null;
    st.planKey = "";
    st.words = [];
    st.selWord = null;
    if (st.streamPid !== ctx.pid()) { st.stream = null; st.streamPid = ""; }
    st.live = null;
    autoStream();
    $("vxWedit").hidden = true;
    renderControls();
    renderNote();
    renderWordChips();
    if (!$("edVx").hidden) { loadPlan(); startLoop(); }
  }

  function onShow() {
    if ($("edVx").hidden || !ctx.clip()) return;
    if (!st.opts) onClip();
    autoStream();
    loadPlan();
    startLoop();
  }

  function attach(context) {
    ctx = context;
    // the nick is measured with the same fonts the render uses: load them before the first measure
    if (document.fonts && document.fonts.load) {
      Promise.all(Object.values(NICK_STYLES).map(([wt, font, size]) => document.fonts.load(`${wt} ${size}px "${font}"`).catch(() => null)))
        .then(() => { st.spMemo = null; st.fontsReady = true; });
    }
    $("vxStyle").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("style", b.dataset.v); });
    $("vxFrame").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("frame", b.dataset.v); });
    $("vxLay").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) commitLook({ layout: b.dataset.v }); });
    $("vxCamRound").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) commitLook({ round: b.dataset.v === "1" }); });
    $("vxCamFrame").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) commitLook({ frame: b.dataset.v }); });
    $("vxCamDiv").addEventListener("change", (e) => commitLook({ divider: e.target.checked }));
    $("vxNick").addEventListener("input", (e) => liveLook({ nick: e.target.value }));
    $("vxNick").addEventListener("change", (e) => commitLook({ nick: e.target.value }));
    $("vxSmOk").addEventListener("click", closeModal);
    $("vxSmNo").addEventListener("click", modalNotStream);
    $("vxSmFix").addEventListener("click", () => { closeModal(); document.querySelector("#vxTabs button[data-t='frame']")?.click(); setView("frame"); });
    $("vxSmSkip").addEventListener("click", () => { st.modalSkipped = true; closeModal(); });
    $("vxStreamModal").addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); if ($("vxStreamModal").dataset.mode === "checking") st.modalSkipped = true; closeModal(); }
    });
    $("vxNickStyle").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) commitLook({ nick_style: b.dataset.v }); });
    $("vxSelReset").addEventListener("click", (e) => { e.stopPropagation(); resetSel(); });
    $("vxSelReset").addEventListener("pointerdown", (e) => e.stopPropagation());
    $("vxNick").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); e.stopPropagation(); });
    $("vxLookOwn").addEventListener("change", (e) => setOwnLook(e.target.checked));
    $("vxLookReset").addEventListener("click", () => {
      const d = {};
      SIZE_KEYS.forEach((k) => { d[k] = LOOK_DEFAULT[k]; });
      commitLook(d);
    });
    $("vxPhone").addEventListener("pointerdown", phoneDown);
    $("vxPhone").addEventListener("pointermove", phoneHover);
    $("vxPhone").addEventListener("wheel", phoneWheel, { passive: false });
    $("vxView").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView(b.dataset.v); });
    $("vxCamFind").addEventListener("click", redetect);
    $("vxCamOff").addEventListener("click", () => saveStream({ cam: null }));
    $("vxCamMark").addEventListener("click", () => {
      if (st.stream && !st.stream.cam) saveStream({ cam: [0.02, 0.66, 0.26, 0.3] });
      setView("frame");
    });
    $("vxFed").addEventListener("pointerdown", startDrag);
    $("vxPos").addEventListener("input", (e) => { $("vxCap").style.top = `${e.target.value}%`; });
    $("vxPos").addEventListener("change", (e) => setOpt("pos", Number(e.target.value)));
    $("vxHookOn").addEventListener("change", (e) => setOpt("hook", e.target.checked));
    $("vxSize").addEventListener("input", (e) => { $("vxPhone").style.setProperty("--vx-s", e.target.value / 100); $("vxSizeV").textContent = `${e.target.value}%`; st.capKey = ""; });
    $("vxSize").addEventListener("change", (e) => setOpt("size", Number(e.target.value) / 100));
    $("vxHookSize").addEventListener("input", (e) => $("vxPhone").style.setProperty("--vx-hs", e.target.value / 100));
    $("vxHookPos").addEventListener("input", (e) => { $("vxHook").style.top = `${e.target.value}%`; });
    $("vxHookPos").addEventListener("change", (e) => setOpt("hook_pos", Number(e.target.value)));
    $("vxHookSize").addEventListener("change", (e) => setOpt("hook_size", Number(e.target.value) / 100));
    $("vxColor").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("color", b.dataset.v || null); });
    $("vxText").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("text", b.dataset.v || null); });
    $("vxHookStyle").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("hook_style", b.dataset.v); });
    const seg = (id, key, conv) => $(id).addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt(key, conv(b.dataset.v)); });
    seg("vxRound", "round", (v) => v === "1");
    seg("vxHookRound", "hook_round", (v) => v === "1");
    seg("vxStroke", "stroke", (v) => v || null);
    seg("vxHookStroke", "hook_stroke", (v) => v || null);
    $("vxHookDur").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("hook_full", b.dataset.v === "all"); });
    $("vxHookText").addEventListener("input", (e) => { $("vxHook").innerHTML = `<span>${escapeText(e.target.value)}</span>`; });
    $("vxHookText").addEventListener("change", (e) => {
      const v = e.target.value.trim();
      setOpt("hook_text", v && v !== (ctx.clip().title || "") ? v : null);
    });
    $("vxHookText").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); e.stopPropagation(); });
    // Scrub through the clip under the preview
    const sc = $("vxScrub");
    sc.addEventListener("input", () => { st.scrubbing = true; ctx.seek(ctx.clip().start_sec + Number(sc.value)); });
    sc.addEventListener("change", () => { st.scrubbing = false; });
    $("vxWords").addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) openWordEdit(Number(b.dataset.i)); });
    $("vxWsave").addEventListener("click", () => applyWordEdit("save"));
    $("vxWdel").addEventListener("click", () => applyWordEdit("del"));
    $("vxWreset").addEventListener("click", () => applyWordEdit("reset"));
    $("vxWinput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); applyWordEdit("save"); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeWordEdit(); }
    });
    $("vxDlSubs").addEventListener("click", (e) => renderAndDownload(e.currentTarget, true));
    $("vxDlClean").addEventListener("click", (e) => renderAndDownload(e.currentTarget, false));
    $("vxZip").addEventListener("click", startZip);
    $("vxZipClean").addEventListener("click", () => {
      if (!ctx.keptCount()) { ctx.toast("Спершу познач кліпи «В роботі».", true); return; }
      ctx.toast("Готуємо архів… Великі архіви можуть зайняти хвилину.");
      ctx.download(`/api/projects/${ctx.pid()}/export-zip`);
    });
    const toggle = () => { if (st.suppressClick) { st.suppressClick = false; return; } ctx.togglePlay(); };
    $("vxPhone").addEventListener("click", toggle);
    $("vxPhone").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); toggle(); } });
    $("vxPlay").addEventListener("click", toggle);
    const onPlay = () => {
      const paused = ctx.video().paused;
      $("vxPlay").textContent = paused ? "▶" : "❚❚";
      $("vxPlay").setAttribute("aria-label", paused ? "Грати" : "Пауза");
      $("vxPhone").classList.toggle("playing", !paused);
      renderSel();
    };
    ctx.video().addEventListener("play", onPlay);
    ctx.video().addEventListener("pause", onPlay);
  }

  /** Read-only snapshot for UI tests. */
  function state() {
    return { opts: st.opts, layout: st.plan && st.plan.layout.kind, words: st.words.length, cap: $("vxCap").textContent,
      stream: curSp(), server: st.plan && st.plan.layout.stream, cam: st.stream && st.stream.cam, view: st.view,
      look: st.opts && st.opts.frame === "stream" ? look() : null };
  }

  /** The preview's plan for a look (tests compare it with streamer.plan). */
  function twin(lk) { return st.stream ? streamPlan(st.stream.cam, st.stream.face, st.plan.src.w, st.plan.src.h, lk) : null; }

  return { attach, onClip, onShow, state, twin };
})();
