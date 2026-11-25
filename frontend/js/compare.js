/**
 * compare.js — «Порівняти наосліп»: the classic and the experimental search on one video, clip against clip,
 * without saying which search made which clip. At the end: who won, and «Взяти кліпи переможця».
 */
const Compare = (() => {
  const MODE = {
    classic: "📼 Класичний",
    experimental: "🧪 Експериментальний",
  };
  let st = { project: null, data: null, cur: 0, timer: null, back: null, onClose: null };

  const api = (path, opts = {}) =>
    fetch(`/api/projects/${encodeURIComponent(st.project.id)}/compare${path}`, {
      headers: { "Content-Type": "application/json" }, ...opts,
    }).then(async (r) => {
      if (!r.ok) {
        let msg = "Щось пішло не так.";
        try { msg = (await r.json()).detail || msg; } catch (_) {}
        throw new Error(msg);
      }
      return r.json();
    });

  /** Opens the comparison for a video; starts both searches unless a comparison is already there. */
  async function open(project, { fresh = false, onClose = null } = {}) {
    st = { project, data: null, cur: 0, timer: null, back: null, onClose };
    build();
    try {
      let data = await api("");
      if (fresh || data.status === "none" || data.status === "failed") data = await api("", { method: "POST" });
      show(data);
    } catch (err) {
      showError(err.message);
    }
  }

  function build() {
    const back = document.createElement("div");
    back.className = "modal-backdrop cmp-dialog";
    back.setAttribute("role", "dialog");
    back.setAttribute("aria-modal", "true");
    back.setAttribute("aria-labelledby", "cmpTitle");
    back.innerHTML = `
      <div class="modal-card cmp-card">
        <div class="modal-header">
          <h3 class="modal-title" id="cmpTitle">⚖ Порівняння наосліп · ${escapeHtml(st.project.title || "Відео")}</h3>
          <button type="button" class="cmp-x" data-a="close" aria-label="Закрити">✕</button>
        </div>
        <div class="cmp-body" id="cmpBody"></div>
      </div>`;
    back.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(back);
    st.back = back;
  }

  function close() {
    clearTimeout(st.timer);
    document.removeEventListener("keydown", onKey, true);
    if (st.back) st.back.remove();
    st.back = null;
    if (st.onClose) st.onClose();
  }

  function onKey(e) {
    if (!st.back) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (["Enter", " ", "Tab"].includes(e.key)) return;
    e.stopPropagation(); // the editor's hotkeys stay quiet
    if (!st.data || st.data.status !== "ready" || st.data.done) return;
    if (e.key === "1" || e.key === "ArrowLeft") vote("left");
    else if (e.key === "2" || e.key === "ArrowRight") vote("right");
    else if (e.key === "0" || e.key === "=") vote("same");
  }

  const body = () => st.back && st.back.querySelector("#cmpBody");

  function show(data) {
    st.data = data;
    clearTimeout(st.timer);
    if (!body()) return;
    if (data.status === "running") return showRunning(data);
    if (data.status === "failed") return showError(data.error || "Порівняння не вдалося.");
    if (data.status !== "ready") return showError("Порівняння ще не почалося.");
    if (data.done) return showResult(data);
    const firstOpen = data.pairs.findIndex((p) => !p.vote);
    if (st.cur >= data.pairs.length || (data.pairs[st.cur] && data.pairs[st.cur].vote && firstOpen >= 0)) st.cur = firstOpen;
    showPair(data);
  }

  function showRunning(data) {
    const pct = Math.round((data.progress || 0) * 100);
    body().innerHTML = `
      <p class="cmp-lead">Студія шукає кліпи двома способами по черзі. Транскрипція вже є, тож це хвилина-дві.
        Відео й твої кліпи не змінюються.</p>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      <p class="cmp-msg">${escapeHtml(data.message || "")}</p>`;
    st.timer = setTimeout(async () => {
      try { show(await api("")); } catch (err) { showError(err.message); }
    }, 1500);
  }

  function showError(msg) {
    if (!body()) return;
    body().innerHTML = `
      <p class="cmp-err">❌ ${escapeHtml(msg)}</p>
      <div class="cmp-actions"><button type="button" class="btn btn-primary" data-a="restart">↻ Спробувати ще раз</button></div>`;
  }

  function side(name, c) {
    const dur = Math.round((c.end || 0) - (c.start || 0));
    return `
      <div class="cmp-side">
        <video class="cmp-video" data-start="${c.start}" data-end="${c.end}" controls preload="metadata"
               src="/api/media?path=${encodeURIComponent(st.project.source_path)}#t=${c.start},${c.end}"></video>
        <div class="cmp-meta"><b>${name === "left" ? "Кліп А" : "Кліп Б"}</b> · ${dur} с</div>
        <div class="cmp-title">${escapeHtml(c.title || "")}</div>
        ${c.hook_text ? `<div class="cmp-hook">«${escapeHtml(String(c.hook_text).slice(0, 120))}»</div>` : ""}
        <button type="button" class="btn btn-primary cmp-pick" data-a="vote" data-v="${name}">Цей кращий</button>
      </div>`;
  }

  function showPair(data) {
    const p = data.pairs[st.cur];
    const voted = data.pairs.filter((x) => x.vote).length;
    body().innerHTML = `
      <p class="cmp-lead">Подивись обидва кліпи й обери кращий. Який спосіб їх знайшов, покажемо в кінці.</p>
      <div class="cmp-pair">${side("left", p.left)}${side("right", p.right)}</div>
      <div class="cmp-foot">
        <button type="button" class="btn btn-secondary" data-a="prev" ${st.cur === 0 ? "disabled" : ""}>← Попередня</button>
        <span class="cmp-step">Пара ${st.cur + 1} з ${data.pairs.length} · оцінено ${voted}</span>
        <button type="button" class="btn btn-secondary" data-a="vote" data-v="same">Однаково</button>
      </div>`;
    body().querySelectorAll(".cmp-video").forEach(bindSegment);
    const mark = p.vote && body().querySelector(`[data-a="vote"][data-v="${p.vote}"]`);
    if (mark) mark.classList.add("cmp-chosen");
  }

  /** A clip plays only its own part of the video. */
  function bindSegment(v) {
    const a = parseFloat(v.dataset.start), b = parseFloat(v.dataset.end);
    v.addEventListener("loadedmetadata", () => { v.currentTime = a; });
    v.addEventListener("play", () => {
      if (v.currentTime < a - 0.3 || v.currentTime >= b - 0.1) v.currentTime = a;
      body().querySelectorAll(".cmp-video").forEach((o) => { if (o !== v) o.pause(); });
    });
    v.addEventListener("timeupdate", () => { if (v.currentTime >= b) v.pause(); });
  }

  function showResult(data) {
    const t = data.tally;
    const exp = t.experimental, cls = t.classic;
    const winner = exp > cls ? "experimental" : cls > exp ? "classic" : "";
    const head = winner
      ? `${MODE[winner]} переміг: ${t[winner]} з ${data.pairs.length}`
      : `Нічия: по ${exp} з ${data.pairs.length}`;
    const rows = data.pairs.map((p, i) => {
      const won = p.vote === "left" ? p.left_mode : p.vote === "right" ? p.right_mode : "";
      const title = won ? (p.vote === "left" ? p.left : p.right).title : `${p.left.title} / ${p.right.title}`;
      return `<li><span class="cmp-r-n">${i + 1}</span><span class="cmp-r-w">${won ? MODE[won] : "однаково"}</span><span class="cmp-r-t">${escapeHtml(title || "")}</span></li>`;
    }).join("");
    body().innerHTML = `
      <div class="cmp-result">
        <div class="cmp-big">${head}</div>
        <p class="cmp-lead">📼 Класичний: ${cls} · 🧪 Експериментальний: ${exp} · однаково: ${t.same}.
          Знайдено кліпів: класичним ${data.counts.classic}, експериментальним ${data.counts.experimental}.</p>
        <ol class="cmp-rows">${rows}</ol>
        <div class="cmp-actions">
          ${winner ? `<button type="button" class="btn btn-primary" data-a="apply" data-v="${winner}">Взяти кліпи: ${MODE[winner]}</button>` : ""}
          <button type="button" class="btn btn-secondary" data-a="close">Залишити як є</button>
          <button type="button" class="btn btn-secondary" data-a="restart">↻ Порівняти заново</button>
        </div>
        <p class="cmp-note">«Взяти кліпи» замінить нові кліпи цього відео. Позначені («В роботі», «Виконані», «Не беру») залишаться.</p>
      </div>`;
  }

  async function vote(pick) {
    try {
      const data = await api("/vote", { method: "POST", body: JSON.stringify({ n: st.cur, pick }) });
      if (!data.done) {
        const next = data.pairs.findIndex((p, i) => i > st.cur && !p.vote);
        st.cur = next >= 0 ? next : data.pairs.findIndex((p) => !p.vote);
      }
      show(data);
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function onClick(e) {
    if (e.target === st.back) return; // a click beside the window does not lose the votes by accident
    const b = e.target.closest("[data-a]");
    if (!b) return;
    const a = b.dataset.a;
    if (a === "close") close();
    else if (a === "vote") vote(b.dataset.v);
    else if (a === "prev" && st.cur > 0) { st.cur -= 1; showPair(st.data); }
    else if (a === "restart") {
      try { show(await api("", { method: "POST" })); } catch (err) { showError(err.message); }
    } else if (a === "apply") {
      try {
        const r = await fetch(`/api/projects/${encodeURIComponent(st.project.id)}/compare/apply`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: b.dataset.v }),
        });
        if (!r.ok) throw new Error((await r.json()).detail || "Не вдалося взяти кліпи.");
        showToast(`Готово: кліпи способу ${MODE[b.dataset.v]}.`);
        close();
      } catch (err) {
        showToast(err.message, "error");
      }
    }
  }

  return { open, close, isOpen: () => !!st.back, state: () => ({ cur: st.cur, status: st.data && st.data.status }) };
})();
