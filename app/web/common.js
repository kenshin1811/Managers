/* Helpers shared by the manager dashboard and the employee page.

   Both pages load this first. Keeping one copy is not tidiness for its own
   sake: api() below is also where the published walkthrough splices in its
   recordings, and a second copy of that would mean a page that silently
   stopped following the real thing. */

/** Shorthand for the one DOM lookup these pages ever do. */
const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function parseUtc(stampObj) {
  return stampObj ? new Date(stampObj.utc) : null;
}

/** "in 4m 12s" / "2m 5s ago" -- the sign is the whole point, so it leads. */
function countdown(target, now = Date.now()) {
  if (!target) return { text: "—", overdue: false, seconds: null };
  const seconds = Math.round((target.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  const h = Math.floor(abs / 3600);
  const m = Math.floor((abs % 3600) / 60);
  const s = abs % 60;
  const parts = h ? `${h}h ${m}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
  return { text: seconds < 0 ? `${parts} ago` : parts, overdue: seconds < 0, seconds };
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "an unknown time";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h`;
}

function ago(seconds) {
  return seconds === null || seconds === undefined ? "never" : `${duration(seconds)} ago`;
}

function toast(message, ms = 3600) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => { el.hidden = true; }, 320);
  }, ms);
}

async function api(path, options = {}) {
  // The published walkthrough has no backend to call: replay.js installs
  // window.__REPLAY__ and serves payloads recorded from the real engine. This
  // one branch is the only difference between the two, so everything below --
  // every render function, every rule of CSS -- stays a single copy that
  // cannot drift out of step with the live dashboard.
  if (window.__REPLAY__) return window.__REPLAY__.handle(path, options);
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${response.status} ${response.statusText}`);
  return body;
}
