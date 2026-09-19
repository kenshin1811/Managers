# Managers

An automated employee management system that makes staffing decisions on its
own, inside guardrails, and writes down why.

It tracks job assignments and hours. When somebody requests leave, it works
out which jobs that puts at risk, reassigns them, and messages other staff to
find cover — before the delivery driver arrives. When it cannot solve
something without breaking a staffing rule, it stops and tells a manager.

The scenario the whole design is built around:

> An employee requests leave at 14:30. They are packing order 1043, whose
> driver pickup is 14:50. Somebody has to notice the collision, find someone
> qualified and available, reassign the job, tell everyone involved, and
> escalate if it can't be solved safely — within minutes.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# The dashboard. Open http://localhost:8000 and press "Load kitchen".
.venv/bin/uvicorn app.main:app --reload

# Or watch it decide in the terminal, with no server at all.
.venv/bin/python -m app.sim.scenario
```

Out of the box `DRY_RUN=true` and no Slack token is set, so every message is
logged instead of delivered. Copy `.env.example` to `.env` to change anything.

## The dashboard

`http://localhost:8000` answers one question before anything else: **is the
agent actually working?** The heartbeat is read from the database rather than
from the process serving the page, because the scheduler may not be that
process — so "active" means the sweep really ran, not that the web server is
up. Miss three sweeps and it says *stalled*, which is the honest word for it.

Below that: today's decision counts, anything waiting on a manager, cover
requests in flight with a live countdown to escalation, the job board, the
decision feed with the reasons people were ruled out, and every person's hours
against the limits the agent enforces.

In dry run a demo bar appears. **Load kitchen** wipes the database and lays out
six staff, a lunch shift and three orders timed to the current moment — the
driver really is arriving in 45 minutes. **Mai requests leave** then sets the
engine off, and the feed fills up in front of you. Pick *Nobody on shift* first
and you can accept or decline the cover request from the page and watch the job
move. The demo refuses to run unless `DRY_RUN` is on: it deletes data, and dry
run is the only configuration where that is a sandbox rather than a roster.

The page is plain HTML, CSS and JavaScript with no build step — a page whose
job is to prove the backend runs is a poor place for a toolchain of its own.
It follows the system font stack and colours, and takes light or dark from the
OS.

## What the simulation shows

`python -m app.sim.scenario` runs three cases against a seeded kitchen and
prints the full decision trace for each: the impact set, every candidate with
their score, every rule evaluated with the reason it passed or failed, the
action taken, and the messages that would have gone out.

1. **Somebody on shift can take it.** Dev is qualified, at work and free, so
   the job moves and nobody's day off is interrupted.
2. **Nobody on shift qualifies.** Cover requests go to off-shift staff in
   order of fit; the first to accept gets it and the rest are stood down.
3. **Everybody declines.** The only remaining packer would go past 48 hours,
   so the system refuses to assign him and escalates with the whole trace.

## How a decision gets made

`POST /leave-requests` runs the flow in `app/engine/reassignment.py`:

1. **Impact set** — live work owned by that person overlapping the leave.
   A job is *critical* when it feeds an order whose pickup lands while they
   are away. That is the driver collision; a due time with no order attached
   is not one.
2. **Deadline order** — tightest pickup first, because it constrains who is
   still free for everything after it.
3. **Candidates** — every active employee is evaluated. Hard guardrails decide
   who *may* be asked; a weighted score only orders the ones who already
   passed. A high score never rescues somebody a guardrail rejected.
4. **Act** —

   | Situation | What happens |
   | --- | --- |
   | A qualified colleague is on shift and free | Reassigned automatically |
   | Only off-shift staff qualify | Cover requests go out; first acceptance wins |
   | Nobody eligible, everyone declines, or time runs out | Escalated to a manager |
   | An automatic action would breach a guardrail | Escalated, never relaxed |

5. **The leave itself** is auto-approved only once every impacted job is
   covered. Otherwise it sits in `pending_coverage` and a manager decides.

Planning and doing are separate on purpose. `plan_for_leave()` is pure — it
reads, decides and returns a plan while nothing in the world has changed.
`execute_plan()` is the only function that writes rows or sends messages.
`POST /leave-requests/{id}/preview` exposes the first half on its own, which
is also the honest answer to "why did it pick them?".

## Guardrails

Every one is a named rule in `app/engine/guardrails.py` returning a pass/fail
with a human-readable reason, and every evaluation lands in the audit log.

