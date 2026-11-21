/**
 * theme.js — one theme choice for the whole studio (home page and «Монтажний стіл»).
 * Loaded in <head> so the page never flashes the wrong theme.
 * "system" follows the OS setting; "light"/"dark" set html[data-theme].
 */
const StudioTheme = (() => {
  const KEY = "studio-theme";
  const MODES = ["system", "light", "dark"];

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
  }

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
      b.addEventListener("click", () => set(b.dataset.theme));
    });
    apply(get());
  }

  apply(get());
  document.addEventListener("DOMContentLoaded", bind);
  return { get, set, apply, bind };
})();
