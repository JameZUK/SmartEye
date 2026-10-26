// Shared helpers: escaping, API calls, time formatting, toasts.
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

let TZ;
export const setTz = (tz) => { TZ = tz; };
const fmt = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...opts });

export const time = (ts) => fmt({ hour: "2-digit", minute: "2-digit" }).format(ts * 1000);
export const date = (ts) => fmt({ weekday: "short", day: "numeric", month: "short" }).format(ts * 1000);
export const dateLong = (ts) => fmt({ weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(ts * 1000);
export const dateTime = (ts) => `${date(ts)} ${time(ts)}`;
export const dayKey = (ts) => fmt({ year: "numeric", month: "2-digit", day: "2-digit" }).format(ts * 1000);
export const stamp = (ts) => fmt({ year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(ts * 1000).replace(",", "");

export function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  if (d < 14) return `${d} day${d === 1 ? "" : "s"} ago`;
  if (d < 60) return `${Math.floor(d / 7)} weeks ago`;
  return `${Math.floor(d / 30)} months ago`;
}

export function duration(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

export const img = (sid) => `/img/s/${sid}.jpg`;

export function face(sid, { osd = "", weak = false, alt = "" } = {}) {
  return `<span class="face${weak ? " weak" : ""}">${sid
    ? `<img src="${img(sid)}" alt="${esc(alt)}" loading="lazy">`
    : `<span class="noimg">NO FACE</span>`}${osd ? `<span class="osd">${esc(osd)}</span>` : ""}</span>`;
}

export function kind(k, label) {
  const cls = k === "known" ? "known" : k === "unknown" ? "unknown" : "none";
  const text = label ?? (k === "known" ? "Known" : k === "unknown" ? "Visitor" : "Unidentified");
  return `<span class="kind ${cls}">${esc(text)}</span>`;
}

// Views are HTML strings in which every dynamic value went through esc(). DOMParser never
// runs scripts, and the nodes are adopted into the live document.
const parser = new DOMParser();
export function setHTML(el, html) {
  const doc = parser.parseFromString(`<body>${html}</body>`, "text/html");
  el.replaceChildren(...doc.body.childNodes);
}
