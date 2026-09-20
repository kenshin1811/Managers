/* The employee page.

   One person's own work, and nothing about anyone else. Written for a phone
   held in one hand: one column, large targets, and the only urgent thing --
   a request to cover somebody's job -- at the top where it cannot be missed.

   Loads after common.js, which supplies $, esc, countdown, duration, toast
   and api. */

const REFRESH_MS = 4000;

let me = null;
let employeeId = null;
let token = null;
let busy = false;

/* ---------- who is looking ---------- */

function query(name) {
  return new URLSearchParams(location.search).get(name);
}

function withToken(path) {
  return token ? `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : path;
}

async function resolveIdentity() {
  token = query("token");
  if (token) {
    // The page has no secret, so it cannot read the link itself. It asks.
    const who = await api(`/api/me/whoami?token=${encodeURIComponent(token)}`);
    return who.employee_id;
  }
  const explicit = query("employee");
  if (explicit) return Number(explicit);

  const people = await api("/api/me/switchable");
  if (!people.length) throw new Error("Nobody is on the roster yet.");
  return people[0].id;
}

async function buildSwitcher(currentId) {
  let people = [];
  try {
    people = await api("/api/me/switchable");
  } catch {
    return; // Not dry run: identity comes from the link, and cannot be swapped.
  }
  const select = $("person-select");
  select.innerHTML = people
    .map(
      (p) =>
        `<option value="${p.id}"${p.id === currentId ? " selected" : ""}>${esc(p.name)}</option>`
    )
    .join("");
  select.addEventListener("change", () => {
    employeeId = Number(select.value);
    const url = new URL(location.href);
    url.searchParams.set("employee", String(employeeId));
    history.replaceState(null, "", url);
    refresh();
  });
  $("person").hidden = false;
}

/* ---------- rendering ---------- */

function renderHeader(data) {
  const first = data.me.name.split(" ")[0];
  $("greeting").textContent = `Hi, ${first}`;
  const where = $("where");
  where.hidden = false;
  if (data.me.on_leave) {
    where.className = "tag orange";
    where.textContent = "On leave";
  } else if (data.me.on_shift) {
    where.className = "tag green";
    where.textContent = "On shift";
  } else {
    where.className = "tag";
    where.textContent = "Off shift";
  }

  const bits = [];
  if (data.shift) {
    bits.push(
      data.shift.in_progress
        ? `${esc(data.shift.name)} until ${data.shift.ends_at.local}`
        : `Next: ${esc(data.shift.name)} at ${data.shift.starts_at.local}`
    );
  } else {
    bits.push("Nothing rostered today");
  }
  if (data.hours.on_the_clock && data.hours.clocked_in_at) {
    bits.push(`clocked in at ${data.hours.clocked_in_at.local}`);
  }
  $("me-sub").textContent = bits.join(" · ");
}

function renderAsks(data) {
  const live = data.cover_requests.filter((r) => r.my_status === "sent");
  const settled = data.cover_requests.filter((r) => r.my_status !== "sent").slice(0, 2);
  $("asks-section").hidden = live.length === 0 && settled.length === 0;

  const liveCards = live.map((r) => {
    // Job titles usually already name the order ("Pack order 1043"), and
    // "Pack order 1043 for order 1043" reads like a bug.
    const order =
      r.order_code && !r.task_title.includes(r.order_code)
        ? ` for order ${esc(r.order_code)}`
        : "";
    return `
    <article class="ask fade-in" data-expires="${r.expires_at.utc}" data-created="${r.created_at.utc}">
      <div class="ask-body">
        <h2>${esc(r.task_title)}${order}</h2>
        <dl>
          <dt>When</dt><dd>${r.starts_at.local}–${r.due_at.local} · about ${r.estimated_minutes} min</dd>
          <dt>Deadline</dt><dd>${r.deadline.local}${r.order_code ? " — the driver won't wait" : ""}</dd>
          ${r.station ? `<dt>Where</dt><dd>${esc(r.station)}</dd>` : ""}
        </dl>
        <p class="why">Asked because ${esc(r.asked_because || "a colleague is unavailable")}.
          <span data-expiry-note></span></p>
      </div>
      <div class="ask-actions">
        <button class="btn btn-big" data-reply="accept" data-req="${r.id}">I can cover</button>
        <button class="btn btn-quiet btn-big" data-reply="decline" data-req="${r.id}">Can't this time</button>
      </div>
    </article>`;
  });

  const settledCards = settled.map((r) => {
    const mine = r.my_status === "accepted";
    const label = { accepted: "You're covering this", declined: "You said no thanks" }[
      r.my_status
    ] || (r.filled_by ? `${esc(r.filled_by)} took it` : "No longer needed");
    return `
    <article class="ask" style="border-top-color: var(--${mine ? "green" : "text-3"})">
      <div class="settled">
        <span class="tag ${mine ? "green" : ""}">${esc(label)}</span>
        <strong>${esc(r.task_title)}</strong>
      </div>
    </article>`;
  });

  $("asks").innerHTML = [...liveCards, ...settledCards].join("");
}

const MY_STAGE = {
  retrieve: ["blue", "Freezer"],
  pack: ["", "Pack + label"],
  sort: ["purple", "Sort"],
  dispatch: ["critical", "Load van"],
};

function renderJobs(data) {
  if (!data.tasks.length) {
    $("jobs").innerHTML = `<p class="empty">Nothing assigned to you right now.</p>`;
    return;
  }
  $("jobs").innerHTML = data.tasks
    .map((t) => {
      const [cls, label] = MY_STAGE[t.stage] || ["", t.station || "Job"];
      const van = t.pickup_at
        ? `<span class="tag critical">van ${esc(t.pickup_at.local)}</span>`
        : t.run
          ? `<span class="tag">${esc(t.run)} van</span>`
          : "";
      // Waiting on the stage in front is not idleness, and saying so stops
      // somebody starting a job whose stock is still in the freezer.
      const waiting = t.blocked
        ? `<div class="row-meta">Waiting on: ${esc(t.waiting_for || "the job before it")}</div>`
        : "";
      return `
      <div class="row">
        <div class="row-main">
          <div class="row-title"><span class="tag ${cls}">${esc(label)}</span> ${esc(t.title)} ${van}</div>
          <div class="row-meta">
            ${esc(t.starts_at.local)}–${esc(t.due_at.local)}${
              t.quantity ? ` · ${t.quantity.toLocaleString()} units` : ""
            }
          </div>
          ${waiting}
        </div>
      </div>`;
    })
    .join("");
}

function renderHours(data) {
  const h = data.hours;
  const pct = Math.min(100, (h.week / h.weekly_hours) * 100);
  const cls = pct >= 100 ? "over" : pct >= 85 ? "warn" : "";
  const clockLabel = h.on_the_clock ? "Clock out" : "Clock in";
  $("hours").innerHTML = `
    <div class="hours-row">
      <div>
        <div class="hours-num mono">${h.today}h</div>
        <div class="row-meta">today, of ${h.daily_hours}h</div>
      </div>
      <div style="text-align:right">
        <div class="hours-num mono">${h.week}h</div>
        <div class="row-meta">this week, of ${h.weekly_hours}h</div>
      </div>
    </div>
    <div class="meter"><i class="${cls}" style="width:${pct}%"></i></div>
    ${
      data.can.clock
        ? `<div class="stacked-actions">
             <button class="btn ${h.on_the_clock ? "btn-quiet" : ""} btn-big" id="btn-clock">
               ${clockLabel}
             </button>
           </div>`
        : `<p class="note note-flush">Clocking in and out works when you
             run the service yourself.</p>`
    }`;
}

const LEAVE_TAG = {
  approved: ["green", "Approved"],
  pending_coverage: ["orange", "Waiting on cover"],
  denied: ["critical", "Denied"],
  cancelled: ["", "Cancelled"],
};

function renderLeave(data) {
  const rows = data.leave
    .map((l) => {
      const [cls, label] = LEAVE_TAG[l.status] || ["", l.status];
      const by = l.decided_by === "system" ? "decided automatically" : l.decided_by || "";
      return `
      <div class="row">
        <div class="row-main">
          <div class="row-title"><span class="tag ${cls}">${esc(label)}</span>
            ${l.starts_at.local}–${l.ends_at.local}</div>
          <div class="row-meta">${esc(l.reason || l.leave_type)}${by ? ` · ${esc(by)}` : ""}</div>
        </div>
      </div>`;
    })
    .join("");

  const handovers = data.handovers.length
    ? `<div class="row"><div class="row-main">
         <div class="row-title">While you're away</div>
         ${data.handovers
           .map(
             (h) =>
               `<div class="row-meta">${esc(h.taken_by)} has ${esc(h.task_title)}${
                 h.how === "volunteered" ? " (volunteered)" : ""
               }</div>`
           )
           .join("")}
       </div></div>`
    : "";

  const form = data.can.request_leave
    ? `<form class="leave-form" id="leave-form">
         <label>From
           <input type="datetime-local" id="leave-from" required>
         </label>
         <label>Until
           <input type="datetime-local" id="leave-to" required>
         </label>
         <label>Reason
           <textarea id="leave-reason" placeholder="Family emergency"></textarea>
         </label>
         <button class="btn btn-big" type="submit">Request time off</button>
       </form>`
    : `<p class="note">Requesting time off works when you run the service yourself.</p>`;

  $("leave").innerHTML =
    (rows || handovers ? `<div class="list">${rows}${handovers}</div>` : "") + form;

  if (data.can.request_leave) prefillLeave(data);
}

/** Default to leaving shortly, ending when the shift would have. */
function prefillLeave(data) {
  const pad = (n) => String(n).padStart(2, "0");
  const local = (d) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
      d.getMinutes()
    )}`;
  const from = new Date(Date.now() + 25 * 60 * 1000);
  const until = data.shift ? new Date(data.shift.ends_at.utc) : new Date(from.getTime() + 4 * 3600e3);
  $("leave-from").value = local(from);
  $("leave-to").value = local(until > from ? until : new Date(from.getTime() + 3600e3));
}

