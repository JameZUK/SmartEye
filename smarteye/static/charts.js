// Small charts built from divs: a weekday x hour heatmap and a bar series.
// Every mark has a hover tooltip and an aria-label, so nothing relies on colour alone.
import { esc } from "./util.js";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const STEPS = 8;

function step(v, max) {
  if (!v) return 0;
  return Math.max(1, Math.ceil((v / max) * STEPS));
}

export function heatmap(grid, { unit = "visit" } = {}) {
  const max = Math.max(1, ...grid.flat());
  const total = grid.flat().reduce((a, b) => a + b, 0);
  let html = `<div class="heat" role="img" aria-label="${total} ${unit}s by weekday and hour">`;
  grid.forEach((row, d) => {
    html += `<span class="rl">${DAYS[d]}</span>`;
    row.forEach((v, h) => {
      const label = `${DAYS[d]} ${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00 · ${v} ${unit}${v === 1 ? "" : "s"}`;
      html += `<span class="c" style="background:var(--seq-${step(v, max)})" data-tip="${esc(label)}" aria-label="${esc(label)}"></span>`;
    });
  });
  html += `<span></span>`;
  for (let h = 0; h < 24; h++) html += `<span class="hl">${h % 3 === 0 ? String(h).padStart(2, "0") : ""}</span>`;
  html += `</div>`;
  const legend = Array.from({ length: STEPS + 1 }, (_, i) => `<i style="background:var(--seq-${i})"></i>`).join("");
  html += `<div class="legend"><span>0</span>${legend}<span>${max}</span><span style="margin-left:auto">${total} total</span></div>`;
  return html;
}

// items: [{label, value, tip}] oldest first; the axis shows the first, middle and last labels
export function bars(items, { unit = "visit", height = 120 } = {}) {
  const max = Math.max(1, ...items.map((i) => i.value));
  const body = items.map((i) => {
    const tip = i.tip || `${i.label} · ${i.value} ${unit}${i.value === 1 ? "" : "s"}`;
    return `<span class="b${i.value ? "" : " zero"}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"><i style="height:${(i.value / max) * 100}%"></i></span>`;
  }).join("");
  const mid = items[Math.floor(items.length / 2)];
  return `<div class="bars" style="height:${height}px" role="img" aria-label="${items.length} bars, max ${max}">${body}</div>
    <div class="axis"><span>${esc(items[0]?.label || "")}</span><span>${esc(mid?.label || "")}</span><span>${esc(items.at(-1)?.label || "")}</span></div>`;
}

// One shared tooltip for anything with data-tip ("Title · detail"). Built with text nodes:
// tips can carry names people typed.
export function wireTips(root = document) {
  const tip = document.getElementById("tip");
  root.addEventListener("pointerover", (e) => {
    const t = e.target.closest("[data-tip]");
    if (!t) return;
    const [head, ...rest] = t.dataset.tip.split(" · ");
    const b = document.createElement("b");
    b.textContent = head;
    tip.replaceChildren(b);
    if (rest.length) tip.append(document.createElement("br"), rest.join(" · "));
    tip.hidden = false;
  });
  root.addEventListener("pointermove", (e) => {
    if (tip.hidden) return;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  });
  root.addEventListener("pointerout", (e) => {
    if (e.target.closest("[data-tip]")) tip.hidden = true;
  });
}
