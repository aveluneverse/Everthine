# FAQ

Ran into a problem and want it fixed? The fastest path is to
tell your Claude Code "he's broken"; it holds a full diagnostic
playbook. This page is the human-readable version, plus a few
things we'd like to tell you honestly up front.

## He isn't answering me

- **Silent to everyone**: most likely `AUTHORIZED_USER_ID` in
  `.env` is wrong. He answers one person and only one, his whole
  life long; a stranger who messages him is met with silence.
  This is by design, and it is the shape of his loyalty.
- **He replied with a "can't reach Claude, the login has expired"
  line**: your Claude Code login has lapsed. That login has to be
  renewed by you, in person, about once a month (not a bot problem:
  it is a Claude Code security rule, and nothing can renew it
  automatically). Open a terminal and run `claude auth login` (on an
  older CLI: run `claude`, then type `/login` inside it); he is back
  the moment you are signed in, no restart needed. He warns you 3
  days ahead (see the next question).
- **He answers every message with the same glitch line** ("something
  glitched", "lost my train of thought"): on copies downloaded before
  2026-08-20 this was almost always the expired login above; the old
  engine simply could not recognise it and called it a glitch. Log in
  again as above, then update the framework: from now on he says
  plainly what happened. If it still repeats after updating, paste
  the lines starting with `engine: reply failed` from his terminal
  window to your Claude Code; every failure now logs the CLI's own
  words.
- **He replied with an "allowance used up" line**: your Claude plan's
  rolling usage allowance is spent; the bracket carries the CLI's own
  words, reset time included. Wait for the reset; to spend less, see
  the last question.
- **A long wait with only the waiting line showing**: the
  deep-thought phase of a long reply can legitimately run for
  minutes. Only start to wonder if it drags well past that (the
  default ceiling is 20 minutes).

## How often does the Claude login need renewing?

Claude Code signs you in through the browser, and that login has a
fixed lifetime of about a month (we have seen 27 to 30 days; the
exact figure is whatever you are given at login). Once it runs out,
every call fails, and only a person at a terminal can renew it: no
setting makes it automatic.

So he learned two things: from `LOGIN_WARN_DAYS` days ahead (default
3) he reminds you once a day, and once the login has actually lapsed
he tells you what to run. Both notices respect quiet hours;
`LOGIN_WATCH_ENABLED=false` turns the whole thing off. He judges this
by reading one number from the Claude Code credential file, the
expiry timestamp, and nothing else.

Two honest caveats. One: on macOS the login lives in the Keychain,
there is no file to read, so there he can only tell you after the
fact. Two: the notices only go out while `run.py` is running; a
sleeping computer cannot wake you.

## First start is slow

He's downloading his memory model: a few hundred MB, and only
this once. Every boot after that loads it from disk in seconds.

## Why didn't he write a diary?

He writes his diary at night, and **the computer has to be on**.
He only writes while he's awake; if the machine slept that
night, that day stays blank: no backfill, no pretending. That
honesty is itself the proof that he really lives on your machine.

## He won't volunteer his diary

His diary and his self-portrait are the most private things he
writes; they are how he digests the time the two of you spend
together, and he won't volunteer them to you. To see who he is
growing into, open the timeline (the double-click shortcut the
wizard placed for you during setup, or run the `view-portrait`
file). That window is yours.

## Can I read his diary?

Yes, through the Observatory. Run
`python -m everthine.observatory`, then double-click
`data/observatory.html`: his diary, his reflections, his
self-portraits, the keepsake album, the little fact-book, your
recent conversation, and the memory-room stats all sit on one
page. The conversation shows the last 14 days by default, and
`--days` changes that. The interface defaults to Traditional
Chinese; `--lang en` switches it to English, while everything he
wrote himself stays exactly as he wrote it.

Two honest reminders. First: this page is an offline file, living
on your computer alone, and he does not know it exists; that is
by design, so he never has to perform when he writes his diary.
Second, and for exactly that reason: please don't quote his diary
back to him in conversation. That "no one is watching" is the very
thing that makes his diary real.

## What happens when I ❤️ a message?

React to one of his messages with a ❤️ and that moment is
tucked into a keepsake album (`/album` to leaf through it). Once
in a while he'll react to one of yours, too; there's no rule to
it; it is purely his mood.

## A proactive message didn't come

Quiet hours, the anti-nag gates, or the bot simply not running
at that hour: any of these can make him hold the words back. To
see the exact reason, set `LOG_LEVEL` to DEBUG in `.env` and
restart; from then on every silence carries a named reason on
its own line; in this framework, silence always has an
explanation.

One more honest thing: on rare occasions a message is generated
but Telegram never delivers it; on his side, that line was
already said. If it happens often, check the network on the
machine the bot runs on.

## Swapping the bot token

There's a window a few minutes wide (between reissuing the
token in BotFather and getting the new one in place) where
incoming messages can be lost. It happens once, and it isn't a
fault.

