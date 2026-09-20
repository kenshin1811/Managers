# Managers

An agent that runs a donut factory's packing floor: it decides who does what,
inside guardrails, and writes down why.

The floor is four packers — Ken, Jasoo, Shaleen and Valentino — working an
afternoon shift, seven days a week. Frozen stock comes out of the general
freezer; fresh lines do not. Everything is packed, labelled, sorted per order
and loaded onto one of five vans running to shops around Melbourne, and
nothing leaves the building after 9pm.

The agent reads the order book, works out what is still outstanding, and lays
the evening out stage by stage — **pull from the freezer → pack and label →
sort to orders → load the van** — assigning each job to somebody signed off
for it and free when it has to happen. When a late order lands, somebody goes
home or a van starts slipping, it re-plans. When the new plan still does not
fit, it asks the people who are off today, and if that fails it tells a
manager rather than quietly missing a van.

The thing that actually bites is not total hours. There are plenty of those.
It is the individual departures: everything for the 18:00 north run has to be
out of the freezer, packed, labelled, sorted and loaded by 18:00, and no
amount of spare capacity at 20:00 helps with that.

A floor manager watches it and talks to it — by voice or by typing — in the
same sentences they would use across the room:

> *"add 240 jam donuts for Fitzroy"* · *"Shaleen is off sick"* ·
> *"Valentino is back"* · *"mark 120 chocolate rings packed"* ·
> *"what's at risk"*

## Try it

