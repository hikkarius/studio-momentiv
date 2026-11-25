/**
 * top3.js — «🏆 Топ-3»: the best short clips glued into one countdown video (#3 → #1).
 * Opened from the «Кліпи» strip of the editor: Top3.open(projectId, clips).
 * The studio renders it in the background (POST /api/projects/{id}/top3, then /api/vertical-jobs/{id}).
 */
const Top3 = (() => {
  const MAX = 5;
  const MAX_SEC = 180;
  let st = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmtS = (s) => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const rank = (c) => c.rank_score ?? c.virality_score ?? 0;
  const lenOf = (c) => c.end_sec - c.start_sec - (c.cuts || []).reduce((s, [a, b]) => s + (b - a), 0);
  const total = () => st.order.reduce((s, c) => s + lenOf(c), 0);

  /** The 3 best «В роботі» clips (short ones), topped up from «Вірусні» when fewer are in work; #3 first, #1 last. */
  function pick(clips) {
    const short = clips.filter((c) => c.kind !== "story" && c.review_status !== "drop");
    const best = (list) => list.slice().sort((a, b) => rank(b) - rank(a));
    let chosen = best(short.filter((c) => c.review_status === "keep")).slice(0, 3);
    if (chosen.length < 3) chosen = chosen.concat(best(short.filter((c) => !chosen.includes(c) && !c.reserve)).slice(0, 3 - chosen.length));
    return chosen.reverse();
  }

  function open(pid, clips) {
    close();
    st = { pid, clips, order: pick(clips), title: "", numbers: true, busy: false };
    st.title = `Топ-${st.order.length} найкращих моментів`;
    const back = document.createElement("div");
    back.className = "modal-backdrop top3-dialog";
    back.id = "top3Dialog";
    back.setAttribute("role", "dialog");
    back.setAttribute("aria-modal", "true");
    back.setAttribute("aria-labelledby", "top3Title");
    back.innerHTML = `
      <div class="modal-card">
        <div class="modal-body">
          <h3 class="modal-title" id="top3Title">🏆 Топ-3 з цього відео</h3>
          <p class="top3-lead">Кліпи склеюються в одне відео: від №${3} до №1. Порядок змінюй стрілками або перетягуванням.</p>
          <ol class="top3-list" id="top3List"></ol>
          <div class="top3-add" id="top3Add"></div>
          <label class="top3-row">Назва на початку <input type="text" id="top3Name" maxlength="120"></label>
          <label class="top3-check"><input type="checkbox" id="top3Num" checked> Великий номер «#3, #2, #1» на 1 с на початку кожного</label>
          <p class="top3-total" id="top3Total"></p>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-a="close">Скасувати</button>
          <button type="button" class="btn btn-secondary" data-a="h">⬇ 16:9 · MP4</button>
          <button type="button" class="btn btn-primary" data-a="v">📱 9:16 із субтитрами</button>
        </div>
      </div>`;
    document.body.appendChild(back);
    st.back = back;
    back.querySelector("#top3Name").value = st.title;
    back.addEventListener("click", onClick);
    back.addEventListener("input", (e) => {
      if (e.target.id === "top3Name") st.title = e.target.value;
      if (e.target.id === "top3Num") st.numbers = e.target.checked;
    });
    back.addEventListener("change", (e) => {
      if (e.target.id === "top3Pick" && e.target.value) {
        const c = st.clips.find((x) => x.clip_index === Number(e.target.value));
        if (c) { st.order.unshift(c); renderList(); }
      }
    });
    wireDrag(back.querySelector("#top3List"));
    st.onKey = (e) => {
      if (e.key === "Escape" && !st.busy) { e.preventDefault(); e.stopPropagation(); close(); }
      else if (!["Tab", "Enter", " "].includes(e.key) && e.target.tagName !== "INPUT") e.stopPropagation(); // the editor's hotkeys stay quiet
      else if (e.target.tagName === "INPUT" && e.target.type === "text") e.stopPropagation();
    };
    document.addEventListener("keydown", st.onKey, true);
    renderList();
    const first = back.querySelector('[data-a="v"]');
    if (first) first.focus();
  }

  function renderList() {
    const n = st.order.length;
    const L = st.back.querySelector("#top3List");
    L.innerHTML = st.order.map((c, i) => `
      <li draggable="true" data-i="${i}">
        <span class="top3-grip" aria-hidden="true" title="Тягни, щоб змінити порядок">⠿</span>
        <b class="top3-n">#${n - i}</b>
        <img src="/api/projects/${encodeURIComponent(st.pid)}/clips/${c.clip_index}/thumbnail?s=${Math.round(c.start_sec * 10)}" alt="" draggable="false" loading="lazy" onerror="this.remove()">
        <span class="top3-t"><b>${esc(c.title)}</b><small>${fmtS(lenOf(c))} · ★ ${esc(rank(c))}</small></span>
        <span class="top3-mv">
          <button type="button" data-up="${i}" title="Раніше" aria-label="Раніше"${i === 0 ? " disabled" : ""}>↑</button>
          <button type="button" data-down="${i}" title="Пізніше" aria-label="Пізніше"${i === n - 1 ? " disabled" : ""}>↓</button>
        </span>
        <button type="button" class="top3-x" data-rm="${i}" title="Прибрати з топу" aria-label="Прибрати з топу">✕</button>
      </li>`).join("");
    const rest = st.clips.filter((c) => c.kind !== "story" && c.review_status !== "drop" && !st.order.includes(c))
      .sort((a, b) => rank(b) - rank(a));
    st.back.querySelector("#top3Add").innerHTML = n < MAX && rest.length
      ? `<select id="top3Pick" class="input-select" aria-label="Додати кліп"><option value="">＋ Додати кліп…</option>${rest.map((c) =>
          `<option value="${c.clip_index}">${esc(c.title)} · ${fmtS(lenOf(c))} · ★ ${esc(rank(c))}</option>`).join("")}</select>`
      : "";
    const t = total();
    const tooLong = t > MAX_SEC + 1, tooFew = n < 2;
    const tot = st.back.querySelector("#top3Total");
    tot.classList.toggle("bad", tooLong || tooFew);
    tot.textContent = tooFew ? "Потрібно щонайменше 2 кліпи." : tooLong
      ? `Разом ${fmtS(t)}: це більше 3 хв, Shorts і TikTok таке не візьмуть. Прибери кліп.`
      : `Разом ${fmtS(t)} · Shorts і TikTok беруть до 3 хв`;
    st.back.querySelector("#top3Title").textContent = `🏆 Топ-${n} з цього відео`;
    st.back.querySelector(".top3-lead").textContent = `Кліпи склеюються в одне відео: від №${n} до №1. Порядок змінюй стрілками або перетягуванням.`;
    st.back.querySelectorAll('[data-a="h"], [data-a="v"]').forEach((b) => { b.disabled = st.busy || tooLong || tooFew; });
  }

  function move(i, j) {
    if (j < 0 || j >= st.order.length || i === j) return;
    const [c] = st.order.splice(i, 1);
    st.order.splice(j, 0, c);
    renderList();
  }

  function wireDrag(L) {
    let from = null;
    L.addEventListener("dragstart", (e) => { const li = e.target.closest("li"); if (li) { from = Number(li.dataset.i); li.classList.add("dragging"); } });
    L.addEventListener("dragover", (e) => { if (from != null) e.preventDefault(); });
    L.addEventListener("drop", (e) => {
      e.preventDefault();
      const li = e.target.closest("li");
      if (li && from != null) move(from, Number(li.dataset.i));
      from = null;
    });
    L.addEventListener("dragend", () => { from = null; L.querySelectorAll(".dragging").forEach((x) => x.classList.remove("dragging")); });
  }

  function onClick(e) {
    if (e.target === st.back && !st.busy) { close(); return; }
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.dataset.up != null) move(Number(b.dataset.up), Number(b.dataset.up) - 1);
    else if (b.dataset.down != null) move(Number(b.dataset.down), Number(b.dataset.down) + 1);
    else if (b.dataset.rm != null) { st.order.splice(Number(b.dataset.rm), 1); renderList(); }
    else if (b.dataset.a === "close" && !st.busy) close();
    else if (b.dataset.a === "h" || b.dataset.a === "v") make(b, b.dataset.a === "v");
  }

  async function make(btn, vertical) {
    const label = btn.innerHTML;
    st.busy = true;
    renderList();
    btn.innerHTML = "⏳ Збираю…";
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(st.pid)}/top3`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clips: st.order.map((c) => c.clip_index), title: st.title.trim(), numbers: st.numbers, vertical }),
      });
      if (!res.ok) throw new Error(await errorText(res));
      let job = await res.json();
      const t0 = Date.now();
      while (job.status === "running") {
        await new Promise((r) => setTimeout(r, 1500));
        if (!st) return;
        const r = await fetch(`/api/vertical-jobs/${job.id}`);
        if (!r.ok) throw new Error(await errorText(r));
        job = await r.json();
        const sec = Math.round((Date.now() - t0) / 1000);
        btn.innerHTML = job.done >= job.total ? `⏳ Склеюю… ${sec} с` : `⏳ Кліп ${job.done + 1} з ${job.total}… ${sec} с`;
      }
      if (job.status !== "completed") throw new Error(job.error || "Не вдалося зібрати топ.");
      const a = document.createElement("a");
      a.href = `/api/vertical-jobs/${job.id}/file`; a.download = "";
      document.body.appendChild(a); a.click(); a.remove();
      if (typeof showToast === "function") showToast("Топ готовий, скачую");
    } catch (err) {
      if (typeof showToast === "function") showToast("Не вдалося зібрати топ: " + err.message, "error");
    } finally {
      if (st) { st.busy = false; btn.innerHTML = label; renderList(); }
    }
  }

  async function errorText(res) {
    try { const d = await res.json(); return typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail); } catch (_) { return res.statusText; }
  }

  function close() {
    if (!st) return;
    document.removeEventListener("keydown", st.onKey, true);
    if (st.back) st.back.remove();
    st = null;
    const b = document.getElementById("edTop3");
    if (b) b.focus();
  }

  return { open, close, pick };
})();