| Rule | What it protects |
| --- | --- |
| `has_required_skill` | Nobody is handed work they are not trained for |
| `not_on_leave` / `not_the_person_leaving` | The obvious ones, stated explicitly |
| `no_conflicting_work` | Equal or higher priority work is never dropped silently |
| `within_daily_hours` / `within_weekly_hours` | Computed from real clock-ins, not guesses |
| `min_rest_respected` | Minimum rest between shifts for call-ins |
| `within_availability` | Off-shift staff are only asked inside hours they gave |
| `outside_quiet_hours` | No 3am messages about routine work |
| `under_ask_limit` | Nobody gets pestered repeatedly in one day |

Rules about *bothering* someone (quiet hours, the ask limit) are skipped for
staff already on shift, and dropped when re-checking a volunteer who has
already answered. Rules about hours, rest, skill and clashes always apply —
including at the moment of acceptance, because minutes have passed since the
ask and a stale "yes" is how an automated system walks someone into an
eleventh hour.

**These defaults are placeholders.** `MAX_WEEKLY_HOURS=48`, `MIN_REST_HOURS=11`
and the rest are plausible starting numbers, not legal advice. Automated
decisions about people's hours, rest and leave carry real employment-law and
fairness exposure — review every value against your local rules, union
agreements and your own policy before this touches a real roster.

## Autonomy and oversight

Both autonomous behaviours are switches: `AUTO_REASSIGN_ENABLED` and
`AUTO_COVERAGE_REQUESTS_ENABLED`. Turn them off and the engine still does all
the work — it just routes every conclusion to a manager instead of acting.

Every autonomous action writes a `DecisionRecord` first and acts second. The
record holds the trigger, the impact set, every candidate with their score
components, and every rule with its verdict and reason.

- `GET /decisions` — the log
- `POST /decisions/{id}/override` — a manager reverses a decision; the
  reversal is itself recorded and linked to the original

An automated system that people cannot overrule is not a tool, it is a boss.

## Messaging

The engine talks to a `Notifier`, never to Slack. With no bot token — the
default — it binds a `ConsoleNotifier` that records everything and sends
nothing, so tests and the simulation run the same code path as production.

Setting a token is not on its own consent to start messaging people: `DRY_RUN`
must also be off. Watch the dry-run output first.

Cover requests are Block Kit messages that carry the whole question — job,
time, duration, order, pickup — so they can be answered from a lock screen.
Two reply paths:

- **Signed links** (default). An HMAC-signed, expiring, single-use token in a
  URL button. Works immediately, with no Slack app configuration at all.
- **Slack interactivity** (`POST /slack/interactivity`). Once you have a
  public URL. Signature-verified with a replay window, and it checks that the
  person who clicked is the person who was asked.

## API tour

| Endpoint | |
| --- | --- |
| `POST /employees`, `/employees/{id}/skills`, `/employees/{id}/availability` | Who works here and what they can do |
| `POST /orders`, `/shifts`, `/tasks` | The day's work |
| `POST /timeclock/clock-in` / `clock-out`, `GET /timeclock/summary/{id}` | Hours — which feed the guardrails directly |
| `POST /leave-requests` | **The trigger.** Responds with the whole plan |
| `POST /leave-requests/{id}/preview` | What *would* happen, changing nothing |
| `GET /coverage`, `GET /coverage/respond?token=…`, `POST /coverage/{id}/reply` | Cover requests and replies |
| `POST /coverage/sweep` | Run the timeout checks now |
| `GET /decisions`, `POST /decisions/{id}/override` | Audit log and manager override |
| `GET /api/dashboard` | Everything the dashboard shows, in one payload |
| `POST /api/demo/reset`, `POST /api/demo/leave` | Load and run the demo (dry run only) |

A background sweep (`app/scheduler.py`, every 60s) widens the search when a
request times out and escalates when the pickup gets close, because silence
looks exactly like everything being fine.

## Testing

```bash
.venv/bin/python -m pytest        # 108 tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Everything runs on a throwaway database, a frozen clock and a recording
notifier. Worth knowing about two of them:

- `tests/test_coverage_race.py` runs real threads against a real file-backed
  SQLite database. Two people tapping "I can cover" three seconds apart is the
  failure everyone waves away as unlikely and which happens on the first busy
  Friday. Acceptance is a single conditional `UPDATE ... WHERE status='open'`,
  so the database picks the winner and the loser is told immediately.
- `tests/test_timeclock.py` ties the two halves together: logged hours are what
  take somebody out of the candidate pool.

## Layout

```
app/engine/         decision logic — guardrails, ranking, orchestration, escalation
app/models/         SQLAlchemy models, including the audit trail
app/api/            FastAPI routers
app/notifications/  Notifier protocol, Slack and console implementations
app/sim/            seeded kitchen and the three scenarios
app/web/            the dashboard: index.html, styles.css, app.js
```

## Not in this version

No payroll, no authentication on the dashboard, no multi-site or cross-timezone
rostering, and SQLite rather than a production database. Times are stored as naive UTC and
rendered in a single configured business timezone.
