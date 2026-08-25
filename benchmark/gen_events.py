#!/usr/bin/env python3
"""Generate a valid events JSONL file of a target byte size.

Writes raw JSON directly (no pydantic) so generation is not the bottleneck.
Every line satisfies schema-events.yaml: uuid key, enum kind, float amount,
datetime at, dict props.
"""
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

KINDS = ("click", "view", "purchase")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
PATHS = ("/", "/pricing", "/docs/getting-started",
         "/blog/append-only-logs", "/checkout")


def gen(path: str, target_bytes: int) -> tuple[int, int]:
    written = n = 0
    buf = []
    with open(path, "wb") as f:
        while written < target_bytes:
            rec = {
                "event_id": str(uuid.UUID(int=n, version=4)),
                "kind": KINDS[n % 3],
                "amount": round((n % 10000) / 100.0, 2),
                "at": (BASE + timedelta(seconds=n)).isoformat(),
                "props": {"session": f"s-{n % 100000:08d}", "path": PATHS[n % 5],
                          "seq": n % 997, "ok": n % 2 == 0},
            }
            line = json.dumps(rec, separators=(",", ":")).encode() + b"\n"
            buf.append(line)
            written += len(line)
            n += 1
            if len(buf) >= 50_000:
                f.write(b"".join(buf))
                buf.clear()
        if buf:
            f.write(b"".join(buf))
    return n, os.path.getsize(path)


def truncate_at_line(src: str, dst: str, target_bytes: int) -> tuple[int, int]:
    """Copy the leading whole lines of src that fit in target_bytes."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        remaining, n = target_bytes, 0
        for raw in fi:
            if len(raw) > remaining:
                break
            fo.write(raw)
            remaining -= len(raw)
            n += 1
    return n, os.path.getsize(dst)


if __name__ == "__main__":
    if sys.argv[1] == "gen":
        print(gen(sys.argv[2], int(sys.argv[3])))
    else:
        print(truncate_at_line(sys.argv[2], sys.argv[3], int(sys.argv[4])))
