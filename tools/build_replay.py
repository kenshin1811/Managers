"""Assemble the published walkthrough from the real dashboard.

    python -m tools.build_replay

Takes the live dashboard's own HTML, stylesheet and script, adds the recorded
snapshots and the replay shim, and writes one self-contained file. Nothing is
re-implemented: run this again after any change to the dashboard and the
walkthrough follows it, which is the only way a demo stays honest about what
the product looks like.

The artifact host wraps the file in its own document skeleton, so the page
body is emitted without doctype, html, head or body tags of its own.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
OUT = Path(__file__).resolve().parents[1] / "dist" / "replay.html"

TITLE = "Managers Shift Agent"

REPO = "https://github.com/kenshin1811/Managers"

BANNER = """
<div class="replay-banner">
  <div class="wrap replay-banner-inner">
    <div>
      <strong>Recorded walkthrough.</strong>
      Every figure, name, fit score and rejection reason below came out of the
      real Python engine — this page replays what it decided, step by step.
      Switch between the manager's screen and an employee's to see the same
      moment from both sides. It is not talking to a live server, so it can
      only follow paths that were recorded.
    </div>
    <div class="replay-banner-actions">
      <div class="segmented" id="role-picker" role="radiogroup" aria-label="Whose screen">
        <button role="radio" aria-checked="true" data-role="manager" class="on">Manager</button>
        <button role="radio" aria-checked="false" data-role="employee">Employee</button>
      </div>
      <button class="btn btn-quiet btn-small" id="btn-stalled">Show a stopped agent</button>
      <a class="btn btn-quiet btn-small" href="__REPO__">Run the real thing</a>
    </div>
  </div>
</div>
""".replace("__REPO__", REPO)

BANNER_CSS = """
/* Walkthrough-only chrome. Kept out of the live dashboard's stylesheet, since
   the live dashboard has nothing to apologise for. */
.replay-banner {
  background: var(--surface-2);
  border-bottom: 1px solid var(--separator);
  font-size: 14px;
  color: var(--text-2);
}
.replay-banner-inner {
  display: flex;
  gap: 20px;
  align-items: center;
  justify-content: space-between;
  padding-block: 14px;
  flex-wrap: wrap;
}
.replay-banner strong { color: var(--text); font-weight: 600; }
.replay-banner-inner > div:first-child { max-width: 68ch; line-height: 1.5; }
/* Must be allowed to shrink, or its three controls push the page wider than
   a phone and the whole document scrolls sideways. */
.replay-banner-actions {
  display: flex; gap: 8px; flex: 1 1 auto; min-width: 0; flex-wrap: wrap;
}
@media (min-width: 900px) { .replay-banner-actions { justify-content: flex-end; } }
.replay-banner a.btn { text-decoration: none; display: inline-block; }
/* The quiet button fill is the banner's own background, which left these two
   reading as plain text. On this surface they need to lift off it. */
.replay-banner .btn-quiet { background: var(--surface); }
.replay-banner .btn-quiet:hover { background: var(--surface-3); }
.replay-banner .segmented { background: var(--surface-3); }

/* The two screens live in one document; only one is shown at a time. */
.view[hidden] { display: none; }
"""


def read(name: str) -> str:
    return (WEB / name).read_text()


def body_of(name: str, drop_scripts: tuple[str, ...]) -> str:
    """The page's own markup, without the document shell or its script tags."""
    html = read(name)
    body = html[html.index("<body>") + len("<body>") : html.index("</body>")]
    for tag in drop_scripts:
        body = body.replace(f'<script src="/static/{tag}"></script>', "")
    return body.strip()


def build() -> str:
    styles = read("styles.css")
    common_js = read("common.js")
    replay_js = read("replay.js")
    app_js = read("app.js")
    me_js = read("me.js")
    snapshots = json.loads(read("snapshots.json"))

    manager = body_of("index.html", ("common.js", "app.js"))
    employee = body_of("me.html", ("common.js", "me.js"))

    # One toast element between them: two ids would be invalid, and only one
    # can be on screen anyway.
    toast = '<div class="toast" id="toast" hidden></div>'
    manager = manager.replace(toast, "")
    employee = employee.replace(toast, "")

    body = "\n".join(
        [
            BANNER,
            '<div class="view" id="view-manager">',
            manager,
            "</div>",
            '<div class="view" id="view-employee" hidden>',
            employee,
            "</div>",
            toast,
        ]
    )

    # The recorded payloads are data, not code: a script tag the browser will
    # not execute, parsed by replay.js.
    data_block = (
        '<script id="replay-data" type="application/json">'
        + json.dumps(snapshots, separators=(",", ":")).replace("</", "<\\/")
        + "</script>"
    )

    return "\n".join(
        [
            f"<title>{TITLE}</title>",
            "<style>",
            styles.strip(),
            BANNER_CSS.strip(),
            "</style>",
            body.strip(),
            data_block,
            "<script>",
            common_js.strip(),
            "</script>",
            "<script>",
            replay_js.strip(),
            "</script>",
            # Both pages were written as standalone scripts with top-level
            # names of their own (REFRESH_MS, refresh, tick, busy). In one
            # document they would collide, so each keeps its own scope.
            "<script>(function () {",
            app_js.strip(),
            "})();</script>",
            "<script>(function () {",
            me_js.strip(),
            "})();</script>",
            "",
        ]
    )


def main() -> None:
    page = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)

    leftovers = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    if leftovers:
        raise SystemExit(f"page still points at the server for: {leftovers}")

    print(f"{OUT}  ({len(page.encode()) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
