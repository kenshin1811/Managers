/* Managers dashboard.

   No framework and no build step, on purpose: this page exists to prove the
   backend is alive, and a page that needs its own toolchain to run is a worse
   proof. It polls one endpoint, ticks its countdowns locally, and lets you
   answer a cover request to watch the agent react. */

const REFRESH_MS = 3000;

const $ = (id) => document.getElementById(id);
let state = null;
let variant = "on_shift";
let busy = false;

/* ---------- helpers ---------- */

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

/* ---------- agent status: the question the page exists to answer ---------- */

const STATUS_COPY = {
  active:   { title: "Agent active", cls: "is-active" },
  stalled:  { title: "Agent not responding", cls: "is-stalled" },
  starting: { title: "Agent starting", cls: "is-starting" },
  offline:  { title: "Service unreachable", cls: "is-stalled" },
};

function renderAgent(agent) {
  const copy = STATUS_COPY[agent.status] || STATUS_COPY.starting;
  $("beacon").className = `beacon ${copy.cls}`;
  $("hero-title").textContent = copy.title;
  $("nav-pill").className = `pill ${copy.cls}`;
  $("nav-pill-text").textContent = copy.title.replace("Agent ", "");

  const next = Math.max(0, agent.sweep_interval_seconds - (agent.seconds_since_sweep ?? 0));
  if (agent.status === "active") {
    $("hero-sub").textContent =
      `Last checked for unanswered cover ${ago(agent.seconds_since_sweep)} · next sweep in ${next}s`;
  } else if (agent.status === "stalled") {
    $("hero-sub").textContent =
      `No check for ${duration(agent.seconds_since_sweep)}. ` +
      "The background scheduler has probably stopped.";
  } else {
    $("hero-sub").textContent = "Waiting for the first sweep.";
  }

  const onOff = (flag) => (flag ? "On" : "Off");
  $("hero-facts").innerHTML = `
    <div><dt>Auto reassign</dt><dd>${onOff(agent.auto_reassign)}</dd></div>
    <div><dt>Cover requests</dt><dd>${onOff(agent.auto_coverage_requests)}</dd></div>
    <div><dt>Messaging</dt><dd>${esc(agent.messaging)}</dd></div>
    <div><dt>Weekly limit</dt><dd>${agent.limits.weekly_hours}h</dd></div>`;
  $("foot-note").textContent =
    `Times shown in ${agent.business_tz}. Server time ${agent.now.local_full}.`;
}

function renderStats(c) {
  const tiles = [
    { num: c.decisions_today, lbl: "Decisions today", cls: "" },
    { num: c.auto_reassigned, lbl: "Reassigned automatically", cls: "accent-green" },
    { num: c.open_coverage, lbl: "Cover requests open", cls: "accent-blue" },
    { num: c.needs_manager, lbl: "Waiting on a manager", cls: c.needs_manager ? "accent-red" : "" },
  ];
  $("stats").innerHTML = tiles.map((t) => `
    <div class="tile ${t.cls}">
      <div class="num mono">${t.num}</div>
      <div class="lbl">${esc(t.lbl)}</div>
    </div>`).join("");
}

/* ---------- escalations ---------- */

function renderEscalations(feed) {
  const items = feed.filter((d) => d.action === "escalated").slice(0, 4);
  $("escalations-section").hidden = items.length === 0;
  $("escalations").innerHTML = items.map((d) => `
    <article class="alert is-urgent fade-in">
      <h3>${esc(d.summary)}</h3>
      ${d.blockers.length ? `<div class="why">${d.blockers.map((b) => `<span>${esc(b)}</span>`).join("")}</div>` : ""}
      <p class="row-meta">${esc(d.at.local_full)} · decision #${d.id}</p>
    </article>`).join("");
}

/* ---------- cover requests ---------- */

const OFFER_TAG = {
  sent: ["blue", "Waiting"],
  accepted: ["green", "Accepted"],
  declined: ["", "Declined"],
  superseded: ["", "Stood down"],
  expired: ["", "No answer"],
};

