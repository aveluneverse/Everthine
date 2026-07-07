"""The self-portrait album: one command that renders every dated snapshot in
data/portrait_history/ into a single, self-contained, offline timeline HTML
the user can double-click open -- no server, no network, no build step. It is
the framework's flagship word-of-mouth page: the one a user screenshots to
show how their companion has grown, so its whole job is to read the snapshots
back out and typeset them calmly and well.

This module is a deliberate island. It imports neither config nor bot nor
even portrait.py: it reads the JSON snapshots itself and takes exactly one
knob, --data-dir (default "data"), so a future install wizard can invoke it
with nothing but a path and no BOT_TOKEN in sight. The companion does not
know the viewer exists -- there is no bot-side wiring in either direction.

Fail-soft throughout, mirroring the rest of the codebase: a broken JSON file,
a non-object payload, or a snapshot missing its `content` is logged and
skipped rather than allowed to crash the render; a missing `updated` falls
back to the filename's date stem; missing `opinions`/`observations` default
to empty. An empty history -- the directory absent, empty, or every file
skipped -- still produces a page, just a gentle empty state. Every dynamic
string (content, dates, opinions, observations) is html-escaped, and the
emitted page carries no external reference of any kind: inline CSS only, a
system font stack with CJK fallback, zero CDN, zero JS. Snapshots are ordered
by filename (which is their date), oldest first, so the page reads front to
back as a growth history and arrives, at the bottom, on who the companion is
now.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("everthine")

# --- Static English chrome (neutral: the companion's gender is the persona's
# business, not the viewer's, so the copy never assumes one) -------------
PAGE_TITLE = "Portrait Timeline"
PAGE_SUBTITLE = "Self-portraits over time — who they've noticed themselves becoming."
EMPTY_LEAD = "No self-portrait yet — they haven't written their first one."
EMPTY_HINT = "Come back in a little while, and their timeline will begin here."

# Card and section labels are the open-source canonical (English) strings the
# brief pins verbatim; do not localize them here -- the persona's own prose
# inside `content` carries whatever language it was written in.
SECTION_POSITIONS = "Positions"
SECTION_NOTES = "Notes to self"

# Warm-paper theme, written fresh for this page: cream ground, ink text, a
# single quiet ochre accent for the timeline nodes and version eyebrows.
# Hierarchy is carried by size / weight / spacing, not by color. No dark
# navy, no wine red, no serif display face, no external resource anywhere.
CSS = """\
:root {
  --paper: #f4efe4;
  --card: #fbf8f1;
  --ink: #33302a;
  --ink-soft: #736b5d;
  --rule: #e4dccd;
  --accent: #b07d4b;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC",
    "Microsoft JhengHei", "PingFang TC", "Hiragino Sans", sans-serif;
  font-size: 17px;
  line-height: 1.75;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
.page {
  max-width: 680px;
  margin: 0 auto;
  padding: 72px 28px 96px;
}
.masthead {
  margin-bottom: 56px;
}
.masthead h1 {
  margin: 0;
  font-size: 1.55rem;
  font-weight: 600;
  letter-spacing: 0.01em;
}
.masthead .subtitle {
  margin: 12px 0 0;
  font-size: 0.98rem;
  font-weight: 400;
  color: var(--ink-soft);
}
.timeline {
  position: relative;
  margin: 0;
  padding: 0;
}
.timeline::before {
  content: "";
  position: absolute;
  left: 6px;
  top: 12px;
  bottom: 12px;
  width: 2px;
  background: var(--rule);
}
.entry {
  position: relative;
  padding-left: 40px;
  margin: 0 0 40px;
}
.entry:last-child { margin-bottom: 0; }
.entry::before {
  content: "";
  position: absolute;
  left: 0;
  top: 7px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--card);
  border: 3px solid var(--accent);
  box-shadow: 0 0 0 4px var(--paper);
}
.card {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 26px 30px 28px;
  box-shadow: 0 1px 2px rgba(60, 50, 30, 0.05);
}
.version {
  margin: 0 0 16px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--accent);
}
.content { margin: 0; }
.content p { margin: 0 0 1.05em; }
.content p:last-child { margin-bottom: 0; }
.block {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--rule);
}
.block-title {
  margin: 0 0 12px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-soft);
}
.block ul { margin: 0; padding: 0; list-style: none; }
.block li { margin: 0 0 9px; }
.block li:last-child { margin-bottom: 0; }
.topic { font-weight: 600; }
.empty {
  text-align: center;
  padding: 64px 0 32px;
}
.empty-lead {
  margin: 0 0 14px;
  font-size: 1.1rem;
}
.empty-hint {
  margin: 0;
  font-size: 0.98rem;
  color: var(--ink-soft);
}
@media (max-width: 520px) {
  .page { padding: 48px 18px 64px; }
  .card { padding: 22px 20px 24px; }
  .entry { padding-left: 34px; }
}"""


# ---------------------------------------------------------------------
# Snapshot loading: filename-ordered, fail-soft on every file
# ---------------------------------------------------------------------

def _load_entries(history_dir: Path) -> list[dict]:
    """Read every {history_dir}/*.json snapshot, oldest first (filename IS the
    date, so a lexical filename sort is the chronological order). Each file is
    parsed defensively: an unreadable or invalid-JSON file, a payload that
    isn't an object, or a snapshot with no usable `content` is logged and
    skipped rather than allowed to crash the render. A missing/blank `updated`
    falls back to the filename stem; missing/wrong-typed `opinions` or
    `observations` default to an empty list. Returns the kept snapshots in
    render order."""
    entries: list[dict] = []
    if not history_dir.is_dir():
        return entries
    for path in sorted(history_dir.glob("*.json"), key=lambda p: p.name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("portrait_viewer: skipping unreadable file %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("portrait_viewer: skipping non-object file %s", path)
            continue
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("portrait_viewer: skipping %s (no content)", path)
            continue
        updated = data.get("updated")
        if not isinstance(updated, str) or not updated.strip():
            updated = path.stem
        opinions = data.get("opinions")
        if not isinstance(opinions, list):
            opinions = []
        observations = data.get("observations")
        if not isinstance(observations, list):
            observations = []
        entries.append({
            "updated": updated,
            "content": content,
            "opinions": opinions,
            "observations": observations,
        })
    return entries


# ---------------------------------------------------------------------
# Rendering: every dynamic string passes through html.escape
# ---------------------------------------------------------------------

def _render_content(content: str) -> str:
    """Escape the prose, then keep its shape: a blank line starts a new
    paragraph, a lone newline becomes a <br>. Escaping happens first so the
    only tags in the result are the ones this function adds."""
    escaped = html.escape(content)
    parts = []
    for para in re.split(r"\n[ \t]*\n", escaped):
        para = para.strip("\n")
        if not para.strip():
            continue
        parts.append("<p>" + para.replace("\n", "<br>") + "</p>")
    return "\n".join(parts)


def _render_positions(opinions: list) -> str:
    """Render the `Positions` block, or "" when nothing well-shaped survives.
    Each opinion is expected to be {"topic": str, "opinion": str}; anything
    that isn't a dict, or carries neither a topic nor an opinion string, is
    dropped."""
    items = []
    for op in opinions:
        if not isinstance(op, dict):
            continue
        topic = op.get("topic")
        opinion = op.get("opinion")
        topic = html.escape(topic) if isinstance(topic, str) else ""
        opinion = html.escape(opinion) if isinstance(opinion, str) else ""
        if not topic and not opinion:
            continue
        if topic and opinion:
            items.append(
                f'<li><span class="topic">{topic}</span> — {opinion}</li>')
        elif topic:
            items.append(f'<li><span class="topic">{topic}</span></li>')
        else:
            items.append(f"<li>{opinion}</li>")
    return _wrap_block(SECTION_POSITIONS, items)


def _render_notes(observations: list) -> str:
    """Render the `Notes to self` block, or "" when nothing survives. Each
    observation is expected to be a string; anything else is dropped."""
    items = []
    for obs in observations:
        if not isinstance(obs, str):
            continue
        text = html.escape(obs)
        if not text.strip():
            continue
        items.append(f"<li>{text}</li>")
    return _wrap_block(SECTION_NOTES, items)


def _wrap_block(title: str, items: list) -> str:
    if not items:
        return ""
    return (
        '<section class="block">\n'
        f'<h3 class="block-title">{title}</h3>\n'
        "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        "</section>"
    )


def _render_entry(n: int, entry: dict) -> str:
    label = f"Version {n} · {html.escape(entry['updated'])}"
    inner = [
        f'<h2 class="version">{label}</h2>',
        f'<div class="content">{_render_content(entry["content"])}</div>',
    ]
    for block in (_render_positions(entry["opinions"]), _render_notes(entry["observations"])):
        if block:
            inner.append(block)
    body = "\n".join(inner)
    return f'<article class="entry">\n<div class="card">\n{body}\n</div>\n</article>'


def _wrap_page(body_html: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{PAGE_TITLE}</title>\n"
        f"<style>\n{CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="page">\n'
        '<header class="masthead">\n'
        f"<h1>{PAGE_TITLE}</h1>\n"
        f'<p class="subtitle">{PAGE_SUBTITLE}</p>\n'
        "</header>\n"
        f"{body_html}\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def _render_empty_page() -> str:
    body = (
        '<div class="empty">\n'
        f'<p class="empty-lead">{EMPTY_LEAD}</p>\n'
        f'<p class="empty-hint">{EMPTY_HINT}</p>\n'
        "</div>"
    )
    return _wrap_page(body)


def _render_page(entries: list) -> str:
    if not entries:
        return _render_empty_page()
    cards = "\n".join(_render_entry(n, e) for n, e in enumerate(entries, start=1))
    return _wrap_page(f'<main class="timeline">\n{cards}\n</main>')


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m everthine.portrait_viewer",
        description="Render the self-portrait history into one offline timeline HTML.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help='Directory holding portrait_history/ (default: "data").',
    )
    return parser


def main(argv=None) -> int:
    # Windows consoles often default to a legacy codepage (e.g. cp950); the
    # path we print can carry non-ASCII, so force UTF-8 with replacement
    # rather than crash. Guard hasattr for streams that lack reconfigure
    # (test capture, pipes) -- the same opening memory_recall's probe uses.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = _build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    history_dir = data_dir / "portrait_history"

    page = _render_page(_load_entries(history_dir))

    out_path = data_dir / "portrait_timeline.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
