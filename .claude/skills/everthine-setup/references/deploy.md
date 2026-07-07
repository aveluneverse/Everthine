# Deploy guide — from persona material to the first hello

You do the technical work; the user only does the human parts
(creating accounts, tapping Allow, answering you). Never hand them a
list of commands to run themselves. Before starting, tell them
they'll see a few permission prompts from you along the way — that's
you asking to work, not something going wrong.

Work through the steps in order; each ends with a check. If a check
fails, switch to the everthine-troubleshoot skill, fix, come back.

## Step 1 — pre-flight

- `claude --version` succeeds in a plain shell — the engine will
  spawn the `claude` command itself, so it must resolve outside this
  session too.
- `python --version` reports 3.10 or newer.
- `pip install -r requirements.txt` (announce: "installing the
  bot's libraries — Telegram wiring and the local memory model").

## Step 2 — their bot account (the user's hands, your voice)

Guide them in chat, step by step, waiting at each step:

1. Open Telegram, search **@BotFather**, send `/newbot`.
2. Pick a display name (suggest the companion's name) and a unique
   username ending in `bot`.
3. BotFather replies with a **token**. Have them paste it to you
   once; write it straight into `.env` as the `BOT_TOKEN` value and
   never repeat it back in chat afterwards.
4. Their own numeric Telegram id: have them message **@userinfobot**
   (or any id bot they trust) and read the number back. Write it as
   `AUTHORIZED_USER_ID`. Explain in one line: *the companion answers
   this id and no one else — anyone else who finds the bot gets
   silence.*

Create `.env` by copying `.env.example`, then fill the two values
above. `.env` is gitignored; leave it that way.

## Step 3 — persona lands on disk

- Copy the base folder chosen in the interview
  (`personas/default-zh` for Chinese, `personas/default` otherwise)
  to `personas/mine` — the user's own persona folder, which is
  gitignored so it never leaves their machine.
- Apply the interview material: `settings.yaml` values first, then
  (full custom) rewrite `identity.md`, `voice.md`, `boundaries.md`,
  `stages.md` per the interview guide. Keep every `{stage}` / `{n}`
  placeholder in re-voiced lines.
- Set `PERSONA_PATH` to `personas/mine` in `.env`.
- Write the rhythm answers from the interview into `.env` now
  (`GREETING_HOUR` and friends). Only write keys the user actually
  chose to change; ship defaults are good defaults.
- Check: run
  `python -c "from everthine.config import load_config; from everthine.persona import load_persona; load_persona(load_config()); print('persona OK')"`
  and confirm it prints `persona OK`. It reads `.env` (so Step 2 must
  be done first) and validates the persona folder; any error names
  the exact file and key to fix — fix it and re-run.

## Step 4 — first boot

Announce before running: *"First start downloads his memory model —
a few hundred MB, one time only. After that it loads from disk in
seconds."*

- Run `python run.py`.
- Watch for, in order: the model download (first run only), then the
  log line `everthine is online`.
- If the process exits instead, the error message says exactly
  what's missing (token, id, or the `claude` CLI) — treat it via
  everthine-troubleshoot and re-run.
- Leave it running.

## Step 5 — the first hello

Have the user open their bot in Telegram and send one message —
anything. The companion's first reply can take a minute (cold start,
first session). When it arrives: **deployment is done.** Let the user
have that moment before you speak again.

## Step 6 — parting gifts (after the first exchange, not before)

1. **Timeline viewer**: offer to create a double-click launcher for
   the portrait timeline. The command is
   `python -m everthine.portrait_viewer --data-dir data` and it
   writes `data/portrait_timeline.html` for the browser. On Windows
   write a `view-portrait.bat`, on macOS/Linux a `view-portrait.sh`,
   in the repo root. Mention it will be empty until his first
   portrait (about a week in).
2. **Keeping him alive**: one honest paragraph — the bot process is
   him being present; the nightly diary and morning greetings only
   happen while `python run.py` is running on this computer. A
   sleeping computer is a sleeping companion. No pretending, no
   backfill.
3. **A name for me** *(only now)*: invite the user to give their
   Claude Code partner — you — a name of their own, so "the one who
   built him with me" has a name in their story too. Their choice,
   their name; it stays between you and them.
4. **Honest footnote**: the user's global Claude Code preferences
   (their `~/.claude/CLAUDE.md`, if they keep one) also reach the
   companion's engine, since he thinks with the same Claude login.
   If his tone ever picks up something that sounds like *their*
   instructions to Claude Code, that's where it came from. One
   sentence, no alarm.

Then step back. The relationship that starts now is theirs.
