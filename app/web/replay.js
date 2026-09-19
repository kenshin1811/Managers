/* Replay mode for the published walkthrough.

   There is no server behind the published page, so this file stands in for
   one. Everything it serves was recorded from the real engine by
   `python -m app.sim.capture`: the same /api/dashboard payloads the browser
   would have fetched, after the same engine calls the service makes. Nothing
   here decides anything -- it looks up what the Python already decided.

   What it cannot do is follow a path nobody recorded, so it says so out loud
   rather than letting a reader believe they are driving a live system. */

(function () {
  const data = JSON.parse(document.getElementById("replay-data").textContent);

  /* Shift the recording onto the reader's clock, once, at load. Every
     timestamp keeps its distance from every other one, so the driver is still
     arriving 45 minutes after "now" and the countdowns still run down. */
  const offsetMs = Date.now() - new Date(data.recorded_at).getTime();
  const timeFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: data.business_tz, hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const fullFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: data.business_tz, weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });

  let variant = "on_shift";
  let current = data.start[variant];
  let stalledView = false;
  let beforeStalled = null;

  function stampAt(ms) {
    const when = new Date(ms);
    return {
      utc: when.toISOString(),
      local: timeFmt.format(when),
      local_full: fullFmt.format(when).replace(/,\s*/, ", "),
    };
  }

  /** Walk a payload and move every timestamp in it onto the reader's clock. */
  function rebase(node) {
    if (Array.isArray(node)) return node.map(rebase);
    if (node && typeof node === "object") {
      if (typeof node.utc === "string" && "local" in node) {
        return stampAt(new Date(node.utc).getTime() + offsetMs);
      }
      const out = {};
      for (const [key, value] of Object.entries(node)) out[key] = rebase(value);
      return out;
    }
    return node;
  }

  /* The heartbeat is the one thing in the recording that genuinely repeats,
     so it keeps repeating: the sweep runs on its interval and the age resets,
     exactly as the real scheduler does. A frozen recording would otherwise
     drift past the stall threshold and start claiming the agent had died.
     When the reader asks to *see* a dead agent, it stays frozen on purpose. */
  function advanceHeartbeat(agent) {
    const now = Date.now();
    const base = new Date(agent.last_sweep.utc).getTime();
    let lastSweep = base;
    if (agent.status === "active" && !stalledView) {
      const interval = agent.sweep_interval_seconds * 1000;
      const elapsed = now - base;
      if (elapsed > interval) lastSweep = base + Math.floor(elapsed / interval) * interval;
    }
    agent.last_sweep = stampAt(lastSweep);
    agent.seconds_since_sweep = Math.max(0, Math.round((now - lastSweep) / 1000));
    agent.now = stampAt(now);
  }

  function frame(key) {
    const raw = data.frames[key];
    if (!raw) throw new Error("That step was not recorded.");
    const payload = rebase(JSON.parse(JSON.stringify(raw)));
    advanceHeartbeat(payload.agent);
    return payload;
  }

  function notRecorded() {
    return new Error(
      "This is a recorded walkthrough, and that particular path was not recorded. " +
        "Run it locally to go anywhere you like."
    );
  }

  window.__REPLAY__ = {
    get caption() {
      return (data.frames[current] || {})._caption || "";
    },

    /* Showing a dead agent is the one state nobody can wait around for, and
       it is the state the dashboard exists to make obvious, so it gets a
       switch. The frame underneath is kept so it can be switched back. */
    toggleStalled() {
      if (stalledView) {
        stalledView = false;
        current = beforeStalled || data.start[variant];
      } else {
        beforeStalled = current;
        stalledView = true;
        current = data.stalled;
      }
      return stalledView;
    },

    async handle(path, options = {}) {
      const body = options.body ? JSON.parse(options.body) : {};

      if (path === "/api/dashboard") return frame(current);

      if (path === "/api/demo/variants") {
        return {
          available: true,
          variants: {
            on_shift: "A trained colleague is already at work",
            call_in: "Nobody on shift can pack, so cover has to be called in",
          },
        };
      }

      if (path.startsWith("/api/demo/reset")) {
        const match = /variant=([a-z_]+)/.exec(path);
        variant = match ? match[1] : "on_shift";
        stalledView = false;
        beforeStalled = null;
        current = data.start[variant];
        if (!current) throw notRecorded();
        return {
          variant,
          description:
            variant === "on_shift"
              ? "a trained colleague is already at work"
              : "nobody on shift can pack",
        };
      }

      if (path === "/api/demo/leave") {
        const next = data.leave[current];
        if (!next) throw new Error("Mai has already gone. Load the kitchen again to replay it.");
        const actions = data.leave_actions[current] || [];
        current = next;
        return { plan: { task_plans: actions.map((action) => ({ action })) } };
      }

      if (/^\/coverage\/\d+\/reply$/.test(path)) {
        const key = `${body.accept ? "accept" : "decline"}:${body.employee_id}`;
        const move = (data.replies[current] || {})[key];
        if (!move) throw notRecorded();
        current = move.next;
        return { ok: true, status: body.accept ? "accepted" : "declined", message: move.message };
      }

      throw notRecorded();
    },
  };

  const stalledButton = document.getElementById("btn-stalled");
  if (stalledButton) {
    stalledButton.addEventListener("click", () => {
      const stopped = window.__REPLAY__.toggleStalled();
      stalledButton.textContent = stopped ? "Back to a live agent" : "Show a stopped agent";
      if (window.__dashboardRefresh) window.__dashboardRefresh();
    });
  }
})();
