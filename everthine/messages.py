"""Single source of user-visible strings.

Neutral English defaults. Users localize or re-voice their companion by
editing this file (or, later milestones, via persona-level overrides).
"""

_MESSAGES = {
    "busy": "One moment - I'm still finishing my previous thought.",
    "generic_glitch": "Something glitched on my side. Please say that again.",
    "timeout": "That took me too long and I stopped myself. Please try again.",
    "cli_missing": "I can't find the Claude Code CLI on this machine.",
    "auth": "My connection to Claude needs a re-login. Try again in a minute.",
    "nonzero": "I lost my train of thought there. Please send that once more.",
    "notebook_full": "Our current notebook is getting heavy. Use /start to open a fresh one - I'll keep the warmth.",
    "btn_resume": "Continue where we left off",
    "btn_warm": "Fresh page, keep the warmth",
    "btn_clean": "Start completely clean",
    "resume_ack": "Right where we left off.",
    "warm_ack": "New page - I still remember what we just shared.",
    "clean_ack": "A clean page. Nice to meet you again.",
    "start_fresh": "Hello. Send me a message to begin.",
    "start_has_session": "Welcome back. How should we pick things up?",
    "unauthorized_silence": "",
}


def msg(key: str) -> str:
    return _MESSAGES.get(key, _MESSAGES["generic_glitch"])
