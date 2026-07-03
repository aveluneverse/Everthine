"""A stand-in for the Claude Code CLI, driven by FAKE_CLAUDE_MODE.

Modes: ok | malformed | nonobject | result_error | auth_once | slow | exit1
    | stream_ok | stream_die_mid | stream_result_error | stream_auth_once
    | stream_stall
auth_once uses FAKE_CLAUDE_STATE (a file path) to fail with a 401 fingerprint
on the first call and succeed on the second - exercising the retry loop.
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

        if mode == "stream_die_mid":
            _delta("partial ")
            _delta("thought")
            sys.stderr.write("boom\n")
            sys.exit(1)

        if mode == "stream_result_error":
            _emit({"type": "result", "result": "API Error: 500 internal",
                   "session_id": session, "is_error": True})
            return

        if mode == "stream_stall":
            _delta("before the silence ")
            time.sleep(60)
            return

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
