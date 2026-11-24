/**
 * settings.js — «Налаштування»: five sections in a side menu (Кліпи, Ключ і розпізнавання, Місце на диску,
 * Надійність, Про студію). Buttons, cards, the two-thumb length slider and switches all carry data-key and save at once
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
    if (typeof Queue !== "undefined") Queue.refresh(); // the home disk notice may have changed after a cleanup
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
    if (name === "safe") loadBackups();
  }

  // ── «Надійність»: daily database copies ───────────────────────────────────

  const BK_KIND = { daily: "", manual: "зроблено вручну", "before-restore": "перед відновленням" };

  function bkWhen(ts) {
    const d = new Date(ts * 1000), today = new Date();
    const y = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    const t = d.toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return `Сьогодні, ${t}`;
    if (d.toDateString() === y.toDateString()) return `Вчора, ${t}`;
    const day = d.toLocaleDateString("uk-UA", { weekday: "long", day: "numeric", month: "long" });
    return `${day[0].toUpperCase()}${day.slice(1)}, ${t}`;
  }
  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
  }

  let backups = [];
  async function loadBackups() {
    try {
      const res = await fetch("/api/backups");
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      backups = data.items || [];
      const last = backups.find((b) => b.kind !== "before-restore") || backups[0];
      $("bkLast").textContent = last ? `Остання копія: ${bkWhen(last.created).replace(/^С/, "с").replace(/^В/, "в")}` : "Копій ще немає";
      $("bkNote").textContent = backups.length
        ? `Зберігаємо ${data.keep} останніх · разом ${fmtBytes(data.total_size)} · папка backups`
        : `Перша копія зробиться сама за кілька хвилин після запуску студії`;
      $("bkRestoreGrp").hidden = !backups.length;
      $("bkList").innerHTML = backups.map((b) => {
        const what = b.videos == null ? "" : `${b.videos} відео · ${b.clips} ${plural(b.clips, "кліп", "кліпи", "кліпів")}`;
        const kind = BK_KIND[b.kind] ? ` · ${BK_KIND[b.kind]}` : "";
        return `<li><b>${escapeHtml(bkWhen(b.created))}</b><span>${escapeHtml(what + kind)}</span>
          <button type="button" class="btn btn-secondary btn-sm" data-restore="${escapeHtml(b.name)}">Відновити</button></li>`;
      }).join("");
    } catch (err) {
      console.warn("backups", err);
    }
  }

  async function backupNow() {
    $("bkNow").disabled = true;
    try {
      const res = await fetch("/api/backups", { method: "POST" });
      if (!res.ok) { showToast(await errorText(res), "error"); return; }
      showToast("Копію бази збережено", "success");
      await loadBackups();
    } finally {
      $("bkNow").disabled = false;
    }
  }

  async function restoreBackup(name) {
    const b = backups.find((x) => x.name === name);
    if (!b) return;
    const when = bkWhen(b.created);
    const ok = await askConfirm(
      `Статуси, межі й заголовки кліпів стануть такими, як були на момент копії. Зміни, зроблені після цього, зникнуть. ` +
      `Перед цим студія сама збереже копію поточного стану, тож це можна буде скасувати.`,
      { title: `Повернути базу до «${when}»?`, ok: "Відновити" });
    if (!ok) return;
    const res = await fetch(`/api/backups/${encodeURIComponent(name)}/restore`, { method: "POST" });
    if (!res.ok) { showToast(await errorText(res), "error"); return; }
    showToast(`Базу повернуто до «${when}»`, "success");
    await loadBackups();
    if (typeof loadRecentProjects === "function") loadRecentProjects();
    if (typeof Queue !== "undefined") Queue.refresh();
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
    const hs = $("heroSearch");
    if (hs && !hs.dataset.touched) hs.value = v.studio.default_search || "classic";
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
    if (!(await askConfirm("Повернути всі налаштування як було спочатку? Ключ Gemini, відео й кліпи лишаться.", { ok: "Повернути" }))) return;
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

  async function update() {
    const info = typeof Welcome !== "undefined" && Welcome.info ? Welcome.info() : null;
    if (!info) return;
    if (!(await askConfirm(`Оновити студію до версії ${info.latest}? Вона закриється й відкриється знову. Твої відео, кліпи й ключ лишаться.`, { ok: "Оновити" }))) return;
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
    $("diskDonePlate").hidden = !disk.finished_count;
    $("diskDoneCount").textContent = `· ${disk.finished_count} відео`;
    $("btnCleanDone").textContent = `Видалити · звільнить ${fmtBytes(disk.finished_bytes)}`;

    const list = disk.projects;
    $("diskProjects").innerHTML = list.length ? list.map((p) => `
      <li>
        <img class="dp-th" src="/api/projects/${encodeURIComponent(p.id)}/thumbnail" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="dp-main">
          <span class="dp-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</span>${chipOf(p)}
          <span class="dp-meta">відео ${fmtBytes(p.source_bytes)}${p.clips_bytes ? ` · файли кліпів ${fmtBytes(p.clips_bytes)}` : ""}${p.kept_count ? ` · в роботі ${p.kept_count}` : ""}</span>
        </div>
        <b class="dp-size">${fmtBytes(p.total_bytes)}</b>
        <div class="dp-actions">
          ${p.removable ? `<button type="button" class="btn btn-primary btn-sm" data-finish="${escapeHtml(p.id)}" title="Ти закінчив з цим відео: видалити його повністю разом із кліпами">Видалити</button>` : ""}
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


  /** Shelf summary next to a video's title: «✓ закінчено», or what is still left to watch. */
  function chipOf(p) {
    if (p.finished) return '<span class="dp-chip done" title="У «Вірусних» і «В роботі» нічого не лишилось">✓ закінчено</span>';
    if (!p.clips_count) return "";
    return `<span class="dp-chip left">вірусних ${p.new_count} · в роботі ${p.kept_count}</span>`;
  }

  // ─── «Видалити відео, з якими закінчив»: a dialog with a checkbox per video ───
  let rmIds = [];
  function openRemoveDialog(ids) {
    const list = disk.projects.filter((p) => p.removable && (!ids || ids.includes(p.id)));
    if (!list.length) return;
    rmIds = list.map((p) => p.id);
    $("rmList").innerHTML = list.map((p) => `
      <li><label>
        <input type="checkbox" data-rm="${escapeHtml(p.id)}" checked>
        <span class="rm-t"><b>${escapeHtml(p.title)}</b><small>виконано ${p.done_count} · у запасі ${p.reserve_count}${p.drop_count ? ` · не беру ${p.drop_count}` : ""}</small></span>
        <span class="rm-s">${fmtBytes(p.total_bytes)}</span>
      </label></li>`).join("");
    $("rmList").onchange = updateRemoveDialog;
    updateRemoveDialog();
    $("rmDialog").style.display = "flex";
    $("rmGo").focus();
  }
  function rmChosen() { return [...$("rmList").querySelectorAll("input[data-rm]:checked")].map((i) => i.dataset.rm); }
  function updateRemoveDialog() {
    const ids = rmChosen();
    const bytes = disk.projects.filter((p) => ids.includes(p.id)).reduce((s, p) => s + p.total_bytes, 0);
    $("rmTitle").textContent = `Видалити ${ids.length} відео?`;
    $("rmLead").textContent = `Звільнить ${fmtBytes(bytes)}. Відео зникнуть зі студії повністю, разом із кліпами й позначками. Це не скасувати.`;
    $("rmGo").textContent = `Видалити · ${fmtBytes(bytes)}`;
    $("rmGo").disabled = !ids.length;
  }
  function closeRemoveDialog() { $("rmDialog").style.display = "none"; }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("rmDialog")?.style.display === "flex") closeRemoveDialog(); });
  async function confirmRemove() {
    const ids = rmChosen();
    if (!ids.length) return;
    $("rmGo").disabled = true;
    const res = await fetch("/api/storage/delete-finished", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_ids: ids }),
    });
    $("rmGo").disabled = false;
    if (!res.ok) { showToast(await errorText(res), "error"); return; }
    const data = await res.json();
    closeRemoveDialog();
    disk = data.storage;
    renderDisk();
    showToast(`Видалено ${data.deleted} відео: звільнено ${fmtBytes(data.freed_bytes)}`, "success");
    if (typeof loadRecentProjects === "function") loadRecentProjects();
  }

  async function cleanClips() {
    const days = parseInt($("diskOlder").value, 10) || 0;
    const what = days ? `файли кліпів, старші за ${days} днів` : "усі готові файли кліпів";
    if (!(await askConfirm(`Видалити ${what}? Самі кліпи й позначки лишаться, файл зробиться знову при скачуванні.`, { ok: "Видалити", danger: true }))) return;
    cleanup({ clips: true, older_than_days: days }, "Файли кліпів видалено");
  }

  async function onProjectAction(e) {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.finish) {
      openRemoveDialog([b.dataset.finish]);
    } else if (b.dataset.clean) {
      cleanup({ clips: true, project_ids: [b.dataset.clean] }, "Файли кліпів видалено");
    } else if (b.dataset.del) {
      const p = disk.projects.find((x) => x.id === b.dataset.del);
      if (!(await askConfirm(`Видалити «${p ? p.title : "відео"}» повністю? Зникнуть відео, кліпи й позначки. Це не скасувати.`, { ok: "Видалити", danger: true }))) return;
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
    $("bkList").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-restore]");
      if (b) restoreBackup(b.dataset.restore);
    });
    ["heroClips", "heroFilter", "heroLang", "heroSearch"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => { el.dataset.touched = "1"; });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !s.hidden) close();
    });
    load();
  }

  document.addEventListener("DOMContentLoaded", init);

  return { open, close, setTab, backupNow, saveKey, checkSavedKey, showKeyInput, update, resetAll, cleanJunk, cleanClips, openRemoveDialog, closeRemoveDialog, confirmRemove, fmtBytes, reload: load };
})();