function renderCoverage(coverage) {
  const open = coverage.filter((c) => c.status === "open");
  $("coverage-section").hidden = open.length === 0;
  $("coverage").innerHTML = open.map((c) => {
    const order = c.order_code ? ` · order ${esc(c.order_code)}` : "";
    return `
    <article class="cover fade-in" data-expires="${c.expires_at.utc}" data-created="${c.created_at.utc}">
      <div class="cover-head">
        <div>
          <h3>${esc(c.task_title)}${order}</h3>
          <p class="sub">${esc(c.vacating || "Someone")} is away · deadline ${esc(c.deadline.local)}${c.wave > 1 ? ` · wave ${c.wave}` : ""}</p>
        </div>
        <div class="timer" data-timer>
          <span class="big">—</span>
          until the agent escalates
        </div>
      </div>
      <div class="bar"><i data-bar style="width:100%"></i></div>
      <div class="asked">
        ${c.offers.map((o) => {
          const [cls, label] = OFFER_TAG[o.status] || ["", o.status];
          const pending = o.status === "sent";
          return `
          <div class="asked-row">
            <div class="asked-name">
              ${esc(o.employee_name)}
              <small>fit score ${o.score}${o.rank ? ` · asked ${o.rank}${o.rank === 1 ? "st" : o.rank === 2 ? "nd" : o.rank === 3 ? "rd" : "th"}` : ""}</small>
            </div>
            <span class="tag ${cls}">${esc(label)}</span>
            <div class="asked-actions">
              ${pending ? `
                <button class="btn btn-small" data-reply="accept" data-req="${c.id}" data-emp="${o.employee_id}">Accept</button>
                <button class="btn btn-quiet btn-small" data-reply="decline" data-req="${c.id}" data-emp="${o.employee_id}">Decline</button>` : ""}
            </div>
          </div>`;
        }).join("")}
      </div>
    </article>`;
  }).join("");
}

/* ---------- job board ---------- */

function renderBoard(board) {
  if (!board.length) {
    $("board").innerHTML = `<p class="empty">No live work scheduled.</p>`;
    return;
  }
  $("board").innerHTML = board.map((t) => {
    const driver = t.pickup_at
      ? `<span class="tag critical">driver ${esc(t.pickup_at.local)}</span>`
      : "";
    return `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${esc(t.title)} ${driver}</div>
        <div class="row-meta">
          ${esc(t.starts_at.local)}–${esc(t.due_at.local)}${t.station ? ` · ${esc(t.station)}` : ""}
        </div>
      </div>
      <div class="row-side">
        ${t.assignee ? esc(t.assignee) : `<span class="urgent">Unassigned</span>`}
      </div>
    </div>`;
  }).join("");
}

/* ---------- decision feed ---------- */

const ACTION_TAG = {
  auto_reassigned: ["green", "Reassigned"],
  coverage_requested: ["blue", "Asked around"],
  coverage_filled: ["green", "Covered"],
  escalated: ["critical", "Escalated"],
  leave_approved: ["green", "Leave approved"],
  leave_pending_coverage: ["orange", "Leave pending"],
  overridden: ["purple", "Manager override"],
  blocked_by_policy: ["orange", "Blocked by policy"],
  no_action_needed: ["", "No action"],
};

function renderFeed(feed) {
  if (!feed.length) {
    $("feed").innerHTML = `<p class="empty">Nothing decided yet.</p>`;
    return;
  }
  $("feed").innerHTML = feed.slice(0, 14).map((d) => {
    const [cls, label] = ACTION_TAG[d.action] || ["", d.action];
    const why = d.rejected.length
      ? `<div class="row-meta">Ruled out: ${d.rejected.map((r) => `${esc(r.name)} (${esc(r.reason)})`).join(", ")}</div>`
      : "";
    const by = d.actor.startsWith("manager") ? ` · by a manager` : "";
    return `
    <div class="row">
      <div class="row-main">
        <div class="row-title"><span class="tag ${cls}">${esc(label)}</span> ${esc(d.summary)}</div>
        <div class="row-meta">${esc(d.at.local)}${by}${d.considered ? ` · ${d.considered} people considered` : ""}</div>
        ${why}
      </div>
    </div>`;
  }).join("");
}

/* ---------- staff ---------- */

function renderStaff(staff, limits) {
  if (!staff.length) {
    $("staff").innerHTML = `<p class="empty">Nobody on the roster.</p>`;
    return;
  }
  $("staff").innerHTML = staff.map((p) => {
    const pct = Math.min(100, (p.hours_week / limits.weekly_hours) * 100);
    const cls = pct >= 100 ? "over" : pct >= 85 ? "warn" : "";
    const where = p.on_leave
      ? `<span class="tag orange">On leave</span>`
      : p.on_shift
        ? `<span class="tag green">On shift</span>`
        : `<span class="tag">Off shift</span>`;
    const skills = p.skills.map((s) => `${esc(s.code)} ${s.level}`).join(" · ") || "no skills on file";
    return `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${esc(p.name)} ${where}</div>
        <div class="row-meta">${skills}${p.live_tasks ? ` · ${p.live_tasks} job(s)` : ""}</div>
      </div>
      <div class="row-side">
        <span class="mono">${p.hours_week}h</span> / ${limits.weekly_hours}h week
        <div class="meter"><i class="${cls}" style="width:${pct}%"></i></div>
      </div>
    </div>`;
  }).join("");
}

