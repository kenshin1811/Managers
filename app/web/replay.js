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

  let variant = "full_team";
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

  /* A recorded stamp carries only its UTC value; the local strings below are
     rendered fresh, in the reader's own browser, from the shifted moment. */
  const STAMP_KEYS = new Set(["utc", "local", "local_full"]);

  /** Walk a payload and move every timestamp in it onto the reader's clock. */
  function rebase(node) {
    if (Array.isArray(node)) return node.map(rebase);
    if (node && typeof node === "object") {
      if (typeof node.utc === "string" && Object.keys(node).every((k) => STAMP_KEYS.has(k))) {
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

  /** One person's own view of the current moment. */
  function meFrame(employeeId) {
    const perFrame = data.me_frames[current] || {};
    const raw = perFrame[String(employeeId)];
    if (!raw) throw new Error("That person was not recorded at this step.");
    const payload = rebase(JSON.parse(JSON.stringify(raw)));
    advanceHeartbeat(payload.agent);
    // Clocking in and out was never recorded, and only one person's leave was,
    // so the page shows those read-only rather than offering a dead button.
    payload.can = {
      switch_people: true,
      clock: false,
      request_leave:
        String(employeeId) === String(data.leave_employee_id) && Boolean(data.leave[current]),
    };
    return payload;
  }

  /** Whoever this step is actually about, for the role switch to land on. */
  function principal() {
    const perFrame = data.me_frames[current] || {};
    const waiting = Object.entries(perFrame).find(([, view]) =>
      view.cover_requests.some((r) => r.my_status === "sent")
    );
    if (waiting) return Number(waiting[0]);
    const leaving = Object.entries(perFrame).find(([, view]) => view.leave.length);
    if (leaving) return Number(leaving[0]);
    return (data.roster[0] || {}).id;
  }

  function frame(key) {
    const raw = data.frames[key];
    if (!raw) throw new Error("That step was not recorded.");
    const payload = rebase(JSON.parse(JSON.stringify(raw)));
    advanceHeartbeat(payload.agent);
    return payload;
  }

  /** The key a spoken phrase is filed under, matching app.sim.capture. */
  function normalise(transcript) {
    return transcript.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
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

    /** Whoever this step is about, so the role switch lands somewhere useful. */
    get principal() {
      return principal();
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

      if (path === "/api/me/switchable") return data.roster;

      const mine = /^\/api\/me\/(\d+)/.exec(path);
      if (mine) return meFrame(mine[1]);

      if (path.startsWith("/api/me/whoami")) {
        throw new Error("The walkthrough has no signed links -- pick a person instead.");
      }

      if (path === "/leave-requests") {
        const next = data.leave[current];
        if (!next || String(body.employee_id) !== String(data.leave_employee_id)) {
          throw notRecorded();
        }
        const actions = data.leave_actions[current] || [];
        current = next;
        return { plan: { task_plans: actions.map((action) => ({ action })) } };
      }

      if (path === "/api/console/vocabulary") return data.vocabulary;

      /* The console. Every phrase below was spoken into the real parser while
         recording, and the answer is the sentence the engine gave back --
         which is why the hints offer these and nothing else. */
      if (path === "/api/console/command") {
        const said = normalise(body.transcript || "");
        const move = (data.commands[current] || {})[said];
        if (!move) {
          return {
            ok: false,
            kind: "unknown",
            replanned: false,
            speech:
              "That one was not recorded. Try one of the suggestions below, " +
              "or run it locally to say anything you like.",
            detail: {},
          };
        }
        current = move.next;
        return { ok: move.ok, kind: "voice", replanned: true, speech: move.speech, detail: {} };
      }

      if (path === "/api/demo/variants") {
        return {
          available: true,
          variants: {
            full_team: "Three packers rostered: the evening fits",
            short_team: "Two packers rostered: the vans start slipping",
          },
        };
      }

      if (path.startsWith("/api/demo/reset")) {
        const match = /variant=([a-z_]+)/.exec(path);
        variant = match ? match[1] : "full_team";
        stalledView = false;
        beforeStalled = null;
        current = data.start[variant];
        if (!current) throw notRecorded();
        return {
          variant,
          description:
            variant === "full_team"
              ? "three packers rostered, and the evening fits"
              : "two packers rostered, and the vans start slipping",
        };
      }

      /* The demo button and the microphone take the same route here for the
         same reason they do on the server: if they could diverge, the button
         would stop being evidence of anything. */
      if (path === "/api/demo/disrupt") {
        return this.handle("/api/console/command", {
          body: JSON.stringify({ transcript: data.vocabulary.examples[1] }),
        });
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

  const rolePicker = document.getElementById("role-picker");
  if (rolePicker) {
    rolePicker.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-role]");
      if (!button) return;
      const employee = button.dataset.role === "employee";
      rolePicker.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", b === button);
        b.setAttribute("aria-checked", String(b === button));
      });
      document.getElementById("view-manager").hidden = employee;
      document.getElementById("view-employee").hidden = !employee;
      window.scrollTo({ top: 0 });
      // Land on whoever this step is about, rather than whichever name sorts
      // first -- otherwise switching to the employee view at the moment a
      // cover request goes out shows somebody with nothing to do.
      if (employee && window.__employeeView) {
        window.__employeeView.setPerson(window.__REPLAY__.principal);
      } else if (window.__dashboardRefresh) {
        window.__dashboardRefresh();
      }
    });
  }

  const stalledButton = document.getElementById("btn-stalled");
  if (stalledButton) {
    stalledButton.addEventListener("click", () => {
      const stopped = window.__REPLAY__.toggleStalled();
      stalledButton.textContent = stopped ? "Back to a live agent" : "Show a stopped agent";
      if (window.__dashboardRefresh) window.__dashboardRefresh();
    });
  }
})();
