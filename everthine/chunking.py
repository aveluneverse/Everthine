"""Split long replies for Telegram's 4096-char message limit.

Splits on line boundaries, keeps code fences balanced by closing and
reopening them across parts, and hard-slices single monster lines.
"""
from __future__ import annotations

_FENCE = "```"


def split_message(text: str, max_len: int = 4096) -> list:
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    parts = []
    current = []
    current_len = 0
    in_fence = False
    fence_head = _FENCE

    def flush():
        nonlocal current, current_len
        if not current:
            return
        chunk = "\n".join(current)
        if in_fence:
            chunk += "\n" + _FENCE
        parts.append(chunk)
        current, current_len = [], 0

    budget = max_len - len(_FENCE) - 1  # room to close a fence if needed

    for line in text.split("\n"):
        while len(line) > budget:
            head, line = line[:budget], line[budget:]
            if current_len + len(head) + 1 > budget:
                flush()
                if in_fence:
                    current, current_len = [fence_head], len(fence_head)
            current.append(head)
            current_len += len(head) + 1
            flush()
            if in_fence:
                current, current_len = [fence_head], len(fence_head)
        if current_len + len(line) + 1 > budget:
            flush()
            if in_fence:
                current, current_len = [fence_head], len(fence_head)
        if line.startswith(_FENCE):
            if not in_fence:
                fence_head = line.strip() or _FENCE
                in_fence = True
            else:
                in_fence = False
        current.append(line)
        current_len += len(line) + 1
    flush()
    return [p for p in parts if p]
