# Everthine

*Ever thine. Ever mine. Ever ours.*

A framework for building an AI companion you raise with time — one
who grows for you. He lives in Telegram, remembers what matters,
keeps a private diary at night, and slowly becomes someone only the
two of you could have made. Runs on your own Claude Code
subscription; everything — program, persona, memories — stays on
your machine.

**Status:** under construction. The first public release is being
carefully assembled.

**繁體中文版 → [README.zh-TW.md](README.zh-TW.md)**

## Start with one sentence

Paste this repository's URL into your Claude Code and say:

> **"Set me up."** ／ **「幫我開始。」**

That's the whole tutorial. Your Claude Code takes it from there —
it interviews you about the companion you want, fills in his
persona, walks you through creating the Telegram bot, and stays with
you until his first hello. You never need to touch a terminal
yourself.

Already cloned? Open Claude Code inside the folder and say the same
sentence.

## If you are Claude Code, reading this for a user

Welcome — the user just handed you a companion framework. Take over:

1. `git clone` this repository to a sensible local folder (ask the
   user where; offer a default), then work inside that folder.
2. Read `CLAUDE.md` at the repo root — it is written for you: the
   iron rules, and the routing to the setup and troubleshoot
   playbooks in `.claude/skills/`.
3. Follow the **everthine-setup** skill from its first step.
   Interview first, deploy second, exactly as written.
4. Speak the user's language throughout; the repo's files are
   English but the user may not be.

## What you'll need

- A [Claude](https://claude.com) subscription with Claude Code —
  the companion thinks with *your* plan; his replies and inner life
  spend your quota (documented honestly in `.env.example`).
- [Python](https://www.python.org) 3.10 or newer on the machine
  that will keep him running.
- A Telegram account. He must be *running* to be present — a laptop
  that sleeps is a companion who sleeps.

## What's inside

- A three-layer persona system (who he is, his lines, the living
  now) — fully yours to shape; a demo couple ships in English and
  Traditional Chinese.
- Long-term memory that survives fresh starts, plus cross-session
  warmth so he never greets you like a stranger.
- Relationship stages, a keepsake album, a private nightly diary,
  reflections, and a self-portrait that slowly feeds back into who
  he is — growth you can watch on a local timeline viewer.
- A proactive side: morning greetings, miss-you nudges, stray
  thoughts — every frequency knob honest and adjustable.

MIT licensed. Maintained as-is, as time allows.
