#!/usr/bin/env python3
"""Benchmark every datafile.py action across JSONL store sizes up to 2 GB.

Each action runs as its own process. Wall time comes from a monotonic clock,
peak RSS from /usr/bin/time -l (rusage), and a watchdog kills any process that
exceeds MEM_CAP or TIMEOUT so a memory blow-up is recorded as a result instead
of taking the machine down.
"""
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "datafile.py")
CONTRACT = os.path.join(ROOT, "schema-events.yaml")
DATA = os.path.join(HERE, "data")
RESULTS = os.path.join(HERE, "results", "results.jsonl")
SINK = os.path.join(HERE, "results", ".stdout")

MEM_CAP = 12 * 1024**3          # kill above 12 GiB on an 18 GiB machine
TIMEOUT = 600                   # seconds per action
POLL = 0.05

MB, GB = 1024**2, 1024**3
SIZES = [("1MB", MB), ("10MB", 10 * MB), ("100MB", 100 * MB), ("500MB", 500 * MB),
         ("1GB", GB), ("2GB", 2 * GB)]
MASTER_BYTES = max(n for _, n in SIZES)   # every tier is a prefix of the master

RSS_RE = re.compile(rb"(\d+)\s+maximum resident set size")


def pgroup_rss(pgid: int) -> int:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-g", str(pgid)],
                             capture_output=True, timeout=5).stdout
        return max((int(x) * 1024 for x in out.split() if x.isdigit()), default=0)
    except Exception:
        return 0


def run(args: list[str]) -> dict:
    """Run one datafile.py invocation under time(1) with a watchdog."""
    cmd = ["/usr/bin/time", "-l", PY, APP, "-c", CONTRACT, *args]
    t0 = time.monotonic()
    with open(SINK, "wb") as sink:
        p = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.PIPE,
                             start_new_session=True)
        pgid, verdict, peak_poll = os.getpgid(p.pid), "ok", 0
        while p.poll() is None:
            time.sleep(POLL)
            peak_poll = max(peak_poll, pgroup_rss(pgid))
            if peak_poll > MEM_CAP:
                verdict = "killed:memory"
                break
            if time.monotonic() - t0 > TIMEOUT:
                verdict = "killed:timeout"
                break
        if verdict != "ok":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
        err = p.communicate()[1]
        wall = time.monotonic() - t0
    stdout_bytes = os.path.getsize(SINK)
    m = RSS_RE.search(err or b"")
    peak = int(m.group(1)) if m else peak_poll
    noisy = verdict != "ok" or p.returncode not in (0, 1)
    return {"cmd": " ".join(args), "wall_s": round(wall, 3),
            "peak_rss_mb": round(max(peak, peak_poll) / MB, 1),
            "exit": p.returncode, "verdict": verdict,
            "stdout_bytes": stdout_bytes,
            "stderr_tail": (err or b"").decode("utf-8", "replace")[-400:]
            if noisy else ""}


def event_id(n: int) -> str:
    return str(uuid.UUID(int=n, version=4))


def sidecar_rm(path: str) -> None:
    for suffix in (".idx", ".lock", ".quarantine"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


def plan(path: str, nrecords: int) -> list[tuple[str, list[str], bool]]:
    """(label, argv, drop_index_first) in execution order. Mutating last."""
    f, mid, fresh = ["-f", path], event_id(nrecords // 2), event_id(10**9)
    return [
        ("schema",        [*f, "schema"],                         False),
        ("get (cold idx)",[*f, "get", mid],                       True),
        ("get (warm idx)",[*f, "get", mid],                       False),
        ("keys",          [*f, "keys"],                           False),
        ("list --limit 100", [*f, "list", "--limit", "100"],      False),
        ("validate",      [*f, "validate"],                       False),
        ("put (1 record)",[*f, "put", json.dumps({
            "event_id": fresh, "kind": "purchase", "amount": 9.99,
            "at": "2026-08-25T12:00:00+00:00", "props": {"bench": True}})], False),
        ("delete",        [*f, "delete", fresh],                  False),
        ("compact",       [*f, "compact"],                        False),
        ("repair",        [*f, "repair"],                         False),
    ]


def main():
    only = sys.argv[1:] or None
    master = os.path.join(DATA, "events-master.jsonl")
    if not os.path.exists(master) or os.path.getsize(master) < MASTER_BYTES:
        print(f"generating {MASTER_BYTES/GB:.0f}GB master ...", flush=True)
        t = time.monotonic()
        sys.path.insert(0, HERE)
        from gen_events import gen
        n, b = gen(master, MASTER_BYTES)
        print(f"  {n} records, {b/GB:.2f} GB in {time.monotonic()-t:.1f}s", flush=True)

    failed: set[str] = set()
    if os.path.exists(RESULTS):                  # resume: keep prior verdicts
        with open(RESULTS) as f:
            for line in f:
                r = json.loads(line)
                if r.get("verdict", "ok") != "ok":
                    failed.add(r["action"])
    with open(RESULTS, "a") as out:
        for label, nbytes in SIZES:
            if only and label not in only:
                continue
            path = os.path.join(DATA, f"events-{label}.jsonl")
            sidecar_rm(path)
            sys.path.insert(0, HERE)
            from gen_events import truncate_at_line
            # a copy, not the master itself: the mutating actions rewrite it
            nrec, size = truncate_at_line(master, path, nbytes)
            print(f"\n=== {label}: {nrec:,} records, {size/MB:.1f} MB ===", flush=True)
            for name, argv, cold in plan(path, nrec):
                if name in failed:
                    rec = {"size": label, "bytes": size, "records": nrec, "action": name,
                           "verdict": "skipped:failed-at-smaller-size"}
                    print(f"  {name:<18} skipped (failed at a smaller size)", flush=True)
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    continue
                if cold:
                    sidecar_rm(path)
                r = run(argv)
                r.update(size=label, bytes=size, records=nrec, action=name)
                out.write(json.dumps(r) + "\n")
                out.flush()
                ok = r["verdict"] == "ok" and r["exit"] == 0
                flag = "" if ok else f"  <-- {r['verdict']} exit={r['exit']}"
                print(f"  {name:<18} {r['wall_s']:>8.2f}s  "
                      f"rss {r['peak_rss_mb']:>8.1f} MB  "
                      f"out {r['stdout_bytes']:>10,}B{flag}", flush=True)
                if r["verdict"] != "ok":
                    failed.add(name)
            os.unlink(path)
            sidecar_rm(path)


if __name__ == "__main__":
    main()
