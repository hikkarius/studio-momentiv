/**
 * vertical.js — the «📱 Вертикальні кліпи» screen of the editor.
 * Live preview: draws the editor's own <video> into a 9:16 canvas with the server's crop plan
 * (face tracking / blurred fit / two people stacked) and lays the caption style over it, synced
 * to the playhead. Settings are saved per clip (vertical_opts) and remembered as the template for
 * clips you haven't touched yet. Rendering itself happens on the server (ffmpeg + .ass).
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
  const st = { opts: null, plan: null, planKey: "", words: [], loading: false, raf: 0, saveTimer: null, pending: null, selWord: null, zipJob: null, capKey: "" };

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
  function writeTemplate(o) {
    const t = {};
    TEMPLATE_FIELDS.forEach((k) => { t[k] = o[k]; });
    try { localStorage.setItem(TEMPLATE_KEY, JSON.stringify(t)); } catch (_) {}
  }

  function optsFor(clip) {
    const own = clip.vertical_opts || {};
    const base = { style: "hl", frame: "auto", pos: null, hook: false, edits: {}, size: 1, color: null, text: null,
      hook_text: null, hook_style: "white", hook_size: 1, hook_full: false,
      round: false, stroke: null, hook_round: false, hook_stroke: null, hook_pos: null };
    const o = Object.keys(own).length ? { ...base, ...own, edits: { ...(own.edits || {}) } } : { ...base, ...readTemplate(), edits: {} };
    if (o.frame === "track" || o.frame === "split") o.frame = "auto"; // these now live inside «Авто»
    return o;
  }

  // ─── Controls ──────────────────────────────────────────────────────────────
  function renderControls() {
    const o = st.opts;
    $("vxStyle").innerHTML = STYLES.map((s) => `<button type="button" data-v="${s.id}" aria-pressed="${s.id === o.style}">` +
      `<span class="vx-tile vx-t-${s.id}" aria-hidden="true">${TILE[s.id]}</span><b>${s.name}</b></button>`).join("");
    document.querySelectorAll("#vxFrame button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === (o.frame === "blur" ? "blur" : "auto"))));
    const split = st.plan && st.plan.layout.kind === "split";
    $("vxPos").value = o.pos ?? (split ? 50 : styleOf(o.style).pos || 66);
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
    $("vxHookPos").value = o.hook_pos ?? 9.9;
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
    hook.style.top = `${o.hook_pos ?? 9.9}%`;
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
      const kind = { track: "Кадр слідкує за обличчям.", split: "У кадрі двоє: ділю екран навпіл.", blur: "Показую весь кадр на розмитому тлі.", center: "Кадр по центру." }[p.layout.kind];
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
    if (k === "frame") loadPlan(true);
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
    const key = `${c.clip_index}:${c.start_sec}:${c.end_sec}:${st.opts.frame}`;
    if (!force && key === st.planKey && st.plan) return;
    st.planKey = key;
    $("vxBusy").hidden = false;
    $("vxBusy").textContent = "Шукаю обличчя…";
    try {
      const plan = await ctx.api(`/api/projects/${ctx.pid()}/clips/${c.clip_index}/vertical-plan?frame=${st.opts.frame}`);
      if (st.planKey !== key) return;
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

  function loop() {
    st.raf = 0;
    if ($("edVx").hidden || $("editor").hidden) return;
    draw();
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
    $("vxWedit").hidden = true;
    renderControls();
    renderNote();
    renderWordChips();
    if (!$("edVx").hidden) { loadPlan(); startLoop(); }
  }

  function onShow() {
    if ($("edVx").hidden || !ctx.clip()) return;
    if (!st.opts) onClip();
    loadPlan();
    startLoop();
  }

  function attach(context) {
    ctx = context;
    $("vxStyle").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("style", b.dataset.v); });
    $("vxFrame").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setOpt("frame", b.dataset.v); });
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
    const toggle = () => ctx.togglePlay();
    $("vxPhone").addEventListener("click", toggle);
    $("vxPhone").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); toggle(); } });
    $("vxPlay").addEventListener("click", toggle);
    const onPlay = () => {
      const paused = ctx.video().paused;
      $("vxPlay").textContent = paused ? "▶" : "❚❚";
      $("vxPlay").setAttribute("aria-label", paused ? "Грати" : "Пауза");
      $("vxPhone").classList.toggle("playing", !paused);
    };
    ctx.video().addEventListener("play", onPlay);
    ctx.video().addEventListener("pause", onPlay);
  }

  /** Read-only snapshot for UI tests. */
  function state() {
    return { opts: st.opts, layout: st.plan && st.plan.layout.kind, words: st.words.length, cap: $("vxCap").textContent };
  }

  return { attach, onClip, onShow, state };
})();