function renderAgent(agent) {
  const line = $("agent-line");
  if (agent.status === "active") {
    line.className = "agent-line is-active";
    $("agent-text").textContent = `Cover is being watched — last checked ${ago(
      agent.seconds_since_sweep
    )}.`;
  } else if (agent.status === "stalled") {
    line.className = "agent-line is-stalled";
    $("agent-text").textContent = `Nothing has checked for cover in ${duration(
      agent.seconds_since_sweep
    )}. Tell a manager.`;
  } else {
    line.className = "agent-line";
    $("agent-text").textContent = "Starting up.";
  }
}

/* ---------- countdowns ---------- */

function tick() {
  const now = Date.now();
  document.querySelectorAll(".ask[data-expires]").forEach((card) => {
    const note = card.querySelector("[data-expiry-note]");
    if (!note) return;
    const { text, overdue } = countdown(new Date(card.dataset.expires), now);
    note.textContent = overdue
      ? "A manager has been told."
      : `We'll ask someone else in ${text}.`;
    card.classList.toggle("is-urgent", overdue || new Date(card.dataset.expires) - now < 120000);
  });
}

/* ---------- loop ---------- */

async function refresh() {
  try {
    const data = await api(withToken(`/api/me/${employeeId}`));
    me = data;
    renderHeader(data);
    renderAsks(data);
    renderJobs(data);
    renderHours(data);
    renderLeave(data);
    renderAgent(data.agent);
    tick();
  } catch (err) {
    $("greeting").textContent = "Can't reach the service";
    $("me-sub").textContent = err.message || "Try again in a moment.";
  }
}

