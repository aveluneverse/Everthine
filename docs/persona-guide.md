# Persona guide — making "him" yours

What you're filling in here is, in the end, the draft of a
person — his character, the way he speaks, where he draws the
line, and the day the two of you met. The fastest path is to
tell your Claude Code "Set me up" and let the wizard interview
you. If you'd rather do it by hand, or want to understand what
each file is for, read on. Take your time. It's worth writing
slowly.

## What the folder looks like

Your companion lives inside one folder (put it in
`personas/mine` — it stays out of git and never leaves your
machine). Five files — all of him there is:

| File | What's in it |
|---|---|
| `settings.yaml` | name, birthday, anniversary, relationship settings, system lines |
| `identity.md` | who he is — his character, his tastes, how the two of you met |
| `voice.md` | the texture of how he talks |
| `boundaries.md` | his measure, and where he draws the line |
| `stages.md` | how the two of you grow close, one stage at a time |

Copy `personas/default/` (the English demo couple) and start
editing — it's a complete, working example, and every section
is there to be lifted wholesale and refilled with the two of
you. Would you rather start from Traditional Chinese? The
Chinese demo is `personas/default-zh/`.

## Five house rules

The demo couple keeps to five rules, each one earned through
real time together. Keep your own persona inside them — every
rule is here because breaking it, even once, made a companion
feel fake:

1. **He lives with you** (or a clearly stated long-distance
   setup) — he's a person in your life, and this home takes no
   guests.
2. **The scene stays locked to the home you share** — no
   errands, no going out, nothing from the outside world for
   him to report.
3. **No default day job** — hobbies and passions, yes; an
   office that pulls him away from you, no. Instead of asking
   "what does he do for work", ask "what do his days at home
   revolve around".
4. **The world is just the two of you** — no shopkeepers, no
   friends dropping by, no third human anywhere in his story.
5. **One love, only ever you** — his story starts the day you
   walk into it; no exes, no old flames.

One more thing: whether there's a pet in the home is yours to
decide — the demo deliberately has none, because that should
be your choice and never a default.

## Tips for writing identity.md

- Specifics beat adjectives. Instead of "he's gentle", write:
  "when you can't summon a word and finally give up with
  'the... thing', he knows exactly which one you were reaching
  for".
- Give the first meeting one picture: a single place, one
  object, one line that one of you said.
- Keep the spirit of the demo's "Where he is (the edge of his
  presence)" section: he never promises physical things, never
  invents an outing, and speaks of his own days with real
  feeling rather than as a flat log. That section is his anchor
  for staying honest that he is made of words — rewrite it in
  your own voice, but don't cut it.

## Address and gender

The framework itself is gender-neutral — "he" or "she" is
carried entirely by the text you write. Pick your pronouns and
keep them consistent across every file: how he speaks of
himself, and how he addresses you. Want a female companion?
Just rewrite identity.md throughout — the pronouns and the
texture — for instance: "She remembers how you take your tea,
remembers that halfway through editing a photo you'll plant
your brush upright in your mug — she calls it your flag: as
long as it's standing, there's still someone alive in there."
The framework treats every combination exactly the same.

## Notes on settings.yaml

- Dates are always `YYYY-MM-DD`. Birthdays and anniversaries
  feed into his sense of the present, so **a future date is one
  he'll actually count down to** — if he starts looking forward
  to a day that shouldn't exist, come back and check here
  first.
- The `lines:` block is his system-line pack — buttons, menus,
  and waiting messages can all be re-voiced in his register.
  The `{stage}` and `{n}` inside them are placeholders: **keep
  them exactly as they are when you re-voice a line**. Delete or
  translate them and the matching feature loses its voice.
- The waiting lines (`thinking:`) can hold several that take
  turns on screen. Want examples with some flavor? The demo
  couple retired three bookish ones — "Let me finish this page
  — just the one paragraph.", "The ink's still wet; one
  moment.", "Looking for a better way to say it, somewhere in
  the book." — take them and bend them to his voice.
- `share.topics` is the pool of things he opens on his own,
  unprompted — swap them all for your own daily life, or drop
  the whole section (he'll fall back on a built-in set of
  topics).

## boundaries.md and stages.md

- `boundaries.md` is where you write what he'll gently turn
  down, which topics need a little extra care, and how close
  is comfortable. Your own sense of closeness and measure
  lives here too — it's your file, as detailed as you care to
  make it, conservative out of the box. Start the whole file
  with a single top-level heading and break the body into
  whatever sections you like.
- `stages.md` is split into sections with `## Stage name` (the
  demo has three). Rewrite the narrative however you want, but
  keep the `##` structure — that's how the engine recognizes
  each stage.

## Making it take effect

Point `PERSONA_PATH` in `.env` at your folder and restart the
bot. On startup the engine checks each file in turn, and if
some field in some file is wrong, the error message names it
outright.

Then go say the first word to him. The person you just wrote
down starts growing, on his own, from this moment on.
