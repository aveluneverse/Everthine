"""A stand-in for the Claude Code CLI, driven by FAKE_CLAUDE_MODE.

Modes: ok | malformed | nonobject | result_error | auth_once | slow | exit1
    | login_expired | not_logged_in | rate_limited
    | stream_ok | stream_slow_ok | stream_die_mid | stream_result_error
    | stream_auth_once | stream_stall | stream_login_expired
    | stream_not_logged_in | stream_rate_limited | stream_text_then_server_error
    | stream_text_then_silent_exit
auth_once uses FAKE_CLAUDE_STATE (a file path) to fail with a 401 fingerprint
on the first call and succeed on the second - exercising the retry loop.
stream_slow_ok mirrors stream_ok but sits silent (no text delta, like a long
thinking phase) before replying - it exists to race a tiny total-timeout
deadline against a generous one without ever tripping the stall guard.
"""
import json
import os
import sys
import time


def main() -> None:
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

    def _emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _delta(text):
        _emit({"type": "stream_event",
               "event": {"type": "content_block_delta",
                         "delta": {"type": "text_delta", "text": text}}})

    if mode.startswith("stream_"):
        session = "fake-stream-123"
        if "--resume" in sys.argv:
            session = sys.argv[sys.argv.index("--resume") + 1]
        _emit({"type": "system", "subtype": "init"})

        if mode == "stream_auth_once":
            state = os.environ["FAKE_CLAUDE_STATE"]
            if not os.path.exists(state):
                with open(state, "w", encoding="utf-8") as fh:
                    fh.write("tried")
                sys.stderr.write('API Error: 401 {"type":"authentication_error"}\n')
                sys.exit(1)
            mode = "stream_ok"

        if mode == "stream_ok":
            for chunk in ("Hello ", "there, ", "friend."):
                _delta(chunk)
            _emit({"type": "result", "result": "Hello there, friend.",
                   "session_id": session, "is_error": False})
            return

        if mode == "stream_slow_ok":
            time.sleep(2.5)  # outlasts a tiny total-timeout, well under a stall
            for chunk in ("Hello ", "there, ", "friend."):
                _delta(chunk)
            _emit({"type": "result", "result": "Hello there, friend.",
                   "session_id": session, "is_error": False})
            return

        if mode == "stream_die_mid":
            _delta("partial ")
            _delta("thought")
            sys.stderr.write("boom\n")
            sys.exit(1)

        if mode == "stream_result_error":
            _emit({"type": "result", "result": "API Error: 500 internal",
                   "session_id": session, "is_error": True})
            return

        if mode == "stream_text_then_server_error":
            # The reply text itself happens to talk about logins/limits for
            # unrelated (conversational) reasons, then the run fails for a
            # THIRD, unrelated reason -- the emitted text must never be
            # mistaken for the CLI's own words when result_text says plenty.
            _delta("My login expired yesterday, remember? ")
            _emit({"type": "result", "subtype": "success", "is_error": True,
                   "result": "API Error: 500 internal", "session_id": session})
            sys.exit(1)

        if mode == "stream_text_then_silent_exit":
            # No result event, no stderr -- the emitted text is ALL there is,
            # so it must still be read as the fallback (older CLI builds
            # surfaced "API Error: 401 ..." exactly this way, as a delta).
            _delta("API Error: 401 Invalid authentication credentials")
            sys.exit(1)

        if mode == "stream_stall":
            _delta("before the silence ")
            time.sleep(60)
            return

        if mode in ("stream_login_expired", "stream_not_logged_in", "stream_rate_limited"):
            text = {
                "stream_login_expired": "Failed to authenticate: OAuth session expired and could not be refreshed",
                "stream_not_logged_in": "Not logged in \u00b7 Please run /login",
                "stream_rate_limited": "You've hit your session limit \u00b7 resets 3:45pm",
            }[mode]
            _emit({"type": "result", "subtype": "success", "is_error": True,
                   "result": text, "session_id": session})
            sys.exit(1)

    if mode == "auth_once":
        state = os.environ["FAKE_CLAUDE_STATE"]
        if not os.path.exists(state):
            with open(state, "w", encoding="utf-8") as fh:
                fh.write("tried")
            sys.stderr.write('API Error: 401 {"type":"authentication_error"}\n')
            sys.exit(1)
        mode = "ok"

    if mode == "slow":
        time.sleep(10)
        mode = "ok"

    if mode == "exit1":
        sys.stderr.write("boom\n")
        sys.exit(1)

    if mode in ("login_expired", "not_logged_in", "rate_limited"):
        text = {
            "login_expired": "Failed to authenticate: OAuth session expired and could not be refreshed",
            "not_logged_in": "Not logged in \u00b7 Please run /login",
            "rate_limited": "You've hit your session limit \u00b7 resets 3:45pm",
        }[mode]
        sys.stdout.write(json.dumps({"type": "result", "subtype": "success",
                                     "is_error": True, "result": text,
                                     "session_id": "fake-session-err"}))
        sys.exit(1)

    if mode == "malformed":
        sys.stdout.write("this is not json at all")
        return

    if mode == "nonobject":
        sys.stdout.write("[1, 2, 3]")
        return

    if mode == "result_error":
        sys.stdout.write(json.dumps({"result": "API Error: 500 internal",
                                     "is_error": True,
                                     "session_id": "fake-session-err"}))
        return

    reply = {"result": f"echo:{prompt.strip()[:60]}", "session_id": "fake-session-123"}
    if "--resume" in sys.argv:
        reply["session_id"] = sys.argv[sys.argv.index("--resume") + 1]
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
