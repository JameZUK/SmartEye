// SmartEye web UI: a hash router over a handful of views. No build step.
import { heatmap, bars, wireTips } from "./charts.js";
import {
  api, esc, setHTML, setTz, toast, face, kind, ago, duration, time, date, dateLong, dateTime, dayKey, stamp,
} from "./util.js";

const view = document.getElementById("view");
const modal = document.getElementById("modal");
const modalBody = document.getElementById("modal-body");
let current = null;     // the active route's refresh function

// ---------- shell

function themeInit() {
  let saved = null;
  try { saved = localStorage.getItem("smarteye-theme"); } catch {}
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById("theme").addEventListener("click", () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("smarteye-theme", next); } catch {}
  });
}

async function livePoll() {
  try {
    const s = await api("/api/visits?limit=10&kind=&since=" + (Date.now() / 1000 - 3600));
    const open = s.filter((v) => !v.closed);
    const el = document.getElementById("live");
    el.classList.toggle("on", open.length > 0);
    document.getElementById("live-text").textContent = open.length
      ? `At ${open[0].camera}: ${open.map((v) => v.name || "unknown").join(", ")}`
      : "Quiet";
    const sug = await api("/api/suggestions");
    const b = document.getElementById("review-badge");
    b.hidden = !sug.length;
    b.textContent = sug.length;
  } catch {}
}

function visitRow(v) {
  const who = v.name || "Unidentified";
  return `<button class="visit${v.closed ? "" : " open"}" data-visit="${v.id}">
    ${face(v.best, { weak: !v.identity_id, alt: who })}
    <span><div class="who">${esc(who)}</div>
      <div class="meta">${kind(v.identity_id ? v.kind : null)}<span>${esc(v.camera)}</span>
        <span>${v.faces} face${v.faces === 1 ? "" : "s"}</span>
        ${v.end_ts > v.start_ts ? `<span>${duration(v.end_ts - v.start_ts)}</span>` : ""}
        ${v.closed ? "" : `<span class="chip warn">AT THE DOOR</span>`}
        ${v.merged_from ? `<span class="chip" title="${esc(`Seen as ${v.merged_from}, merged in`)}">MERGED IN</span>`
          : v.how === "new" ? `<span class="chip warn">FIRST VISIT</span>` : ""}
        ${v.folded ? `<span class="chip" title="Stray faces from a separate short visit, folded into this one">${v.folded} FOLDED IN</span>` : ""}</div>
      ${v.caption ? `<div class="desc">${esc(v.caption.summary)}</div>` : ""}</span>
    <span class="when">${time(v.start_ts)}<small>${esc(ago(v.start_ts))}</small></span>
  </button>`;
}

function visitList(list, { groupByDay = false } = {}) {
  if (!list.length) return `<div class="empty">No visits yet.</div>`;
  let html = "", last = "";
  for (const v of list) {
    if (groupByDay) {
      const k = dayKey(v.start_ts);
      if (k !== last) { html += `<div class="day-head">${esc(dateLong(v.start_ts))}</div>`; last = k; }
    }
    html += visitRow(v);
  }
  return `<div class="visits">${html}</div>`;
}

