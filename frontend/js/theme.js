/**
 * theme.js — one theme choice for the whole studio (home page and «Монтажний стіл»).
 * Loaded in <head> so the page never flashes the wrong theme.
 * "system" follows the OS setting; "light"/"dark" set html[data-theme].
 * The choice lives in a small drop-down (.theme-menu): a button showing the current theme + a list.
 */
const StudioTheme = (() => {
  const KEY = "studio-theme";
  const MODES = ["system", "light", "dark"];
  const LABELS = { light: "Тема: світла", dark: "Тема: темна", system: "Тема: авто" };

  function get() {
    try {
      const v = localStorage.getItem(KEY);
      return MODES.includes(v) ? v : "system";
    } catch (_) {
      return "system";
    }
  }

  function apply(mode) {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", mode);
    document.querySelectorAll(".theme-switch button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.theme === mode));
    });
    document.querySelectorAll(".theme-menu-cur").forEach((el) => { el.innerHTML = `<svg class="i" aria-hidden="true"><use href="#i-theme"/></svg><span></span>`; el.lastChild.textContent = LABELS[mode]; });
  }

  function openMenu(menu, on) {
    const btn = menu.querySelector(".theme-menu-btn");
    const pop = menu.querySelector(".theme-menu-pop");
    if (!btn || !pop) return;
    pop.hidden = !on;
    btn.setAttribute("aria-expanded", String(on));
  }
  const closeAll = (except) => document.querySelectorAll(".theme-menu").forEach((m) => { if (m !== except) openMenu(m, false); });

  function set(mode) {
    if (!MODES.includes(mode)) return;
    try { localStorage.setItem(KEY, mode); } catch (_) {}
    apply(mode);
  }

  /** Wires every .theme-switch on the page (buttons carry data-theme="system|light|dark"). */
  function bind() {
    document.querySelectorAll(".theme-switch button").forEach((b) => {
      if (b._themeBound) return;
      b._themeBound = true;
      b.addEventListener("click", () => {
        set(b.dataset.theme);
        const m = b.closest(".theme-menu");
        if (m) { openMenu(m, false); m.querySelector(".theme-menu-btn")?.focus(); }
      });
    });
    document.querySelectorAll(".theme-menu").forEach((m) => {
      if (m._themeBound) return;
      m._themeBound = true;
      m.querySelector(".theme-menu-btn")?.addEventListener("click", () => {
        const on = m.querySelector(".theme-menu-pop").hidden;
        closeAll(m);
        openMenu(m, on);
        if (on) m.querySelector(".theme-menu-pop button[aria-pressed='true']")?.focus();
      });
    });
    if (!document._themeMenuBound) {
      document._themeMenuBound = true;
      document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".theme-menu")) closeAll(); });
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        const open = [...document.querySelectorAll(".theme-menu")].find((m) => !m.querySelector(".theme-menu-pop").hidden);
        if (open) { e.stopPropagation(); openMenu(open, false); open.querySelector(".theme-menu-btn").focus(); }
      }, true);
    }
    apply(get());
  }

  apply(get());
  document.addEventListener("DOMContentLoaded", bind);
  return { get, set, apply, bind };
})();
