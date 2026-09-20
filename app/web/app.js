/* The manager dashboard.

   No framework and no build step, on purpose: this page exists to prove the
   backend is alive, and a page that needs its own toolchain to run is a worse
   proof. It polls one endpoint, ticks its countdowns locally, and lets you
   answer a cover request to watch the agent react.

   Loads after common.js, which supplies $, esc, countdown, duration, toast
   and api. */

const REFRESH_MS = 3000;

let state = null;
let variant = "full_team";
let busy = false;

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

function renderStats(data) {
  const p = data.production;
  const late = p.runs.filter((r) => r.status === "missed").length;
  const risky = p.runs.filter((r) => r.status === "at_risk").length;
  const minutes = Object.values(p.stages).reduce((sum, s) => sum + s.minutes, 0);
  const tiles = [
    { num: p.units_outstanding.toLocaleString(), lbl: "Units still to pack", cls: "" },
    { num: `${Math.round(minutes / 60)}h`, lbl: "Work left on the bench", cls: "accent-blue" },
    { num: risky, lbl: "Vans cutting it fine", cls: risky ? "accent-orange" : "" },
    { num: late, lbl: "Vans that will be late", cls: late ? "accent-red" : "accent-green" },
  ];
  $("stats").innerHTML = tiles
    .map(
      (t) => `
    <div class="tile ${t.cls}">
      <div class="num mono">${esc(t.num)}</div>
      <div class="lbl">${esc(t.lbl)}</div>
    </div>`
    )
    .join("");
}

/* ---------- the vans ---------- */

const RUN_STATUS = {
  on_time: ["green", "On time"],
  at_risk: ["orange", "Cutting it fine"],
  missed: ["critical", "Will be late"],
  packed: ["green", "Packed"],
  unplanned: ["orange", "Not planned"],
};

function renderRuns(production) {
  if (!production.runs.length) {
    $("runs").innerHTML = `<p class="empty">No vans booked.</p>`;
    return;
  }
  $("runs").innerHTML = production.runs
    .map((r) => {
      const [cls, label] = RUN_STATUS[r.status] || ["", r.status];
      const packed = r.ordered ? Math.round(((r.ordered - r.outstanding) / r.ordered) * 100) : 100;
      const bar = r.status === "missed" ? "is-urgent" : "";
      const late = r.late_minutes
        ? `<span class="urgent">${r.late_minutes} min late</span>`
        : r.ready_at
          ? `ready ${esc(r.ready_at.local)}`
          : "nothing scheduled";
      return `
      <article class="cover run-card fade-in" data-departs="${r.departs_at.utc}">
        <div class="cover-head">
          <div>
            <h3>${esc(r.label)} run <span class="tag ${cls}">${esc(label)}</span></h3>
            <p class="sub">${esc(r.stops.join(" · "))}</p>
          </div>
          <div class="timer" data-run-timer>
            <span class="big">—</span>
            until it leaves at ${esc(r.departs_at.local)}
          </div>
        </div>
        <div class="bar"><i class="${bar}" style="width:${packed}%"></i></div>
        <div class="asked">
          <div class="asked-row">
            <div class="asked-name">
              ${r.outstanding.toLocaleString()} of ${r.ordered.toLocaleString()} units left
              <small>${r.open_tasks} job(s) · ${late}</small>
            </div>
            <span class="row-side">${esc(r.people.join(", ") || "nobody assigned")}</span>
          </div>
        </div>
      </article>`;
    })
    .join("");
}

function renderRemaining(production) {
  if (!production.remaining.length) {
    $("remaining").innerHTML = `<p class="empty">Everything on the book is packed.</p>`;
    return;
  }
  $("remaining").innerHTML = production.remaining
    .map(
      (row) => `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${esc(row.product)}
          <span class="tag ${row.kind === "frozen" ? "blue" : ""}">${esc(row.kind)}</span></div>
        <div class="row-meta">about ${row.minutes} min of packing</div>
      </div>
      <div class="row-side mono">${row.units.toLocaleString()}</div>
    </div>`
    )
    .join("");
}

/* ---------- every shop, line by line ----------

   The van cards answer "does it leave on time". This answers the other half:
   whether what goes on it is the right thing, in the right quantity, for the
   right address. A van that leaves at 18:00 two hundred jam donuts short is
   not a van that made it. */

