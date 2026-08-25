#!/usr/bin/env python3
"""Turn results.jsonl into a markdown report: one table per metric."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIONS = ["schema", "get (cold idx)", "get (warm idx)", "keys", "list --limit 100",
           "validate", "put (1 record)", "delete", "compact", "repair"]
SIZES = ["1MB", "10MB", "100MB", "500MB", "1GB", "2GB", "5GB"]


def load():
    rows = {}
    meta = {}
    with open(os.path.join(HERE, "results", "results.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            rows[(r["action"], r["size"])] = r
            if "records" in r:
                meta[r["size"]] = (r["bytes"], r["records"])
    return rows, meta


def cell(r, metric):
    if r is None:
        return "-"
    v = r.get("verdict", "ok")
    if v.startswith("skipped"):
        return "skip"
    if v == "killed:memory":
        return "**OOM**"
    if v == "killed:timeout":
        return "**TIMEOUT**"
    if r.get("exit") not in (0, 1):
        return f"err {r['exit']}"
    if metric == "wall_s":
        s = r["wall_s"]
        return f"{s*1000:.0f}ms" if s < 1 else f"{s:.1f}s"
    if metric == "peak_rss_mb":
        m = r["peak_rss_mb"]
        return f"{m/1024:.1f}GB" if m >= 1024 else f"{m:.0f}MB"
    b = r["stdout_bytes"]
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if b >= div:
            return f"{b/div:.1f}{unit}"
    return f"{b}B"


def table(rows, metric, sizes):
    out = ["| action | " + " | ".join(sizes) + " |",
           "|---|" + "---|" * len(sizes)]
    for a in ACTIONS:
        if not any((a, s) in rows for s in sizes):
            continue
        out.append(f"| `{a}` | " + " | ".join(cell(rows.get((a, s)), metric)
                                              for s in sizes) + " |")
    return "\n".join(out)


def main():
    rows, meta = load()
    sizes = [s for s in SIZES if any((a, s) in rows for a in ACTIONS)]
    parts = ["# datafile.py benchmark", "", "## Store sizes", "",
             "| size | bytes | records |", "|---|---|---|"]
    for s in sizes:
        if s in meta:
            b, n = meta[s]
            parts.append(f"| {s} | {b:,} | {n:,} |")
    for title, metric in (("Wall time", "wall_s"), ("Peak RSS", "peak_rss_mb"),
                          ("stdout size", "stdout_bytes")):
        parts += ["", f"## {title}", "", table(rows, metric, sizes)]
    text = "\n".join(parts) + "\n"
    with open(os.path.join(HERE, "results", "report.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
