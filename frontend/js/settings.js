/**
 * settings.js — «Налаштування»: clip lengths, defaults for new videos, Whisper / Gemini, disk space.
 * Every change is saved at once (POST /api/settings) and applies without restarting the studio.
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
    if (tab === "disk") loadDisk();
  }

  function close() {
    sheet().hidden = true;
    document.body.style.overflow = "";
  }

  function setTab(name) {
    tab = name;
    sheet().querySelectorAll(".settings-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    sheet().querySelectorAll(".settings-body").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    if (name === "disk" && !sheet().hidden) loadDisk();
  }

  // ── values ────────────────────────────────────────────────────────────────

  async function load() {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) throw new Error(await res.text());
      view = await res.json();
      fill();
      applyToHome();
    } catch (err) {
      console.warn("settings", err);
    }
  }

  function options(sel, list) {
    if (!sel || sel.options.length) return;
    sel.innerHTML = list.map((o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`).join("");
  }

  function get(key) {
    const [sec, k] = key.split(".");
    return view.values[sec][k];
  }

  function fill() {
    options($("setModel"), view.choices.whisper_model);
    options($("setLang"), view.choices.language);
    options($("setFilter"), view.choices.filter);
    sheet().querySelectorAll("[data-key]").forEach((el) => {
      const v = get(el.dataset.key);
      if (el.type === "checkbox") el.checked = !!v;
      else {
        if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === String(v))) {
          el.add(new Option(String(v), String(v)));   // a value set in config.yaml by hand
        }
        el.value = String(v);
      }
    });
    const k = view.gemini_key;
    $("setKeyState").textContent = k.set ? `збережено (${k.hint || "…"})` : "не задано — пошук моментів не працюватиме";
    $("setKeyState").classList.toggle("bad", !k.set);
  }

  /** Home page picks up the defaults: clip count, search style and the hint line. */
  function applyToHome() {
    if (!view) return;
    const v = view.values;
    const hc = $("heroClips");
    const hf = $("heroFilter");
    if (hc && !hc.dataset.touched) {
      const val = String(v.studio.default_clips);
      if (![...hc.options].some((o) => o.value === val)) hc.add(new Option(val, val));
      hc.value = val;
    }
    if (hf && !hf.dataset.touched) hf.value = v.studio.default_filter;
    const lang = (view.choices.language.find((l) => l.value === v.whisper.language) || {}).label || v.whisper.language;
    if (typeof Home !== "undefined") Home.setDefaults({ lang: lang.toLowerCase(), min: v.clips.default_min_len, max: v.clips.default_max_len });
  }

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
      fill();   // put the last saved value back
      flash(err.message, true);
      showToast(err.message, "error");
      return false;
    }
  }

  function onFieldChange(el) {
    const [sec, k] = el.dataset.key.split(".");
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "number" || el.dataset.type === "int") {
      v = Number(el.value);
      if (el.value === "" || Number.isNaN(v)) { fill(); return; }
    } else v = el.value;
    save({ [sec]: { [k]: v } });
  }

  async function saveKey() {
    const input = $("setKeyInput");
    const key = input.value.trim();
    if (!key) { input.focus(); return; }
    if (await save({ gemini_api_key: key }, "Ключ збережено ✓")) input.value = "";
  }

  async function resetAll() {
    if (!confirm("Повернути всі налаштування як було спочатку (з config.yaml)? Ключ Gemini лишиться.")) return;
    const res = await fetch("/api/settings/reset", { method: "POST" });
    if (res.ok) {
      view = await res.json();
      fill();
      applyToHome();
      flash("Повернуто ✓");
    }
  }

  function flash(text, bad = false) {
    const el = $("settingsSaved");
    el.textContent = text;
    el.classList.toggle("bad", bad);
    el.classList.add("show");
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => el.classList.remove("show"), bad ? 6000 : 1800);
  }

  // ── disk ──────────────────────────────────────────────────────────────────

  const CATS = [
    { key: "sources", label: "Вихідні відео", note: "потрібні, щоб різати кліпи", color: "var(--disk-src)" },
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
    s.querySelectorAll("[data-key]").forEach((el) => el.addEventListener("change", () => onFieldChange(el)));
    $("setKeyInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveKey(); });
    $("diskProjects").addEventListener("click", onProjectAction);
    ["heroClips", "heroFilter"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => { el.dataset.touched = "1"; });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !s.hidden) close();
    });
    load();
  }

  document.addEventListener("DOMContentLoaded", init);

  return { open, close, setTab, saveKey, resetAll, cleanJunk, cleanClips, fmtBytes, reload: load };
})();