function renderDeliveries(production) {
  const rows = production.deliveries || [];
  const short = production.shops_short || 0;
  $("shops-note").textContent = short
    ? `${short} of ${rows.length} still short of something. The right product, in the right quantity, at the right address.`
    : `All ${rows.length} complete. The right product, in the right quantity, at the right address.`;

  if (!rows.length) {
    $("deliveries").innerHTML = `<p class="empty">No open orders.</p>`;
    return;
  }

  $("deliveries").innerHTML = rows
    .map((row) => {
      const [cls, label] = RUN_STATUS[row.status] || ["", row.status];
      const lines = row.lines
        .map(
          (line) => `
        <div class="asked-row">
          <div class="asked-name">${esc(line.product)}
            <small>${line.short ? `${line.short} still to pack` : "packed"}</small></div>
          <div class="row-side mono ${line.short ? "" : "muted"}">
            ${line.packed.toLocaleString()} / ${line.ordered.toLocaleString()}</div>
        </div>`
        )
        .join("");
      return `
      <article class="cover run-card fade-in">
        <div class="cover-head">
          <div>
            <h3>${esc(row.where)} <span class="tag ${cls}">${esc(label)}</span></h3>
            <p class="sub">${esc(row.code)} · ${esc(row.channel)} · ${esc(row.run)} van,
              leaves ${esc(row.departs_at.local)}</p>
          </div>
          <div class="timer ${row.short ? "is-urgent" : ""}">
            <span class="big">${row.short ? row.short.toLocaleString() : "✓"}</span>
            ${row.short ? "units short" : "complete"}
          </div>
        </div>
        <div class="asked">${lines}</div>
      </article>`;
    })
    .join("");
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

const STAGE_TAG = {
  retrieve: ["blue", "Freezer"],
  pack: ["", "Pack"],
  sort: ["purple", "Sort"],
  dispatch: ["critical", "Load"],
};

function renderBoard(board) {
  const live = board.filter((t) => t.stage);
  if (!live.length) {
    $("board").innerHTML = `<p class="empty">Nothing on the bench.</p>`;
    return;
  }
  $("board").innerHTML = live
    .slice(0, 16)
    .map((t) => {
      const [cls, label] = STAGE_TAG[t.stage] || ["", t.stage];
      return `
      <div class="row">
        <div class="row-main">
          <div class="row-title"><span class="tag ${cls}">${esc(label)}</span> ${esc(t.title)}</div>
          <div class="row-meta">
            ${esc(t.starts_at.local)}–${esc(t.due_at.local)}${t.run ? ` · ${esc(t.run)} van` : ""}
          </div>
        </div>
        <div class="row-side">
          ${t.assignee ? esc(t.assignee) : `<span class="urgent">Unassigned</span>`}
        </div>
      </div>`;
    })
    .join("");
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
  document.querySelectorAll("[data-departs]").forEach((card) => {
    const timer = card.querySelector("[data-run-timer] .big");
    if (!timer) return;
    const { text, overdue } = countdown(new Date(card.dataset.departs), now);
    timer.textContent = overdue ? "gone" : text;
    timer.closest(".timer").classList.toggle("is-urgent", overdue || (new Date(card.dataset.departs) - now) < 1800000);
  });
  // Scoped to cover requests specifically: the van cards reuse the same
  // surface styling, and an unscoped ".cover" swept them up and threw.
  document.querySelectorAll(".cover[data-expires]").forEach((card) => {
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
    renderStats(data);
    renderRuns(data.production);
    renderDeliveries(data.production);
    renderRemaining(data.production);
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
    $("demo-note").textContent =
      variant === "full_team"
        ? "Three packers rostered. The agent plans the evening and every van makes it."
        : "Two packers rostered. Watch which vans start slipping.";
  }
});

$("btn-reset").addEventListener("click", () => withBusy(async () => {
  const result = await api(`/api/demo/reset?variant=${variant}`, { method: "POST" });
  toast(`Factory loaded: ${result.units.toLocaleString()} units, ${result.tasks} jobs planned.`);
}));

$("btn-leave").addEventListener("click", () => withBusy(async () => {
  const result = await api("/api/demo/disrupt", { method: "POST" });
  toast(result.speech);
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