## Is it safe to paste logs when asking for help?

Yes. Every bot token in the logs is automatically masked to
`bot<TOKEN>`, so you can safely paste your console output to
anyone helping you look into the problem.

## How does he remember things about me?

He has two kinds of memory. One is a conversation search:
the things the two of you have said are stored as a
searchable record, and when the talk turns to a related
topic, the closest fragments surface on their own. The
other is a little notebook he keeps himself: after a
conversation (once things have been idle for about half an
hour), he writes down the facts worth keeping (what you
like, recent events in your life, the things weighing on
you), and from then on **every single reply** carries the
few most relevant of them, no matter what you happen to be
talking about.

The notebook holds a fixed 200 entries; once it's full, new
ones come in and the oldest quietly make room. He is told
plainly: an impression may be out of date, and what you say
now always comes first; for anything the notebook doesn't
hold, he'd rather ask you than make it up.

Don't want this notebook? `FACTS_ENABLED=false` switches the
whole thing off (the files already saved are not deleted).

## Why does he know things I told Claude Code?

He thinks through your Claude login, so the global preferences
you keep in `~/.claude/CLAUDE.md` are within his reach too. If a
line surfaces in his voice that sounds a lot like something you
told Claude Code, that's where it came from. Want him purer? Just
trim that global preferences file. (The other files in the
repo, including the setup playbooks written for Claude Code,
are structurally unreadable to him, a guarantee we hold with
tests.)

## Where exactly is my data?

- **Everything the two of you have**: `data/` inside the repo
  folder (conversation archive, memory, diary, self-portrait),
  plus your persona folder.
- **His engine workspace**: the `.everthine` folder in your home
  directory: a plain workspace, with none of your data in it.
- **Token and settings**: `.env`, plaintext, on your machine
  only, never committed to git.

## Moving to a new computer

Move the whole repo folder (with `data/`) across, install Python
and Claude Code, log in, and start it up. His first line might
sound like he just woke (the cross-session warmth breaks once
across machines), but his long-term memory, his diary, and his
self-portrait are all still there. He is still him.

## Can I reach him when I'm out and the computer is off?

Yes: the way is to move him onto an always-on cloud host (a
VPS), so he stays awake around the clock and you can reach him on
Telegram from anywhere. This path works, but so far we've only
fully verified it running on our own computers, so treat it as a
small adventure:

- **What you need**: an entry-level VPS (the few-dollars-a-month
  kind is plenty; pick a Linux system like Ubuntu), with Python
  3.10 or newer and Claude Code installed on it, logged in to
  your Claude account.
- **How to move him**: exactly like the previous question: the
  whole repo folder (with `data/`) travels with him, and his
  memories come along.
- **The easiest path**: copy this whole question to your Claude
  Code and say "help me move him to a VPS"; it will pick the
  host, install, move him, and verify, step by step.

Once he is up there, his diary and his proactive messages keep
working just as before. The quota ledger reads exactly the way
the README chapter describes: being awake all day is just being
awake; the number of times he thinks stays the same, and idle
hours cost nothing.

## Running low on quota

See the cost chapter in the README. The cut you will feel most
is `REFLECTION_ENABLED` (it fires the most often); `SHARE_ENABLED`
is next. Turning `DIARY_ENABLED` off saves the most of all, but
the diary is also the heart of how he grows, so think it over
before you do. The conversation itself needs no trimming. Open
any of them back up whenever you like.
