/**
 * settings.js — «Налаштування»: four sections in a side menu (Кліпи, Ключ і розпізнавання, Місце на диску,
 * Про студію). Buttons, cards, the two-thumb length slider and switches all carry data-key and save at once
 * (POST /api/settings); the change applies without restarting the studio. The Gemini key is checked with Google.
 * Depends on app.js globals: showToast, escapeHtml, loadRecentProjects.
 */
const Settings = (() => {
  let view = null;      // last /api/settings answer
  let disk = null;      // last /api/storage answer
  let tab = "clips";
  let savedTimer = null;
  const $ = (id) => document.getElementById(id);
  const sheet = () => $("settingsScreen");

  // ── open / close ──────────────────────────────────────────────────────────

  async function open(which) {
    sheet().hidden = false;
    document.body.style.overflow = "hidden";
    setTab(which || tab);
    await load();
  }

  function close() {
    sheet().hidden = true;
    document.body.style.overflow = "";
  }

  function setTab(name) {
    tab = name;
    sheet().querySelectorAll(".settings-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    sheet().querySelectorAll(".settings-body").forEach((p) => {
      p.hidden = p.dataset.panel !== name;
      if (!p.hidden) $("settingsSection").textContent = p.dataset.title;
    });
    if (sheet().hidden) return;
    if (name === "disk") loadDisk();
    if (name === "ai") { showHardware(); if (keyState === "unknown" && view && view.gemini_key.set) checkSavedKey(); }
    if (name === "about") showAbout();
  }

  // ── values ────────────────────────────────────────────────────────────────

  async function load() {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) throw new Error(await res.text());
      view = await res.json();
      fill();
      applyToHome();
      // the «Ключ і розпізнавання» section may have opened before the settings arrived
      if (tab === "ai" && !sheet().hidden && keyState === "unknown" && view.gemini_key.set) checkSavedKey();
    } catch (err) {
      console.warn("settings", err);
    }
  }

  function get(key) {
    const [sec, k] = key.split(".");
    return view.values[sec][k];
  }

  /** A value set by hand in config.yaml that has no button gets a button of its own, so it shows as chosen. */
  function ensureChoice(group, v) {
    if (group.querySelector(`[data-v="${CSS.escape(String(v))}"]`)) return;
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.v = String(v);
    b.dataset.extra = "1";
    if (group.classList.contains("set-cards")) {
      const label = (view.choices.whisper_model.find((c) => c.value === v) || {}).label || String(v);
      b.innerHTML = `<b>${escapeHtml(label)}</b><small>Вибрано вручну</small>`;
    } else {
      b.textContent = group.dataset.key === "clips.max_overlap_sec" ? `${v} с` : String(v);
    }
    group.appendChild(b);
  }

  function fill() {
    options($("setLang"), view.choices.language);
    sheet().querySelectorAll("[data-key]").forEach((el) => {
      const v = get(el.dataset.key);
      if (el.classList.contains("set-seg") || el.classList.contains("set-cards")) {
        ensureChoice(el, v);
        el.querySelectorAll("[data-v]").forEach((b) => {
          const on = String(Number(b.dataset.v)) === String(v) || b.dataset.v === String(v);
          b.setAttribute("aria-pressed", String(on));
          if (b.getAttribute("role") === "radio") b.setAttribute("aria-checked", String(on));
        });
      } else if (el.type === "checkbox") el.checked = !!v;
      else {
        if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === String(v))) el.add(new Option(String(v), String(v)));
        el.value = String(v);
      }
    });
    renderLength();
    const ov = Number(get("clips.max_overlap_sec"));
    $("setOverlapNote").textContent = ov > 0 ? `Сусідні кліпи можуть мати до ${String(ov).replace(".", ",")} с спільного` : "Кліпи зовсім не перетинаються";
    const days = Number(get("studio.cleanup_days")) || 0;
    $("setAutoOn").checked = days > 0;
    if (days > 0) {
      if (![...$("setAutoDays").options].some((o) => o.value === String(days))) $("setAutoDays").add(new Option(`старші за ${days} днів`, String(days)));
      $("setAutoDays").value = String(days);
    }
    $("setAutoDays").disabled = days === 0;
    $("setAutoNote").textContent = days > 0
      ? `При кожному запуску студії: сміття і файли кліпів, старші за ${days} днів`
      : "Вимкнено. Можна прибирати вручну кнопками вище.";
    renderKey();
  }

  function options(sel, list) {
    if (!sel || sel.options.length) return;
    sel.innerHTML = list.map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`).join("");
  }

  /** Home page picks up the defaults: clip count, search style and the «Шукаю: …» line. */
  function applyToHome() {
    if (!view) return;
    const v = view.values;
    const hc = $("heroClips");
    const hf = $("heroFilter");
    const hl = $("heroLang");
    if (hl) {
      const keep = hl.dataset.touched ? hl.value : null;
      if (hl.options.length !== view.choices.language.length) {
        hl.innerHTML = "";
        options(hl, view.choices.language);
      }
      hl.value = keep || v.whisper.language;
      if (!hl.value) hl.value = "uk";
    }
    if (hc && !hc.dataset.touched) {
      const val = String(v.studio.default_clips);
      if (![...hc.options].some((o) => o.value === val)) hc.add(new Option(val, val));
      hc.value = val;
    }
    if (hf && !hf.dataset.touched) hf.value = v.studio.default_filter;
    const lang = (view.choices.language.find((l) => l.value === v.whisper.language) || {}).label || v.whisper.language;
    if (typeof Home !== "undefined") Home.setDefaults({ lang: lang.toLowerCase(), min: v.clips.default_min_len, max: v.clips.default_max_len });
  }

  // ── clip length: two thumbs on one track + presets ──────────────────────────
  const minIn = () => sheet().querySelector("[data-key='clips.default_min_len']");
  const maxIn = () => sheet().querySelector("[data-key='clips.default_max_len']");

  function renderLength() {
    const lo = Number(minIn().value), hi = Number(maxIn().value);
    const a = Number(minIn().min), z = Number(minIn().max);
    const pos = (v) => ((Math.min(Math.max(v, a), z) - a) / (z - a)) * 100;
    $("setLenFill").style.left = pos(lo) + "%";
    $("setLenFill").style.width = Math.max(0, pos(hi) - pos(lo)) + "%";
    $("setLenBig").textContent = `${lo}–${hi} с`;
    $("setLenPresets").querySelectorAll("button").forEach((b) => {
      b.setAttribute("aria-pressed", String(Number(b.dataset.min) === lo && Number(b.dataset.max) === hi));
    });
  }

  function onLengthInput(e) {
    const gap = 5;
    if (e.target === minIn() && Number(minIn().value) > Number(maxIn().value) - gap) minIn().value = Number(maxIn().value) - gap;
    if (e.target === maxIn() && Number(maxIn().value) < Number(minIn().value) + gap) maxIn().value = Number(minIn().value) + gap;
    renderLength();
  }

  // ── saving ──────────────────────────────────────────────────────────────────
  async function save(patch, okText = "Збережено ✓") {
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(await errorText(res));
      view = await res.json();
      fill();
      applyToHome();
      flash(okText);
      return true;
    } catch (err) {
      fill();
      flash(err.message, true);
      showToast(err.message, "error");
      return false;
    }
  }

  const patchOf = (key, v) => { const [sec, k] = key.split("."); return { [sec]: { [k]: v } }; };

  function onFieldChange(el) {
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "number" || el.type === "range" || el.dataset.type === "int") {
      v = Number(el.value);
      if (el.value === "" || Number.isNaN(v)) { fill(); return; }
    } else v = el.value;
    save(patchOf(el.dataset.key, v));
  }

  function onChoice(e) {
    const b = e.target.closest("[data-v]");
    const group = e.currentTarget;
    if (!b || b.getAttribute("aria-pressed") === "true") return;
    let v = b.dataset.v;
    if (group.dataset.type === "int") v = parseInt(v, 10);
    else if (group.dataset.type === "num") v = Number(v);
    save(patchOf(group.dataset.key, v));
  }

  function onAutoClean() {
    const on = $("setAutoOn").checked;
    save({ studio: { cleanup_days: on ? parseInt($("setAutoDays").value, 10) || 30 : 0 } });
  }

  function flash(text, bad = false) {
    const el = $("settingsSaved");
    el.textContent = text;
    el.classList.toggle("bad", bad);
    el.classList.add("show");
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => el.classList.remove("show"), bad ? 6000 : 1800);
  }

  async function resetAll() {
    if (!confirm("Повернути всі налаштування як було спочатку? Ключ Gemini, відео й кліпи лишаться.")) return;
    const res = await fetch("/api/settings/reset", { method: "POST" });
    if (res.ok) {
      view = await res.json();
      fill();
      applyToHome();
      flash("Повернуто ✓");
    }
  }

  // ── Gemini key ───────────────────────────────────────────────────────────────
  let keyState = "unknown";   // unknown | checking | ok | bad | none
  let keyMsg = "";

  function renderKey() {
    if (!view) return;
    const k = view.gemini_key;
    if (!k.set) { keyState = "none"; keyMsg = "Без ключа студія не зможе шукати моменти. Він безкоштовний і робиться за 2 хвилини."; }
    const T = {
      none: ["!", "Ключа Gemini немає"],
      unknown: ["…", `Ключ Gemini збережено ${k.hint ? `(${k.hint})` : ""}`],
      checking: ["…", "Перевіряю ключ у Google…"],
      ok: ["✓", "Ключ Gemini працює"],
      bad: ["!", "Ключ Gemini не працює"],
    }[keyState];
    $("setKeyBox").dataset.state = keyState;
    $("setKeyIc").textContent = T[0];
    $("setKeyState").textContent = T[1];
    $("setKeyMsg").textContent = keyState === "ok" || keyState === "unknown"
      ? `${k.hint ? k.hint + " · " : ""}${keyMsg || "зберігається тільки на цьому комп’ютері"}`
      : keyMsg;
    $("setKeyCheck").hidden = !k.set;
    $("setKeyCheck").disabled = keyState === "checking";
    $("setKeyChange").textContent = k.set ? "Замінити" : "Додати ключ";
    $("setKeyDot").hidden = !(keyState === "none" || keyState === "bad");
    if (keyState === "none") $("setKeyEdit").hidden = false;
  }

  async function checkSavedKey() {
    keyState = "checking";
    renderKey();
    try {
      const res = await fetch("/api/settings/check-saved-key", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Не вдалося перевірити ключ.");
      keyState = data.ok ? "ok" : "bad";
      keyMsg = data.ok ? `${data.message === "Ключ працює." ? "перевірено" : data.message} · о ${new Date().toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}` : data.message;
    } catch (err) {
      keyState = "bad";
      keyMsg = err.message;
    }
    renderKey();
  }

  function showKeyInput() {
    $("setKeyEdit").hidden = false;
    $("setKeyInput").focus();
  }

  async function saveKey() {
    const input = $("setKeyInput");
    const key = input.value.trim();
    if (!key) { input.focus(); return; }
    const btn = $("setKeySave");
    btn.disabled = true;
    keyState = "checking";
    renderKey();
    try {
      const res = await fetch("/api/settings/check-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Не вдалося перевірити ключ.");
      if (data.settings) view = data.settings;
      if (data.ok) {
        keyState = "ok";
        keyMsg = "збережено й перевірено";
        input.value = "";
        $("setKeyEdit").hidden = true;
        flash("Ключ збережено ✓");
      } else {
        keyState = view.gemini_key.set ? "bad" : "none";
        keyMsg = data.message + (view.gemini_key.set ? " Старий ключ лишився." : "");
      }
    } catch (err) {
      keyState = view.gemini_key.set ? "bad" : "none";
      keyMsg = err.message;
    } finally {
      btn.disabled = false;
      renderKey();
    }
  }

  // ── hardware & about ─────────────────────────────────────────────────────────
  async function showHardware() {
    const box = $("setHw");
    if (box.dataset.done) return;
    try {
      const h = await (await fetch("/api/health")).json();
      const gpu = h.has_cuda;
      box.dataset.done = "1";
      box.classList.toggle("slow", !gpu);
      box.innerHTML = gpu
        ? `<i aria-hidden="true">⚡</i><span>${escapeHtml(h.gpu_name)}</span>`
        : `<i aria-hidden="true">🐢</i><span>Без відеокарти: година відео ≈ 30–40 хв</span>`;
    } catch (_) {
      box.innerHTML = `<i aria-hidden="true">?</i><span>Не вдалося дізнатися</span>`;
    }
  }

  async function showAbout() {
    let info = typeof Welcome !== "undefined" && Welcome.info ? Welcome.info() : null;
    if (!info) {
      try { info = await (await fetch("/api/app-info")).json(); } catch (_) { info = null; }
    }
    if (!info) { $("setVersionNote").textContent = "Не вдалося дізнатися версію."; return; }
    $("setVersion").textContent = `Студія моментів ${info.version}`;
    const canUpdate = info.update_available && info.can_self_update;
    $("setUpdate").hidden = !canUpdate;
    if (canUpdate) $("setUpdate").textContent = `⬆ Оновити до ${info.latest}`;
    $("setVersionNote").textContent = info.update_available
      ? `Вийшла нова версія ${info.latest}.${canUpdate ? "" : " Онови її через «Оновити.bat»."}`
      : info.latest ? "У тебе найновіша версія." : "Не вдалося перевірити оновлення (немає інтернету?).";
  }

  function update() {
    const info = typeof Welcome !== "undefined" && Welcome.info ? Welcome.info() : null;
    if (!info) return;
    if (!confirm(`Оновити студію до версії ${info.latest}? Вона закриється й відкриється знову. Твої відео, кліпи й ключ лишаться.`)) return;
    Welcome.update(true);
  }

  // ── disk ──────────────────────────────────────────────────────────────────

  const CATS = [
    { key: "sources", label: "Оригінали відео", note: "потрібні, щоб різати кліпи", color: "var(--disk-src)" },
    { key: "clips", label: "Нарізані кліпи 16:9", note: "робляться знову за потреби", color: "var(--disk-clips)" },
    { key: "vertical", label: "Вертикальні кліпи", note: "робляться знову за потреби", color: "var(--disk-vert)" },
    { key: "transcripts", label: "Розпізнаний текст", note: "щоб не розпізнавати вдруге", color: "var(--disk-tr)" },
    { key: "junk", label: "Сміття", note: "можна прибрати", color: "var(--disk-junk)" },
  ];

  function fmtBytes(b) {
    if (!b) return "0 МБ";
    if (b >= 1e9) return `${(b / 1e9).toFixed(b >= 1e10 ? 0 : 1)} ГБ`;
    if (b >= 1e6) return `${Math.round(b / 1e6)} МБ`;
    return `${Math.max(1, Math.round(b / 1e3))} КБ`;
  }

  async function loadDisk() {
    $("diskStudio").textContent = "рахуємо…";
    try {
      const res = await fetch("/api/storage");
      if (!res.ok) throw new Error(await res.text());
      disk = await res.json();
      renderDisk();
    } catch (err) {
      $("diskStudio").textContent = "не вдалося порахувати";
    }
  }

  function renderDisk() {
    const t = disk.totals;
    const all = disk.studio_bytes || 1;
    $("diskStudio").textContent = fmtBytes(disk.studio_bytes);
    $("diskFree").textContent = disk.disk.total ? `Вільно на диску: ${fmtBytes(disk.disk.free)} з ${fmtBytes(disk.disk.total)}` : "";
    $("diskBar").innerHTML = CATS.filter((c) => t[c.key] > 0)
      .map((c) => `<i style="flex:${t[c.key] / all};background:${c.color}" title="${c.label}: ${fmtBytes(t[c.key])}"></i>`).join("");
    $("diskLegend").innerHTML = CATS.map((c) => `
      <li><i style="background:${c.color}"></i><span>${c.label}<small>${c.note}</small></span><b>${fmtBytes(t[c.key])}</b></li>`).join("");
    $("diskJunk").textContent = t.junk ? `· ${fmtBytes(t.junk)}` : "· чисто";
    $("btnCleanJunk").disabled = !t.junk;
    $("diskClips").textContent = `· ${fmtBytes(t.clips + t.vertical)}`;

    const list = disk.projects;
    $("diskProjects").innerHTML = list.length ? list.map((p) => `
      <li>
        <img class="dp-th" src="/api/projects/${encodeURIComponent(p.id)}/thumbnail" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="dp-main">
          <span class="dp-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>
          <span class="dp-meta">відео ${fmtBytes(p.source_bytes)}${p.clips_bytes ? ` · файли кліпів ${fmtBytes(p.clips_bytes)}` : ""}${p.kept_count ? ` · обрано ${p.kept_count}` : ""}</span>
        </div>
        <b class="dp-size">${fmtBytes(p.total_bytes)}</b>
        <div class="dp-actions">
          ${p.clips_bytes ? `<button type="button" class="btn btn-secondary btn-sm" data-clean="${escapeHtml(p.id)}" title="Видалити тільки готові файли кліпів">Файли кліпів</button>` : ""}
          <button type="button" class="btn btn-secondary btn-sm dp-del" data-del="${escapeHtml(p.id)}" title="Видалити відео з усіма кліпами">Видалити відео</button>
        </div>
      </li>`).join("") : `<li class="dp-empty">Відео ще немає.</li>`;
  }

  async function cleanup(body, what) {
    const res = await fetch("/api/storage/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showToast(await errorText(res), "error");
      return;
    }
    const data = await res.json();
    disk = data.storage;
    renderDisk();
    showToast(data.freed_bytes ? `${what}: звільнено ${fmtBytes(data.freed_bytes)}` : "Нічого прибирати не було.", "success");
  }

  function cleanJunk() {
    cleanup({ junk: true }, "Сміття прибрано");
  }

  function cleanClips() {
    const days = parseInt($("diskOlder").value, 10) || 0;
    const what = days ? `файли кліпів, старші за ${days} днів` : "усі готові файли кліпів";
    if (!confirm(`Видалити ${what}? Самі кліпи й позначки лишаться, файл зробиться знову при скачуванні.`)) return;
    cleanup({ clips: true, older_than_days: days }, "Файли кліпів видалено");
  }

  async function onProjectAction(e) {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.clean) {
      cleanup({ clips: true, project_ids: [b.dataset.clean] }, "Файли кліпів видалено");
    } else if (b.dataset.del) {
      const p = disk.projects.find((x) => x.id === b.dataset.del);
      if (!confirm(`Видалити «${p ? p.title : "відео"}» повністю? Зникнуть відео, кліпи й позначки. Це не скасувати.`)) return;
      const res = await fetch(`/api/projects/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      if (!res.ok) {
        showToast(await errorText(res), "error");
        return;
      }
      showToast("Відео видалено.");
      loadDisk();
      if (typeof loadRecentProjects === "function") loadRecentProjects();
    }
  }

  async function errorText(res) {
    const t = await res.text();
    try {
      const d = JSON.parse(t).detail;
      if (typeof d === "string") return d;
    } catch (_) {}
    return t || `Помилка ${res.status}`;
  }

  function init() {
    const s = sheet();
    if (!s) return;
    s.addEventListener("click", (e) => { if (e.target === s) close(); });
    s.querySelectorAll(".settings-tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
    s.querySelectorAll("[data-key]").forEach((el) => {
      if (el.classList.contains("set-seg") || el.classList.contains("set-cards")) el.addEventListener("click", onChoice);
      else el.addEventListener("change", () => onFieldChange(el));
    });
    [minIn(), maxIn()].forEach((el) => el.addEventListener("input", onLengthInput));
    $("setLenPresets").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-min]");
      if (b) save({ clips: { default_min_len: Number(b.dataset.min), default_max_len: Number(b.dataset.max) } });
    });
    $("setAutoOn").addEventListener("change", onAutoClean);
    $("setAutoDays").addEventListener("change", onAutoClean);
    $("setKeyInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveKey(); });
    $("diskProjects").addEventListener("click", onProjectAction);
    ["heroClips", "heroFilter", "heroLang"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => { el.dataset.touched = "1"; });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !s.hidden) close();
    });
    load();
  }

  document.addEventListener("DOMContentLoaded", init);

  return { open, close, setTab, saveKey, checkSavedKey, showKeyInput, update, resetAll, cleanJunk, cleanClips, fmtBytes, reload: load };
})();
