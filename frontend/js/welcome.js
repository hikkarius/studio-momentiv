/**
 * welcome.js — first run: if no Gemini key is saved, shows 3 steps to get a free key, checks it with
 * Google (POST /api/settings/check-key) and saves it. Also shows the «Оновити» pill when GitHub has a
 * newer release (GET /api/app-info) and starts the updater (POST /api/update).
 * Depends on app.js globals: showToast.
 */
const Welcome = (() => {
  const $ = (id) => document.getElementById(id);
  let info = null;

  function open() {
    $("welcomeScreen").hidden = false;
    document.body.style.overflow = "hidden";
    setTimeout(() => $("welcomeKey").focus(), 50);
  }

  function close() {
    $("welcomeScreen").hidden = true;
    document.body.style.overflow = "";
  }

  function msg(text, kind) {
    const el = $("welcomeMsg");
    el.textContent = text;
    el.className = "welcome-msg" + (kind ? " " + kind : "");
  }

  async function saveKey() {
    const key = $("welcomeKey").value.trim();
    if (!key) { msg("Спершу встав ключ.", "err"); return; }
    const btn = $("welcomeSave");
    btn.disabled = true;
    msg("Перевіряю ключ у Google…");
    try {
      const res = await fetch("/api/settings/check-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { msg(data.detail || "Не вдалося перевірити ключ.", "err"); return; }
      if (!data.ok) { msg(data.message, "err"); return; }
      msg(data.message + " Можна починати!", "ok");
      if (typeof Settings !== "undefined" && Settings.reload) Settings.reload();
      setTimeout(close, 1200);
      if (typeof showToast === "function") showToast("Ключ Gemini збережено");
    } catch (err) {
      msg("Студія не відповідає. Перевір, що її чорне вікно відкрите.", "err");
    } finally {
      btn.disabled = false;
    }
  }

  async function showGpu() {
    try {
      const h = await (await fetch("/api/health")).json();
      const g = $("welcomeGpu");
      g.classList.toggle("ok", !!h.has_cuda);
      g.innerHTML = icon("monitor") + "<span></span>";
      g.lastChild.textContent = h.has_cuda
        ? `Знайдено відеокарту ${h.gpu_name}: відео оброблятимуться швидко.`
        : "Відеокарту NVIDIA не знайдено. Студія працюватиме, але повільніше: година відео ≈ 30–40 хв.";
    } catch (_) { /* not critical */ }
  }

  async function checkKey() {
    try {
      const s = await (await fetch("/api/settings")).json();
      if (!s.gemini_key || !s.gemini_key.set) { showGpu(); open(); }
    } catch (_) { /* server busy: the settings sheet still lets them add a key */ }
  }

  async function checkUpdate() {
    try {
      info = await (await fetch("/api/app-info")).json();
      $("welcomeVersion").textContent = "Версія " + info.version;
      const btn = $("btnUpdate");
      if (info.update_available && info.can_self_update) {
        btn.hidden = false;
        btn.textContent = `⬆ Оновити до ${info.latest}`;
        btn.title = `Вийшла нова версія ${info.latest} (у тебе ${info.version})`;
      }
    } catch (_) { /* offline: no pill */ }
  }

  /** skipConfirm: the caller already asked (the «Про студію» section uses a dialog). */
  async function update(skipConfirm = false) {
    if (!info) return;
    if (!skipConfirm && !confirmUpdate()) return;
    try {
      const res = await fetch("/api/update", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(data.detail || "Не вдалося почати оновлення"); return; }
      document.body.innerHTML = `<div style="display:grid;place-items:center;min-height:100vh;padding:24px;text-align:center;font:500 17px system-ui;color:var(--text-main);background:var(--bg-main)"><div><p style="font-size:22px;font-weight:700">Оновлюю студію до версії ${info.latest}…</p><p>Відкрилось окреме вікно з ходом оновлення. Коли воно закінчить, студія відкриється сама.</p></div></div>`;
    } catch (_) {
      showToast("Студія не відповідає");
    }
  }

  function confirmUpdate() {
    const btn = $("btnUpdate");
    if (btn.dataset.armed === "1") return true;
    btn.dataset.armed = "1";
    btn.textContent = "Точно? Натисни ще раз";
    setTimeout(() => { btn.dataset.armed = ""; if (info) btn.textContent = `⬆ Оновити до ${info.latest}`; }, 4000);
    return false;
  }

  function start() { checkKey(); checkUpdate(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  return { open, close, saveKey, update, info: () => info };
})();