function tile(k, v, s = "", sm = false) {
  return `<div class="tile"><div class="k">${esc(k)}</div><div class="v${sm ? " sm" : ""}">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
}

// ---------- overview

async function dashboard() {
  const s = await api("/api/summary");
  const totalId = s.identities.known + s.identities.unknown;
  const regMax = Math.max(1, ...s.regulars.map((r) => r.n));
  const knownShare = totalId ? (s.identities.known / totalId) * 100 : 0;
  setHTML(view, `
    <div class="page-head"><h1><small>${esc(dateLong(s.now))}</small>Front door intel</h1></div>
    <div class="tiles">
      ${tile("Visits today", `<span class="num">${s.today.visits}</span>`)}
      ${tile("People today", `<span class="num">${s.today.people}</span>`)}
      ${tile("Unknown today", `<span class="num">${s.today.unknown}</span>`, s.today.visits ? `${Math.round((s.today.unknown / s.today.visits) * 100)}% of visits` : "")}
      ${tile("New visitors · 7d", `<span class="num">${s.new_visitors_7d}</span>`, "first seen this week")}
      ${tile("Identities", `<span class="num">${totalId}</span>`,
        `<span class="split" style="width:100%;display:inline-flex;margin:4px 0 0" aria-hidden="true">
          <i style="width:${knownShare}%;background:var(--known)"></i><i style="flex:1;background:var(--unknown)"></i></span>
         ${s.identities.known} known · ${s.identities.unknown} visitors`)}
    </div>
    <div class="grid cols-dash">
      <div class="stack">
        ${s.open.length ? `<section class="panel"><h2>At the door now</h2>${visitList(s.open)}</section>` : ""}
        <section class="panel"><h2>Recent visits <a class="aside" href="#/visits">all visits →</a></h2>${visitList(s.recent.filter((v) => v.closed))}</section>
      </div>
      <div class="stack">
        <section class="panel"><h2>When people come <span class="aside">last 30 days</span></h2>${heatmap(s.heatmap_30d)}</section>
        <section class="panel"><h2>Visits per day <span class="aside">last 30 days</span></h2>
          ${bars(s.daily_30d.map((d) => ({ label: d.date.slice(5).split("-").reverse().join("/"), value: d.count })))}</section>
        <section class="panel"><h2>Regulars <span class="aside">last 30 days</span></h2>
          ${s.regulars.length ? s.regulars.map((r) => `
            <a class="reg" href="#/person/${r.id}">
              ${face(r.cover, { alt: r.name })}
              <span><b>${esc(r.name)}</b> <span class="muted mono" style="font-size:12px">· ${ago(r.last)}</span>
                <div class="meter"><i class="${r.kind === "known" ? "" : "u"}" style="width:${(r.n / regMax) * 100}%"></i></div></span>
              <span class="num">${r.n}</span>
            </a>`).join("") : `<div class="empty">Nobody yet.</div>`}
          <div class="legend"><i style="background:var(--known);border-radius:50%"></i>Known (Immich)<i style="background:var(--unknown);margin-left:10px"></i>Visitor</div>
        </section>
      </div>
    </div>`);
}

// ---------- people

const peopleState = { kind: "", sort: "last", q: "" };

async function people() {
  const st = peopleState;
  const segs = [["", "All"], ["known", "Known"], ["unknown", "Visitors"], ["named", "Named"], ["household", "Household"]];
  const kindParam = st.kind === "household" ? "" : st.kind;
  const ignored = st.kind === "household" ? "1" : "all";
  const list = await api(`/api/identities?kind=${kindParam}&ignored=${ignored}&sort=${st.sort}&q=${encodeURIComponent(st.q)}`);
  setHTML(view, `
    <div class="page-head"><h1><small>${list.length} ${list.length === 1 ? "identity" : "identities"}</small>People</h1></div>
    <div class="toolbar">
      <div class="seg" role="tablist">${segs.map(([k, l]) => `<button data-kind="${k}" class="${st.kind === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <select class="field" id="sort" aria-label="Sort">
        ${[["last", "Last seen"], ["visits", "Most visits"], ["first", "Newest"], ["name", "Name"]].map(([k, l]) => `<option value="${k}"${st.sort === k ? " selected" : ""}>${l}</option>`).join("")}
      </select>
      <input class="field" id="q" type="search" placeholder="Search names" value="${esc(st.q)}" style="flex:1;max-width:280px">
    </div>
    ${list.length ? `<div class="people">${list.map((p, n) => `
      <a class="card" href="#/person/${p.id}" style="animation-delay:${Math.min(n, 20) * 25}ms">
        ${face(p.cover, { osd: p.last_seen ? stamp(p.last_seen) : "", alt: p.name })}
        <div class="body"><div class="name">${esc(p.name)}</div>
          <div class="line">${kind(p.kind)}${p.ignored ? `<span class="chip">Household</span>` : ""}<span class="num">${p.visits} visit${p.visits === 1 ? "" : "s"}</span></div>
          <div class="line"><span class="muted" style="font-size:12px">${p.last_seen ? `seen ${ago(p.last_seen)}` : "no visits"}</span></div>
        </div></a>`).join("")}</div>` : `<div class="empty">No one matches.</div>`}`);
  view.querySelectorAll("[data-kind]").forEach((b) => b.onclick = () => { st.kind = b.dataset.kind; people(); });
  view.querySelector("#sort").onchange = (e) => { st.sort = e.target.value; people(); };
  const q = view.querySelector("#q");
  q.oninput = () => { clearTimeout(q._t); q._t = setTimeout(() => { st.q = q.value; people().then(() => { const n = view.querySelector("#q"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }); }, 250); };
}

// ---------- person dossier

async function person(id) {
  const p = await api(`/api/identities/${id}`);
  const st = p.stats;
  const w = st.window;
  const reg = st.regularity;
  const visits = await api(`/api/visits?identity=${id}&limit=100`);
  const cams = Object.entries(st.cameras).map(([c, n]) => `${esc(c)} <span class="num">${n}</span>`).join(" · ") || "—";
  setHTML(view, `
    <div class="dossier">
      ${face(p.cover, { osd: st.last_seen ? `${Object.keys(st.cameras)[0] || ""} ${stamp(st.last_seen)}` : "", alt: p.name })}
      <div>
        <div class="namebox"><h1 id="pname">${esc(p.name)}</h1></div>
        <div class="labels">${kind(p.kind, p.kind === "known" ? "Known · Immich" : "Visitor")}
          ${p.ignored ? `<span class="chip">HOUSEHOLD</span>` : ""}
          ${p.labels.map((l) => `<span class="chip" title="ImmichEye label">${esc(l)}</span>`).join("")}
          ${reg?.overdue ? `<span class="chip warn">OVERDUE</span>` : ""}</div>
        ${p.merged.length ? `<div class="muted" style="font-size:13px;margin-top:6px">Merged in: ${p.merged.map((m) => `${esc(m.name)} <span class="num">(${esc(date(m.ts))}, ${m.visits} visit${m.visits === 1 ? "" : "s"})</span>`).join(", ")}</div>` : ""}
        <textarea class="field" id="notes" rows="2" placeholder="Notes (e.g. Royal Mail, usually parcels)">${esc(p.notes)}</textarea>
        <div class="actions">
          <button class="btn" data-act="rename">Rename</button>
          <button class="btn" data-act="ignore">${p.ignored ? "Not household" : "Household"}</button>
          <button class="btn" data-act="merge">Merge into…</button>
          <button class="btn danger" data-act="forget">Forget</button>
        </div>
      </div>
    </div>
    <div class="tiles">
      ${tile("Visits", `<span class="num">${st.visits}</span>`, `${st.visits_30d} in last 30 days`)}
      ${tile("First seen", st.first_seen ? esc(date(st.first_seen)) : "—", st.first_seen ? ago(st.first_seen) : "", true)}
      ${tile("Last seen", st.last_seen ? esc(ago(st.last_seen)) : "—", st.last_seen ? dateTime(st.last_seen) : "", true)}
      ${tile("Usual time", w ? `<span class="num">${w.from}–${w.to}</span>` : "—",
        w ? `${Math.round(w.share * 100)}% of visits${w.days.length ? " · " + w.days.join(", ") : ""}` : "needs 3 visits", true)}
      ${tile("Rhythm", reg ? `every <span class="num">${reg.median_gap_days}</span>d` : "—",
        reg ? (reg.overdue ? `overdue · expected ${date(reg.next_expected)}` : `next ≈ ${date(reg.next_expected)}`) : "needs 4 visit days", true)}
      ${tile("At the door", st.avg_dwell_s != null ? `<span class="num">${duration(st.avg_dwell_s)}</span>` : "—", "average, faces seen", true)}
    </div>
    <div class="grid cols-2">
      <section class="panel"><h2>When they come</h2>${heatmap(st.heatmap)}</section>
      <section class="panel"><h2>Visits per week <span class="aside">12 weeks</span></h2>
        ${bars(st.weekly.map((x) => ({ label: x.week.slice(5).split("-").reverse().join("/"), value: x.count, tip: `Week of ${x.week} · ${x.count} visit${x.count === 1 ? "" : "s"}` })))}
        <div class="kv" style="margin-top:18px"><dt>Cameras</dt><dd>${cams}</dd></div></section>
    </div>
    <section class="panel" style="margin-top:18px"><h2>Faces <span class="aside">best per visit</span></h2>
      <div class="sheet">${p.faces.map((f) => face(f.id, { osd: `${f.camera} ${stamp(f.ts).slice(0, 8)}`, weak: !f.usable, alt: "" })).join("") || `<div class="empty">No crops.</div>`}</div></section>
    ${p.similar.length ? `<section class="panel" style="margin-top:18px"><h2>Looks like <span class="aside">possible same person</span></h2>
      ${p.similar.map((s) => `<div class="cand" style="cursor:default">${face(s.cover)}<span><b>${esc(s.name)}</b> ${kind(s.kind)}</span>
        <span style="display:flex;gap:8px;align-items:center"><span class="num muted">${s.score.toFixed(2)}</span>
        <a class="btn" href="#/person/${s.id}">View</a><button class="btn primary" data-merge-from="${s.id}">Merge here</button></span></div>`).join("")}</section>` : ""}
    <section class="panel" style="margin-top:18px"><h2>Visit log <span class="aside">${visits.length}${visits.length === 100 ? "+" : ""}</span></h2>${visitList(visits, { groupByDay: true })}</section>`);

  const notes = view.querySelector("#notes");
  notes.onchange = async () => { await api(`/api/identities/${id}`, { method: "PATCH", body: { notes: notes.value } }); toast("Notes saved"); };
  view.querySelector("[data-act=rename]").onclick = () => rename(p);
  view.querySelector("[data-act=ignore]").onclick = async () => {
    await api(`/api/identities/${id}`, { method: "PATCH", body: { ignored: !p.ignored } });
    toast(p.ignored ? "No longer household" : "Marked as household");
    person(id);
  };
  view.querySelector("[data-act=merge]").onclick = () => pickIdentity(`Merge ${p.name} into…`, id, async (target) => {
    await api(`/api/identities/${id}/merge`, { method: "POST", body: { into: target.id } });
    toast(`Merged into ${target.name}`);
    location.hash = `#/person/${target.id}`;
  });
  view.querySelector("[data-act=forget]").onclick = async () => {
    if (!(await confirmBox(`Forget ${p.name}?`, `Deletes ${st.visits} visits and every face of them. This can't be undone.${p.kind === "known" ? " Known people come back the next time ImmichEye names them." : ""}`, "Forget"))) return;
    await api(`/api/identities/${id}`, { method: "DELETE" });
    toast(`${p.name} forgotten`);
    location.hash = "#/people";
  };
  view.querySelectorAll("[data-merge-from]").forEach((b) => b.onclick = async () => {
    await api(`/api/identities/${b.dataset.mergeFrom}/merge`, { method: "POST", body: { into: id } });
    toast("Merged");
    person(id);
  });
}

function rename(p) {
  const h = view.querySelector("#pname");
  const input = document.createElement("input");
  input.className = "name-edit";
  input.value = p.name;
  h.replaceWith(input);
  input.focus();
  input.select();
  const done = async (save) => {
    if (save && input.value.trim() && input.value.trim() !== p.name) {
      await api(`/api/identities/${p.id}`, { method: "PATCH", body: { name: input.value.trim() } });
      toast("Renamed");
    }
    person(p.id);
  };
  input.onkeydown = (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); };
  input.onblur = () => done(true);
}

// ---------- visits

const visitsState = { kind: "", camera: "" };

async function visitsView() {
  const st = visitsState;
  const cams = await api("/api/cameras");
  const q = `kind=${st.kind}&camera=${encodeURIComponent(st.camera)}&limit=60`;
  let list = await api(`/api/visits?${q}`);
  const segs = [["", "All"], ["known", "Known"], ["unknown", "Visitors"], ["unidentified", "Unidentified"]];
  setHTML(view, `
    <div class="page-head"><h1><small>every arrival, newest first</small>Visits</h1></div>
    <div class="toolbar">
      <div class="seg">${segs.map(([k, l]) => `<button data-kind="${k}" class="${st.kind === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <select class="field" id="cam" aria-label="Camera"><option value="">All cameras</option>
        ${cams.filter((c) => c.visits).map((c) => `<option${st.camera === c.name ? " selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
    </div>
    <section class="panel" id="vlist">${visitList(list, { groupByDay: true })}</section>
    <div style="text-align:center;margin-top:16px"><button class="btn" id="more"${list.length < 60 ? " hidden" : ""}>Load older</button></div>`);
  view.querySelectorAll("[data-kind]").forEach((b) => b.onclick = () => { st.kind = b.dataset.kind; visitsView(); });
  view.querySelector("#cam").onchange = (e) => { st.camera = e.target.value; visitsView(); };
  view.querySelector("#more").onclick = async (e) => {
    const older = await api(`/api/visits?${q}&before=${list.at(-1).start_ts}`);
    list = list.concat(older);
    setHTML(view.querySelector("#vlist"), visitList(list, { groupByDay: true }));
    if (older.length < 60) e.target.hidden = true;
  };
}

// ---------- visit detail (modal)

function scene(c) {
  const bits = [c.role !== "unknown" ? `${c.role} (${c.role_confidence === "high" ? "sure" : c.role_confidence === "medium" ? "likely" : "a guess"})` : "",
    c.carrying?.length ? "carrying " + c.carrying.join(", ") : "", c.appearance, c.mood, c.weather, c.infrared ? "night (infrared)" : ""].filter(Boolean);
  return `<div class="scene"><p>${esc(c.summary)}</p>
    <div class="muted">${esc(bits.join(" · "))}${bits.length ? " · " : ""}<span title="${esc(`Described in ${c.seconds}s`)}">${esc(c.model)}</span></div></div>`;
}

async function openVisit(id) {
  const v = await api(`/api/visits/${id}`);
  const frame = v.frame ? `<img class="frame" src="/img/e/${encodeURIComponent(v.frame)}.jpg" alt="Frame">` : "";
  const who = v.name || "Unidentified";
  const how = { named: "Named by ImmichEye", matched: `Matched by face (${(v.match_score ?? 0).toFixed(2)})`, new: "First visit: new visitor", manual: "Assigned by hand", none: "No face good enough to identify" }[v.how] || "Still at the door";
  setHTML(modalBody, `
    <div class="m-head"><div>
        <div class="kind ${v.identity_id ? v.kind : "none"}">${esc(v.camera)} · ${esc(dateTime(v.start_ts))}${v.end_ts > v.start_ts ? " · " + duration(v.end_ts - v.start_ts) : ""}</div>
        <h3>${v.identity_id ? `<a href="#/person/${v.identity_id}" data-close>${esc(who)}</a>` : esc(who)}</h3>
        <div class="muted" style="font-size:13px">${esc(v.merged_from ? `${v.how === "new" ? "New visitor" : how.split(":")[0]}, as ${v.merged_from}; merged in` : how)}${v.folded ? ` · ${v.folded} stray face${v.folded === 1 ? "" : "s"} folded in from a separate visit` : ""}</div></div>
      <button class="x" data-close aria-label="Close">×</button></div>
    <div class="m-body">
      ${v.caption ? scene(v.caption) : ""}
      ${frame}
      <h2>Faces <span class="aside">${v.sightings.length}</span></h2>
      <div class="sheet">${v.sightings.map((s) => `<span data-tip="${esc(`${time(s.ts)} · ${s.px}px · det ${s.det.toFixed(2)}${s.label ? " · ImmichEye: " + s.label + " " + (s.score ?? 0).toFixed(2) : s.top?.length ? " · closest " + s.top[0][0] + " " + s.top[0][1].toFixed(2) : ""}${s.usable ? "" : " · too poor to identify"}${s.folded_from ? " · folded in from a separate visit" : ""}`)}">
        ${s.has_crop ? face(s.id, { osd: `${s.px}px${s.label ? " ✓" : ""}${s.folded_from ? " folded" : ""}`, weak: !s.usable }) : face(null)}</span>`).join("")}</div>
      <h2 style="margin-top:22px">Who is this?</h2>
      ${v.candidates.length ? v.candidates.map((c) => `<button class="cand" data-assign="${c.id}">${face(c.cover)}
        <span><b>${esc(c.name)}</b> ${kind(c.kind)}</span><span class="num muted">${c.score.toFixed(2)}</span></button>`).join("")
        : `<div class="muted" style="font-size:13px;margin-bottom:8px">${v.sightings.some((s) => s.usable) ? "Nobody else looks like this." : "No face good enough to compare."}</div>`}
      <div class="actions">
        <button class="btn" data-pick>Someone else…</button>
        <button class="btn" data-assign="new">New visitor</button>
        <button class="btn danger" data-delete>Not a visitor: delete</button>
      </div>
    </div>`);
  modal.showModal();
  modalBody.querySelectorAll("[data-close]").forEach((b) => b.onclick = () => modal.close());
  const assign = async (target, name) => {
    const r = await api(`/api/visits/${id}/assign`, { method: "POST", body: { identity: target } });
    modal.close();
    toast(name ? `Visit moved to ${name}` : "Visit moved to a new visitor");
    if (target === "new") location.hash = `#/person/${r.identity}`; else route();
  };
  modalBody.querySelectorAll("[data-assign]").forEach((b) => b.onclick = () => {
    const c = v.candidates.find((c) => String(c.id) === b.dataset.assign);
    assign(b.dataset.assign === "new" ? "new" : Number(b.dataset.assign), c?.name);
  });
  modalBody.querySelector("[data-pick]").onclick = () => pickIdentity("Move visit to…", v.identity_id, (t) => assign(t.id, t.name));
  modalBody.querySelector("[data-delete]").onclick = async () => {
    if (!(await confirmBox("Delete this visit?", "Removes the visit and its faces, e.g. a poster or reflection.", "Delete"))) return;
    await api(`/api/visits/${id}`, { method: "DELETE" });
    toast("Visit deleted");
    route();
  };
}

async function pickIdentity(title, exclude, onPick) {
  const list = (await api("/api/identities?ignored=all&sort=visits")).filter((i) => i.id !== exclude);
  setHTML(modalBody, `
    <div class="m-head"><h3>${esc(title)}</h3><button class="x" data-close aria-label="Close">×</button></div>
    <div class="m-body"><input class="field" id="pq" type="search" placeholder="Search" style="width:100%">
      <div class="picker-list" id="pl"></div></div>`);
  const draw = (q) => setHTML(modalBody.querySelector("#pl"), list
    .filter((i) => i.name.toLowerCase().includes(q.toLowerCase()))
    .map((i) => `<button class="cand" data-id="${i.id}">${face(i.cover)}<span><b>${esc(i.name)}</b> ${kind(i.kind)}</span>
      <span class="num muted">${i.visits} visits</span></button>`).join("") || `<div class="empty">No match.</div>`);
  draw("");
  if (!modal.open) modal.showModal();
  modalBody.querySelector("[data-close]").onclick = () => modal.close();
  const pq = modalBody.querySelector("#pq");
  pq.focus();
  pq.oninput = () => draw(pq.value);
  modalBody.querySelector("#pl").onclick = (e) => {
    const b = e.target.closest("[data-id]");
    if (!b) return;
    modal.close();
    onPick(list.find((i) => String(i.id) === b.dataset.id));
  };
}

function confirmBox(title, text, ok) {
  return new Promise((resolve) => {
    setHTML(modalBody, `<div class="m-head"><h3>${esc(title)}</h3></div>
      <div class="m-body"><p>${esc(text)}</p><div class="actions">
      <button class="btn danger" data-ok>${esc(ok)}</button><button class="btn" data-no>Cancel</button></div></div>`);
    if (!modal.open) modal.showModal();
    const done = (v) => { modal.close(); resolve(v); };
    modalBody.querySelector("[data-ok]").onclick = () => done(true);
    modalBody.querySelector("[data-no]").onclick = () => done(false);
    modal.addEventListener("cancel", () => resolve(false), { once: true });
  });
}

// ---------- review

async function review() {
  const pairs = await api("/api/suggestions");
  const side = (i) => `<div class="side">${face(i.cover, { alt: i.name })}<div style="min-width:0">
      <a href="#/person/${i.id}"><b>${esc(i.name)}</b></a><div>${kind(i.kind)}</div>
      <div class="muted mono" style="font-size:12px">${i.visits} visits · ${ago(i.last_seen)}</div></div></div>`;
  setHTML(view, `
    <div class="page-head"><h1><small>possible duplicates, most alike first</small>Review</h1></div>
    <section class="panel">${pairs.length ? pairs.map((p) => `
      <div class="pair">${side(p.a)}
        <div class="score"><div class="num">${p.score.toFixed(2)}</div><div class="muted mono" style="font-size:11px">SIMILARITY</div>
          <div class="btns"><button class="btn primary" data-merge="${p.a.id}:${p.b.id}">Same person</button>
            <button class="btn" data-dismiss="${p.a.id}:${p.b.id}">Different</button></div></div>
        ${side(p.b)}</div>`).join("") : `<div class="empty">Nothing to review. Visitors that look alike will show up here.</div>`}</section>`);
  view.querySelectorAll("[data-merge]").forEach((b) => b.onclick = async () => {
    const [a, c] = b.dataset.merge.split(":").map(Number);
    const pa = pairs.find((p) => p.a.id === a && p.b.id === c);
    // Keep the identity that's known, named or has more visits
    const rank = (i) => (i.kind === "known") * 4 + (i.named ? 2 : 0) + i.visits / 1e6;
    const [src, dst] = rank(pa.a) >= rank(pa.b) ? [pa.b, pa.a] : [pa.a, pa.b];
    await api(`/api/identities/${src.id}/merge`, { method: "POST", body: { into: dst.id } });
    toast(`${src.name} merged into ${dst.name}`);
    review(); livePoll();
  });
  view.querySelectorAll("[data-dismiss]").forEach((b) => b.onclick = async () => {
    const [a, c] = b.dataset.dismiss.split(":").map(Number);
    await api("/api/suggestions/dismiss", { method: "POST", body: { a, b: c } });
    review(); livePoll();
  });
}

// ---------- settings

async function settings() {
  const [st, cams] = await Promise.all([api("/api/status"), api("/api/cameras")]);
  const s = st.settings;
  setHTML(view, `
    <div class="page-head"><h1><small>uptime ${duration(st.uptime_s)}</small>Settings</h1></div>
    <div class="grid cols-2">
      <section class="panel"><h2>Cameras</h2>
        <p class="muted" style="margin-top:0;font-size:13px">Only enabled cameras build the visitor database. Faces from the others are never stored.</p>
        <table><thead><tr><th>Camera</th><th>Frames</th><th>Visits</th><th>Last seen</th><th>On</th><th></th></tr></thead><tbody>
        ${cams.map((c) => `<tr><td class="mono">${esc(c.name)}</td><td class="num">${c.events}</td><td class="num">${c.visits}</td>
          <td class="muted">${ago(c.last_seen)}</td>
          <td><input type="checkbox" class="switch" data-cam="${esc(c.name)}"${c.enabled ? " checked" : ""} aria-label="Enable ${esc(c.name)}"></td>
          <td>${c.visits ? "" : `<button class="btn" data-delcam="${esc(c.name)}" title="Remove from list">×</button>`}</td></tr>`).join("")}
        </tbody></table></section>
      <section class="panel stack"><div><h2>Feed</h2>
        <dl class="kv">
          <dt>Last event</dt><dd>${st.last_event ? ago(st.last_event) : "none since start"}</dd>
          <dt>Since start</dt><dd class="mono">${esc(Object.entries(st.counters).map(([k, v]) => `${k} ${v}`).join(" · ") || "—")}</dd>
          <dt>Stored</dt><dd class="mono">${st.totals.identities} identities · ${st.totals.visits} visits · ${st.totals.sightings} faces · ${(st.db_bytes / 1e6).toFixed(1)} MB</dd>
          <dt>ImmichEye push</dt><dd>${s.ingest_token_set ? "token set" : "<span class='chip warn'>NO TOKEN</span>"} · POST /api/events</dd>
          <dt>Home Assistant</dt><dd>${st.mqtt.enabled ? (st.mqtt.connected ? "MQTT connected" : "<span class='chip warn'>MQTT not connected</span>") : "off"}</dd>
        </dl></div>
        <div><h2>Matching</h2><dl class="kv">
          <dt>Face model</dt><dd class="mono">${esc(s.model)}</dd>
          <dt>Usable face</dt><dd class="mono">≥ ${s.min_face_px}px, detector ≥ ${s.min_det}</dd>
          <dt>Visit</dt><dd class="mono">ends ${s.visit_gap_s}s after last face · link ≥ ${s.visit_link}</dd>
          <dt>Identity match</dt><dd class="mono">≥ ${s.match_threshold}, margin ${s.match_margin}</dd>
          <dt>Suggest merge</dt><dd class="mono">≥ ${s.suggest_threshold}</dd>
        </dl></div>
        <div><h2>Privacy</h2><dl class="kv">
          <dt>Retention</dt><dd>${s.retention_days ? `visits older than ${s.retention_days} days are forgotten` : "kept forever"}</dd>
          <dt>Web UI</dt><dd>${{ authelia: "Authelia login", basic: "password (basic auth)" }[s.auth_mode] || "<span class='chip warn'>NO LOGIN</span>"}</dd>
          <dt>Descriptions</dt><dd>${s.caption_model ? `<span class="mono">${esc(s.caption_model)}</span> ${s.caption_url.startsWith("https://ollama.com") ? "on Ollama's cloud: each visit's frame leaves your network" : "on " + esc(s.caption_url)}` : "off"}</dd>
          <dt>Time zone</dt><dd class="mono">${esc(s.tz)}</dd>
        </dl><p class="muted" style="font-size:12px">Settings come from SmartEye's .env; change them there and restart.</p></div>
      </section>
    </div>`);
  view.querySelectorAll("[data-cam]").forEach((c) => c.onchange = async () => {
    await api(`/api/cameras/${encodeURIComponent(c.dataset.cam)}`, { method: "PATCH", body: { enabled: c.checked } });
    toast(`${c.dataset.cam} ${c.checked ? "enabled" : "disabled"}`);
  });
  view.querySelectorAll("[data-delcam]").forEach((b) => b.onclick = async () => {
    await api(`/api/cameras/${encodeURIComponent(b.dataset.delcam)}`, { method: "DELETE" });
    settings();
  });
}

// ---------- router

const routes = [
  [/^#?\/?$/, "dash", dashboard],
  [/^#\/people$/, "people", people],
  [/^#\/person\/(\d+)$/, "people", (m) => person(Number(m[1]))],
  [/^#\/visits$/, "visits", visitsView],
  [/^#\/review$/, "review", review],
  [/^#\/settings$/, "settings", settings],
];

async function route() {
  const h = location.hash || "#/";
  for (const [re, nav, fn] of routes) {
    const m = h.match(re);
    if (!m) continue;
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === nav));
    current = () => fn(m);
    if (!view.firstChild) setHTML(view, `<div class="skeleton"></div>`);
    try {
      await current();
    } catch (e) {
      setHTML(view, `<div class="empty">Couldn't load: ${esc(e.message)}</div>`);
    }
    return;
  }
  location.hash = "#/";
}

document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-visit]");
  if (v) openVisit(Number(v.dataset.visit));
});
modal.addEventListener("click", (e) => { if (e.target === modal) modal.close(); });
window.addEventListener("hashchange", () => { if (modal.open) modal.close(); view.replaceChildren(); route().then(() => scrollTo(0, 0)); });

(async () => {
  themeInit();
  wireTips();
  try {
    const st = await api("/api/status");
    setTz(st.settings.tz);
    if (st.settings.auth_mode === "authelia" && st.user) {
      const u = document.getElementById("user");
      u.textContent = `${st.user} · sign out`;
      u.hidden = false;
    }
  } catch {}
  route();
  livePoll();
  setInterval(livePoll, 15000);
  // Keep the overview fresh while it's open
  setInterval(() => { if ((location.hash || "#/").match(/^#?\/?$/) && !modal.open && document.visibilityState === "visible") dashboard(); }, 30000);
})();