async function act(fn) {
  if (busy) return;
  busy = true;
  // Scoped to this page's own controls, and re-enabled by hand afterwards.
  // Disabling every button on the document left the ones this page does not
  // re-render stuck off for good -- which only showed up once the employee
  // page shared a document with the manager's.
  const buttons = [...document.querySelectorAll(".me-page button")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (err) {
    toast(err.message || String(err), 5000);
  } finally {
    busy = false;
    buttons.forEach((b) => {
      if (b.isConnected) b.disabled = false;
    });
    await refresh();
  }
}

document.addEventListener("click", (event) => {
  const reply = event.target.closest("[data-reply]");
  if (reply) {
    const accept = reply.dataset.reply === "accept";
    act(async () => {
      const result = await api(`/coverage/${reply.dataset.req}/reply`, {
        method: "POST",
        body: JSON.stringify({ employee_id: employeeId, accept }),
      });
      toast(result.message);
    });
    return;
  }

  if (event.target.closest("#btn-clock")) {
    const out = me?.hours.on_the_clock;
    act(async () => {
      await api(`/timeclock/clock-${out ? "out" : "in"}`, {
        method: "POST",
        body: JSON.stringify({ employee_id: employeeId }),
      });
      toast(out ? "Clocked out. Thanks." : "Clocked in.");
    });
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id !== "leave-form") return;
  event.preventDefault();
  const from = $("leave-from").value;
  const to = $("leave-to").value;
  if (!from || !to) return;
  act(async () => {
    const result = await api("/leave-requests", {
      method: "POST",
      body: JSON.stringify({
        employee_id: employeeId,
        starts_at: new Date(from).toISOString(),
        ends_at: new Date(to).toISOString(),
        leave_type: "personal",
        reason: $("leave-reason").value || "",
      }),
    });
    const actions = (result.plan.task_plans || []).map((p) => p.action);
    const covered = actions.filter((a) => a === "auto_reassigned" || a === "coverage_requested");
    toast(
      covered.length
        ? `Noted. Your ${covered.length} job(s) are being covered.`
        : "Noted. Nothing of yours clashes with it."
    );
  });
});

// The published walkthrough switches roles without reloading, and needs to
// land on whoever the current moment is about.
window.__employeeView = {
  get person() {
    return employeeId;
  },
  setPerson(id) {
    employeeId = Number(id);
    const select = $("person-select");
    if (select) select.value = String(employeeId);
    return refresh();
  },
};

/* ---------- boot ---------- */

(async function start() {
  try {
    employeeId = await resolveIdentity();
    await buildSwitcher(employeeId);
  } catch (err) {
    $("greeting").textContent = "Can't open this page";
    $("me-sub").textContent = err.message || "The link may have expired.";
    return;
  }
  // Arriving from an accept or decline link: say what happened, once, then
  // take it out of the URL so a refresh does not repeat it.
  const message = query("msg");
  if (message) {
    toast(message, 5000);
    const url = new URL(location.href);
    url.searchParams.delete("msg");
    history.replaceState(null, "", url);
  }

  await refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(tick, 1000);
})();