/* ---------- countdowns, ticked locally so they move between polls ---------- */

function tick() {
  const now = Date.now();
  document.querySelectorAll(".cover").forEach((card) => {
    const expires = new Date(card.dataset.expires);
    const created = new Date(card.dataset.created);
    const timer = card.querySelector("[data-timer]");
    const bar = card.querySelector("[data-bar]");
    const { text, overdue } = countdown(expires, now);
    timer.querySelector(".big").textContent = overdue ? "due" : text;
    timer.classList.toggle("is-urgent", overdue || (expires - now) < 60000);
    const span = expires - created;
    const left = Math.max(0, Math.min(1, span > 0 ? (expires - now) / span : 0));
    bar.style.width = `${left * 100}%`;
    bar.classList.toggle("is-urgent", left < 0.25);
  });
}

/* ---------- polling ---------- */

async function refresh() {
  try {
    const data = await api("/api/dashboard");
    state = data;
    renderAgent(data.agent);
    renderStats(data.counters);
    renderEscalations(data.feed);
    renderCoverage(data.coverage);
    renderBoard(data.board);
    renderFeed(data.feed);
    renderStaff(data.staff, data.agent.limits);
    tick();
  } catch (err) {
    $("beacon").className = "beacon is-stalled";
    $("hero-title").textContent = STATUS_COPY.offline.title;
    $("hero-sub").textContent = "Cannot reach the service. Is uvicorn still running?";
    $("nav-pill").className = "pill is-stalled";
    $("nav-pill-text").textContent = "Offline";
  }
}

// The published walkthrough adds controls of its own (see replay.js) and needs
// to repaint immediately rather than wait out the poll interval.
window.__dashboardRefresh = refresh;

/* ---------- interaction ---------- */

async function withBusy(fn, label) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("#demo-bar button").forEach((b) => { b.disabled = true; });
  try {
    await fn();
  } catch (err) {
    toast(err.message || String(err), 5000);
  } finally {
    busy = false;
    document.querySelectorAll("#demo-bar button").forEach((b) => { b.disabled = false; });
    await refresh();
  }
}

document.addEventListener("click", (event) => {
  const reply = event.target.closest("[data-reply]");
  if (reply) {
    const accept = reply.dataset.reply === "accept";
    withBusy(async () => {
      const result = await api(`/coverage/${reply.dataset.req}/reply`, {
        method: "POST",
        body: JSON.stringify({ employee_id: Number(reply.dataset.emp), accept }),
      });
      toast(result.message);
    });
    return;
  }

  const seg = event.target.closest("#variant-picker button");
  if (seg) {
    variant = seg.dataset.variant;
    document.querySelectorAll("#variant-picker button").forEach((b) => {
      b.classList.toggle("on", b === seg);
      b.setAttribute("aria-checked", String(b === seg));
    });
    $("demo-note").textContent = variant === "on_shift"
      ? "Somebody trained is already at work, so the job just moves."
      : "Nobody on shift can pack, so the agent has to ring round.";
  }
});

$("btn-reset").addEventListener("click", () => withBusy(async () => {
  const result = await api(`/api/demo/reset?variant=${variant}`, { method: "POST" });
  toast(`Kitchen loaded — ${result.description.toLowerCase()}.`);
}));

$("btn-leave").addEventListener("click", () => withBusy(async () => {
  const result = await api("/api/demo/leave", { method: "POST" });
  const actions = result.plan.task_plans.map((p) => p.action.replace(/_/g, " "));
  toast(`Mai is off. Agent: ${actions.join(", ")}.`);
}));

$("demo-toggle").addEventListener("click", () => {
  const bar = $("demo-bar");
  bar.hidden = !bar.hidden;
});

/* ---------- boot ---------- */

(async function start() {
  try {
    const { available } = await api("/api/demo/variants");
    if (available) {
      $("demo-toggle").hidden = false;
      $("demo-bar").hidden = false;
    }
  } catch { /* demo is optional; the dashboard works without it */ }

  await refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(tick, 1000);
})();
