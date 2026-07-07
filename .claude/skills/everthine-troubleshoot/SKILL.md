---
name: everthine-troubleshoot
description: Diagnose and fix a misbehaving Everthine companion. Use when the user reports errors, silence, missing messages, missing diaries, a bot that won't start, or anything that feels broken ("it's broken", "he's not answering", "怎麼沒反應", "沒回我").
---

# Everthine troubleshoot

Diagnose before touching anything. Ask what they saw, run the checks
yourself, and match against the table below — every known failure has
a named symptom. Speak the user's language; keep the companion spoken
of as a person ("he hasn't been able to answer"), because to the user
he is one.

Safe to know: bot tokens are automatically masked in all logs
(`bot<TOKEN>`), so asking the user to paste console output is safe.

## Symptom table

| Symptom | Cause | Fix |
|---|---|---|
| Startup dies: `BOT_TOKEN is required` | `.env` missing or empty token | Re-run the token step of the everthine-setup deploy guide |
| Startup dies: `AUTHORIZED_USER_ID is required` or `must be an integer` | id missing or has stray characters | Get the numeric id again (@userinfobot), fix `.env` |
| Startup dies mentioning the Claude CLI | `claude` not found by a plain shell | Reinstall or relink the Claude Code CLI; verify `claude --version` in a fresh terminal |
| Startup dies naming a persona file and key | That persona file is malformed — the message names the exact file and field | Fix exactly what it names; placeholders like `{stage}` must survive re-voicing |
| He replies with his "need to log in again" line | The Claude CLI session expired (an auth error from the engine) | Run `claude` once in a terminal and complete the login; no restart needed |
| Bot runs, but total silence to *everyone* | Wrong `AUTHORIZED_USER_ID` — he answers one id and no one else; **silence is by design** for strangers | Confirm the user's own numeric id matches `.env` |
| First start very slow, seems stuck | One-time memory-model download (a few hundred MB) | Wait; later boots load from disk in seconds |
| Long waits with only the "thinking" line | The deep-thought phase of a long answer — minutes are legitimate | Only worry past `STREAM_TOTAL_TIMEOUT_S` (default 1200s); raise it before assuming a hang |
| Morning greeting or proactive messages never arrive | Quiet hours, the anti-disturb gates, or the bot wasn't running at that hour | Set `LOG_LEVEL` to DEBUG in `.env`, restart, read the named skip reasons each tick — every silence has an explanation |
| No diary this morning | The bot wasn't running during the nightly window — he only writes when he's awake; there is no backfill and he won't pretend | Keep `run.py` running overnight; check `DIARY_WINDOW_START_HOUR` |
| A proactive message was generated but never received | The Telegram send failed after the fact — his side counts it as said | Rare and transient; if frequent, check the network the bot runs on |

## When nothing matches

1. Reproduce with `LOG_LEVEL` set to DEBUG and read the last 50 log
   lines.
2. Check `.env` against `.env.example` for typo'd keys — unknown
   keys are silently ignored, so a misspelled key means the default
   quietly applies.
3. Do **not** edit engine code as a workaround. If it truly looks
   like a framework bug, help the user write a clear report (symptom,
   log excerpt — tokens are already masked — and steps to reproduce)
   for the project's issue page.
