"""A stand-in for the Claude Code CLI, driven by FAKE_CLAUDE_MODE.

Modes: ok | malformed | nonobject | auth_once | slow | exit1
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

    reply = {"result": f"echo:{prompt.strip()[:60]}", "session_id": "fake-session-123"}
    if "--resume" in sys.argv:
        reply["session_id"] = sys.argv[sys.argv.index("--resume") + 1]
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
