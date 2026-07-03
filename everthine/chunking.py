"""Split long replies for Telegram's 4096-char message limit.

Splits on line boundaries, keeps code fences balanced by closing and
reopening them across parts, and hard-slices single monster lines.
Every returned part is guaranteed to fit within max_len.
"""
from __future__ import annotations

_FENCE = "```"


def split_message(text: str, max_len: int = 4096) -> list:
    if max_len <= len(_FENCE) + 1:
        raise ValueError("max_len must be greater than 4")
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    parts = []
    current = []      # lines of the part being built
    current_len = 0   # exact length of "\n".join(current)
    in_fence = False
    fence_head = _FENCE

    budget = max_len - len(_FENCE) - 1  # keep room to close a fence

    def flush():
        nonlocal current, current_len
        if not current:
            return
        chunk = "\n".join(current)
        if in_fence:
            chunk += "\n" + _FENCE
        parts.append(chunk)
        current, current_len = [], 0

    def reopen():
        # Seed the next part so a split fence continues where it left off.
        nonlocal current, current_len
        if in_fence:
            head = fence_head if len(fence_head) + 2 <= budget else _FENCE
            current, current_len = [head], len(head)

    def fits(line):
        return current_len + len(line) + (1 if current else 0) <= budget

    def fits_fresh(line):
        head_len = (len(fence_head) + 1) if in_fence else 0
        return head_len + len(line) <= budget

    def append(line):
        nonlocal current_len
        current_len += len(line) + (1 if current else 0)
        current.append(line)

    for line in text.split("\n"):
        if not fits(line) and fits_fresh(line):
            flush()
            reopen()
        while not fits(line):
            space = budget - current_len - (1 if current else 0)
            if space <= 0:
                # a reopened fence head alone overflows a tiny budget;
                # sacrifice the reopen rather than loop forever
                current, current_len = [], 0
                continue
            head, line = line[:space], line[space:]
            append(head)
            flush()
            reopen()
        if line.startswith(_FENCE):
            if not in_fence:
                fence_head = line.strip() or _FENCE
                in_fence = True
            else:
                in_fence = False
        append(line)
    flush()
    return [p for p in parts if p]