**Click through it, install nothing** —
[a walkthrough of both screens](https://claude.ai/artifact/Ln7bgLhkcjNAEuzVs21kNm).
Type into the console and watch the vans re-plan; switch between the manager's
screen and an employee's to see the same moment from both sides. Every van
time, fit score and rejection reason in it came out of the real engine —
`python -m app.sim.capture` runs the thing and records what it decided, and the
page replays those steps. It is a recording, not a live server, so it only
follows the paths that were recorded, and it says so when you leave them.

**Run the real one** — one command, from nothing:

```bash
git clone https://github.com/kenshin1811/Managers.git && cd Managers
./demo.sh              # or: ./demo.sh short_team
```

That builds the virtualenv, starts the service, seeds the factory timed to the
current moment, lets the agent plan the evening, and opens the dashboard.
`Ctrl+C` stops it and cleans up. Nothing is sent anywhere: `DRY_RUN` is on by
default, so messages are logged instead.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# The dashboard. Open http://localhost:8000 and press "Load the factory".
.venv/bin/uvicorn app.main:app --reload

# Or watch it decide in the terminal, with no server at all.
.venv/bin/python -m app.sim.scenario
```

`make help` lists the shortcuts: `make demo`, `test`, `lint`, `sim`, `replay`.

Out of the box `DRY_RUN=true` and no Slack token is set, so every message is
logged instead of delivered. Copy `.env.example` to `.env` to change anything.

## Two screens

**The manager's dashboard** (`/`) answers one question before anything else:
**is the agent actually working?** Then: which vans make it, what is left to
pack, and — in *Tell the floor* — a microphone and a text box that take the
same sentence to the same parser.

**The employee's page** (`/me`) is the other half, and the one that drives the
agent. It shows one person their own work and nothing else: cover requests
addressed to them at the top with a countdown to escalation, their jobs with
driver times, their hours against the limits the engine enforces, clock in and
out, and a form to request time off. Each job shows its stage, its van, and what
it is waiting on — nobody packs from an empty bench. It is built for a phone
held in one hand, because that is where it will be read.

What it deliberately does not show: a colleague's hours, the decision log, or
what the engine scored the other people it asked. An employee is told *whose*
job they are covering, because they may have to hand it back — but nothing
about that person beyond the fact they are away. `tests/test_me.py` is mostly
about keeping that line where it is.

Identity has no login behind it yet. In dry run the page offers a person
picker so you can look around; anywhere else it requires the signed link the
cover request carried, and the picker is refused outright. A signed link
proves we sent it to that person — it is not authentication, and anyone
holding the link can use it.

## The dashboard

`http://localhost:8000` answers one question before anything else: **is the
agent actually working?** The heartbeat is read from the database rather than
from the process serving the page, because the scheduler may not be that
process — so "active" means the sweep really ran, not that the web server is
up. Miss three sweeps and it says *stalled*, which is the honest word for it.

Below that: units still to pack and hours of work left on the bench, then
**Vans tonight** — one card per run with a live countdown to its departure,
its stops, what is still outstanding, who is working on it, and whether it is
on time, cutting it fine or going to be late.

Then **Tell the floor**. Press *Speak* and say it, or type it; either way the
words go to `/api/console/command`, are parsed by
[`app/engine/commands.py`](app/engine/commands.py), and the agent answers in
one sentence — usually whether the vans still make it. Speech recognition runs
in the browser (Web Speech API); where it is unsupported the microphone hides
itself and the text box stays. The grammar is deliberately narrow and
deterministic, with no model in the loop: a console that guesses is worse than
one that says it did not understand, because a wrong guess moves real donuts
to the wrong shop. Every command is written to the same audit log as the
agent's own decisions, tagged with the words used and where they came from.

Below that: escalations, cover requests in flight with a live countdown, the
job board, what is left to pack by product, the decision feed with the reasons
people were ruled out, and every person's hours against the limits.

In dry run a demo bar appears. **Load the factory** wipes the database, seeds
the order book timed to the current moment and lets the agent plan it — the
next van really is ninety minutes out. **Shaleen goes home** then sets the
engine off, and it goes through the same command parser the microphone does,
so the button stays evidence of the real path rather than a shortcut around
it. The demo refuses to run unless `DRY_RUN` is on: it deletes data, and dry
run is the only configuration where that is a sandbox rather than a roster.

The page is plain HTML, CSS and JavaScript with no build step — a page whose
job is to prove the backend runs is a poor place for a toolchain of its own.
It follows the system font stack and colours, and takes light or dark from the
OS.

## What the simulation shows

`python -m app.sim.scenario` runs four cases against the seeded factory and
prints the full trace for each: what the evening needs, every candidate with
their score, every rule evaluated with the reason it passed or failed, the
action taken, and the messages that would have gone out.

1. **The agent plans the evening.** 53 jobs across four stages and five vans,
   from an order book and nothing else. Every van makes it.
2. **The floor manager says something.** Five sentences through the parser,
   including one it refuses to guess at.
3. **A packer goes home.** The evening is rebuilt first; only what the new
   plan cannot absorb is escalated, once, naming who could come in.
4. **Somebody steps out and the system rings round.** Cover requests go to the
   people who are off today, in order of fit — then the same evening again
   with everybody saying no, which escalates rather than pushing the last
   available packer past 48 hours.

## How the evening gets planned

`app/engine/planning.py` is the part that manages rather than reacts. It runs
on a demo reset, on every console command that changes the work, and whenever
the roster changes.

1. **Explode the order book** into work units, one stage at a time. Only what
   is still outstanding, and batched the way a line actually works: two shops
   on the north run both wanting jam donuts is *one* trip to the freezer, not
   two. Fresh lines skip retrieval entirely.
2. **Give each unit a deadline** worked back from its van, not from the 9pm
   cutoff — the freezer trip for the 18:00 run has to leave time for packing,
   sorting and loading behind it.
3. **Sort by deadline, then by stage**, so the tightest van is placed first
   and the pipeline stays in order within it.
4. **Place each unit** with the same candidate ranking the leave flow uses:
   guardrails decide who *may* do it, a weighted score orders the ones who
   passed, and the winner is whoever finishes it earliest. Dependencies are
   real — a pack job cannot start before its freezer trip ends.
5. **Say what does not fit.** A run whose work finishes after it leaves is
   `missed`, with the minutes it is short; one that finishes inside the
   at-risk margin is `at_risk`; a run with work nobody can take is `missed`
   too, because there is simply no plan for it.

Re-planning replaces only the planner's own unstarted work. Anything a person
added by hand, and anything already under way, survives — the agent may change
its mind about the future, not rewrite what the floor has already done.

## How a cover decision gets made

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
| `GET /api/dashboard` | Everything the manager's dashboard shows, in one payload |
| `GET /api/me/{id}` | One employee's own view — their work, their hours, nothing else |
| `GET /api/console/vocabulary`, `POST /api/console/command` | What the console understands, and one instruction |
| `POST /api/demo/reset`, `POST /api/demo/disrupt` | Load and disrupt the demo (dry run only) |

A background sweep (`app/scheduler.py`, every 60s) widens the search when a
request times out and escalates when the pickup gets close, because silence
looks exactly like everything being fine.

## Testing

```bash
.venv/bin/python -m pytest        # 216 tests
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
- `tests/test_me.py` guards the line between the two screens, including that a
  signed link opens its owner's page and refuses anybody else's.
- `tests/test_planning.py` is mostly about order rather than arithmetic:
  nothing is packed before it is pulled, nobody is in two places at once, work
  only goes to people signed off for it, and a van with nothing placed against
  it reads as missed rather than ready.
- `tests/test_commands.py` pins the grammar, including that an unparsed
  sentence changes nothing at all.

## Layout

```
app/engine/         decision logic — guardrails, ranking, orchestration, escalation
app/models/         SQLAlchemy models, including the audit trail
app/api/            FastAPI routers
app/notifications/  Notifier protocol, Slack and console implementations
app/engine/planning.py  the day planner: order book → stages → who and when
app/engine/commands.py  the console grammar: a sentence → an intent → an action
app/sim/            seeded factory, the printed scenarios and the recorder
app/web/            both screens: index.html, me.html, styles.css,
                    common.js (shared), app.js, me.js, console.js
tools/              build the published walkthrough from the live dashboard
```

The published walkthrough is not a second copy of the UI. `app/sim/capture.py`
records the real engine's output, `app/web/replay.js` serves those recordings
where `fetch` would have gone (one branch, in `api()`), and
`tools/build_replay.py` inlines the dashboard's own HTML, CSS and JavaScript
around them. Change the dashboard, run `make replay`, and the walkthrough
follows — there is no duplicate to drift.

## Not in this version

No payroll, no authentication on the dashboard, no multi-site or cross-timezone
rostering, and SQLite rather than a production database. Times are stored as naive UTC and
rendered in a single configured business timezone.
