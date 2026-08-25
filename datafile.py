#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic[email]>=2.0", "pyyaml>=6.0"]
# ///
"""Append-only JSONL store with a runtime-injected Pydantic contract.

An AXI-compliant CLI: TOON output, structured errors on stdout, content-first
home view. See /Users/vvonkledge/vvonkledge/axi for the standard.
"""

from __future__ import annotations

import sys

VERSION = "0.1.0"

# AXI §10 fast path: -v/-V/--version must answer before pydantic and yaml are
# imported, so a version probe costs interpreter startup and nothing else.
if __name__ == "__main__" and len(sys.argv) == 2 and sys.argv[1] in ("-v", "-V", "--version"):
    print(VERSION)
    raise SystemExit(0)

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import UTC
from typing import Generic, NamedTuple, TypeVar


def _missing_dependency(name: str):
    """§6: a raw ModuleNotFoundError traceback is leaked dependency output."""
    print(f"error: missing dependency {name}\ncode: DEPENDENCY_ERROR\nhelp[1]:\n"
          "  Run `uv run datafile.py ...` so declared dependencies install automatically")
    raise SystemExit(1)


try:
    from pydantic import BaseModel, Field, ValidationError
except ModuleNotFoundError as _e:
    _missing_dependency(_e.name)

M = TypeVar("M", bound=BaseModel)

TOMBSTONE = "_deleted"   # safe sentinel: pydantic forbids leading-underscore fields


class BadLine(NamedTuple):
    """A line that could not be turned into a valid record."""
    offset: int          # byte offset, stable across incremental scans
    line: int | None     # 1-based line number, only known from a full scan
    reason: str
    raw: str


class Store(Generic[M]):
    def __init__(self, path: str, model: type[M], key: str = "id"):
        if key not in model.model_fields:
            raise ValueError(f"model {model.__name__} has no field {key!r}")
        self.path = str(path)
        self.model = model
        self.key = key
        self.lockpath = self.path + ".lock"
        self.idxpath = self.path + ".idx"
        self._offsets: dict[str, int] = {}   # id -> byte offset of latest version
        self._ino: int | None = None         # inode the index was built against
        self._size = 0                       # bytes of the log already indexed
        self._bad = 0                        # unparseable lines in the indexed span

    # ---------------------------------------------------------------- locking

    @contextlib.contextmanager
    def _lock(self, shared: bool = False):
        """Advisory flock on a sidecar file. The sidecar is never replaced, so
        its inode stays stable across compaction."""
        fd = os.open(self.lockpath, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ write

    def put(self, record: M | dict) -> M:
        """Insert or update. Validates against the contract before writing."""
        obj = self.model.model_validate(record)
        with self._lock():
            off = self._append_unlocked(obj.model_dump(mode="json"))
            self._offsets[getattr(obj, self.key)] = off
        return obj

    def delete(self, rid: str) -> None:
        with self._lock():
            self._append_unlocked({self.key: rid, TOMBSTONE: True})
            self._offsets.pop(rid, None)

    def _append_unlocked(self, obj: dict) -> int:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                + "\n").encode("utf-8")
        with open(self.path, "a+b") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    # Close a torn tail, else it would swallow this record.
                    f.write(b"\n")
                    f.flush()
                    size += 1
            off = size
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self._size = off + len(data)
        return off

    # ------------------------------------------------------------------- scan

    def _scan_unlocked(self, start: int = 0, base_line: int | None = None):
        """Yield (offset, line_no, record_dict|None, BadLine|None) to EOF."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            f.seek(start)
            off, n = start, base_line
            for raw in f:
                nxt = off + len(raw)
                lineno, off_here = n, off
                off, n = nxt, (None if n is None else n + 1)

                def bad(reason: str, _off=off_here, _ln=lineno,
                        _raw=raw) -> BadLine:
                    return BadLine(_off, _ln, reason,
                                   _raw.decode("utf-8", "replace"))

                if not raw.endswith(b"\n"):
                    yield off_here, lineno, None, bad("torn tail: no trailing newline")
                    continue
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError as e:
                    yield off_here, lineno, None, bad(f"invalid json: {e.msg} at col {e.colno}")
                    continue
                if not isinstance(rec, dict) or self.key not in rec:
                    yield off_here, lineno, None, bad(f"missing key {self.key!r}")
                    continue
                yield off_here, lineno, rec, None

    # ------------------------------------------------------------------ index

    def _refresh_index_unlocked(self) -> None:
        """Bring the offset index up to date. O(new bytes), not O(file)."""
        if not os.path.exists(self.path):
            self._offsets, self._ino, self._size, self._bad = {}, None, 0, 0
            return
        st = os.stat(self.path)

        if self._ino is None:                       # first use: try the sidecar
            d = self._read_sidecar()
            if d and d["ino"] == st.st_ino and d["size"] <= st.st_size:
                self._offsets, self._ino = d["offsets"], d["ino"]
                self._size, self._bad = d["size"], d["bad"]

        if self._ino != st.st_ino or st.st_size < self._size:
            self._offsets, self._ino = {}, st.st_ino          # compacted
            self._size, self._bad = 0, 0

        if st.st_size > self._size:
            for off, _ln, rec, _bad in self._scan_unlocked(self._size):
                if rec is None:
                    self._bad += 1
                    continue
                if rec.get(TOMBSTONE):
                    self._offsets.pop(rec[self.key], None)
                else:
                    self._offsets[rec[self.key]] = off
            self._size = st.st_size
            self._write_sidecar()

    def _read_sidecar(self) -> dict | None:
        try:
            with open(self.idxpath, encoding="utf-8") as f:
                d = json.load(f)
            return d if {"ino", "size", "offsets", "bad"} <= d.keys() else None
        except (OSError, json.JSONDecodeError):
            return None          # a bad index is only a cold cache, never fatal

    def _write_sidecar(self) -> None:
        payload = {"ino": self._ino, "size": self._size, "bad": self._bad,
                   "offsets": self._offsets}
        with contextlib.suppress(OSError):
            self._atomic_write(self.idxpath, json.dumps(payload))

    # ------------------------------------------------------------------- read

    def get(self, rid: str) -> M | None:
        """O(1): index lookup, one seek, one line."""
        with self._lock(shared=True):
            self._refresh_index_unlocked()
            off = self._offsets.get(rid)
            if off is None:
                return None
            with open(self.path, "rb") as f:
                f.seek(off)
                raw = f.readline()
        try:
            return self.model.model_validate_json(raw)
        except ValidationError:
            return None

    def keys(self) -> list[str]:
        """O(1) after the index is warm: no file read at all."""
        with self._lock(shared=True):
            self._refresh_index_unlocked()
            return list(self._offsets)

    def page(self, limit: int) -> tuple[list[M], int, int]:
        """The first `limit` live records, each materialised by one seek.

        Returns (records, live total, unreadable lines). Unlike load(), which
        holds a model per record, this holds `limit` of them, so `list` on a
        huge store costs the index and nothing more. The line count is exact
        for unparseable lines and adds any off-contract row met while filling
        the page; `validate` remains the authoritative full check."""
        with self._lock(shared=True):
            self._refresh_index_unlocked()
            total, bad = len(self._offsets), self._bad
            out: list[M] = []
            if total and limit > 0:
                with open(self.path, "rb") as f:
                    for off in self._offsets.values():
                        f.seek(off)
                        try:
                            out.append(self.model.model_validate_json(f.readline()))
                        except ValidationError:
                            bad += 1        # indexed but off-contract, as in get()
                            continue
                        if len(out) == limit:
                            break
            return out, total, bad

    def load(self) -> tuple[dict[str, M], list[BadLine]]:
        """Full fold. Returns (records_by_id, bad_lines). Never raises."""
        with self._lock(shared=True):
            return self._load_unlocked()

    def _load_unlocked(self) -> tuple[dict[str, M], list[BadLine]]:
        alive: dict[str, M] = {}
        bad: list[BadLine] = []
        for off, ln, rec, b in self._scan_unlocked(0, base_line=1):
            if b is not None:
                bad.append(b)
                continue
            rid = rec[self.key]
            if rec.get(TOMBSTONE):
                alive.pop(rid, None)
                continue
            try:
                alive[rid] = self.model.model_validate(rec)
            except ValidationError as e:
                detail = "; ".join(
                    f"{'.'.join(str(p) for p in x['loc']) or '<root>'}: {x['msg']}"
                    for x in e.errors())
                bad.append(BadLine(off, ln, f"contract violation: {detail}",
                                   json.dumps(rec)))
        return alive, bad

    # ------------------------------------------------------------ maintenance

    def compact(self) -> tuple[int, int]:
        with self._lock():
            return self._compact_unlocked()

    def _compact_unlocked(self) -> tuple[int, int]:
        alive, bad = self._load_unlocked()
        offsets, buf, pos = {}, [], 0
        for rid, rec in alive.items():
            line = (json.dumps(rec.model_dump(mode="json"), ensure_ascii=False,
                               separators=(",", ":")) + "\n").encode("utf-8")
            offsets[rid] = pos          # rebuild the index as we write
            pos += len(line)
            buf.append(line)
        self._atomic_write(self.path, b"".join(buf))
        self._offsets, self._ino, self._size = offsets, os.stat(self.path).st_ino, pos
        self._bad = 0
        self._write_sidecar()
        return len(alive), len(bad)

    def repair(self, quarantine: str | None = None) -> tuple[int, int]:
        """Compact, moving every unusable line to a quarantine file."""
        with self._lock():
            _, bad = self._load_unlocked()
            if bad:
                q = quarantine or self.path + ".quarantine"
                with open(q, "a", encoding="utf-8") as f:
                    for b in bad:
                        f.write(json.dumps(b._asdict()) + "\n")
            return self._compact_unlocked()

    # -------------------------------------------------------------- archiving

    def append_only_violations(self) -> tuple[int, int]:
        """(updated keys, tombstones) in the active log.

        Rolling only preserves the fold when every key appears once and nothing
        is tombstoned; otherwise a record's latest version gets sealed into a
        segment that is never folded again. Streams the log without building
        models, so it costs one json.loads per line plus a set of keys.
        Advisory: a concurrent writer can append between this and roll()."""
        seen: set = set()
        dups = tombs = 0
        with self._lock(shared=True):
            for _off, _ln, rec, _bad in self._scan_unlocked(0):
                if rec is None:
                    continue
                if rec.get(TOMBSTONE):
                    tombs += 1
                    continue          # a tombstone is not also an update
                rid = rec[self.key]
                if rid in seen:
                    dups += 1
                else:
                    seen.add(rid)
        return dups, tombs

    def roll(self, archive_dir: str, min_bytes: int = 0) -> dict | None:
        """Seal the active log into a compressed segment, leaving an empty log.

        Returns the manifest row, or None when the log is under min_bytes. The
        rename and the new log happen under the write lock; compression runs
        outside it, so writers stall for two renames rather than for gzip."""
        with self._lock():
            if not os.path.exists(self.path):
                return None
            if os.path.getsize(self.path) < max(min_bytes, 1):
                return None
            os.makedirs(archive_dir, exist_ok=True)
            stem = os.path.basename(self.path)
            stem = stem[:-6] if stem.endswith(".jsonl") else stem
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            staged = os.path.join(archive_dir, f"{stem}-{stamp}.jsonl")
            n = 1
            while os.path.exists(staged) or os.path.exists(staged + ".gz"):
                staged = os.path.join(archive_dir, f"{stem}-{stamp}-{n}.jsonl")
                n += 1
            os.replace(self.path, staged)
            open(self.path, "wb").close()
            d = os.path.dirname(os.path.abspath(self.path)) or "."
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)          # persist the rename and the new empty log
            finally:
                os.close(dfd)
            self._offsets, self._size, self._bad = {}, 0, 0
            self._ino = os.stat(self.path).st_ino
            with contextlib.suppress(OSError):
                os.unlink(self.idxpath)     # stale: it maps the old inode
        return self._seal(staged, archive_dir)

    @staticmethod
    def _seal(staged: str, archive_dir: str) -> dict:
        """Compress a staged segment and append its manifest row, in one pass."""
        import gzip
        import hashlib  # only the roll path pays for these imports

        gz, h, records = staged + ".gz", hashlib.sha256(), 0
        with open(staged, "rb") as src, gzip.open(gz, "wb") as dst:
            for raw in src:
                h.update(raw)
                dst.write(raw)
                if raw.strip():
                    records += 1
        row = {"segment": os.path.basename(gz), "records": records,
               "bytes": os.path.getsize(staged), "gz_bytes": os.path.getsize(gz),
               "sha256": h.hexdigest(),
               "rolled_at": datetime.now(UTC).isoformat()}
        os.unlink(staged)
        with open(os.path.join(archive_dir, "manifest.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return row

    @staticmethod
    def _atomic_write(path: str, data: str | bytes) -> None:
        """temp + fsync + rename, then fsync the directory."""
        d = os.path.dirname(os.path.abspath(path)) or "."
        mode = "wb" if isinstance(data, bytes) else "w"
        try:                       # mkstemp is 0600 and os.replace keeps the
            perms = os.stat(path).st_mode & 0o777   # temp file's mode, which
        except FileNotFoundError:  # would silently tighten the real file
            perms = 0o644
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, mode, **({} if isinstance(data, bytes)
                                        else {"encoding": "utf-8"})) as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, perms)
            os.replace(tmp, path)
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)          # persist the rename itself
            finally:
                os.close(dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# =====================================================================
# Contract: build a Pydantic model from a YAML file at runtime
# =====================================================================

import argparse
import re
from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

try:
    import yaml
    from pydantic import ConfigDict, EmailStr, create_model
except ModuleNotFoundError as _e:
    _missing_dependency(_e.name)


class ContractError(Exception):
    """The contract file itself is invalid."""


SCALARS: dict[str, type] = {
    "str": str, "string": str, "text": str,
    "int": int, "integer": int,
    "float": float, "number": float,
    "bool": bool, "boolean": bool,
    "datetime": datetime, "date": date, "time": time,
    "uuid": UUID, "email": EmailStr,
    "any": Any,
}

# Constraints forwarded verbatim to pydantic.Field.
FIELD_KWARGS = {"ge", "le", "gt", "lt", "multiple_of",
                "min_length", "max_length", "pattern", "description", "title"}
FIELD_KEYS = {"type", "required", "default", "items", "values"} | FIELD_KWARGS


def _resolve_type(spec: dict, path: str):
    kind = spec.get("type")
    if not kind:
        raise ContractError(f"{path}: missing 'type'")

    if kind in ("enum", "choice"):
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            raise ContractError(f"{path}: type 'enum' needs a non-empty 'values' list")
        return Literal[tuple(values)]

    if kind in ("list", "array"):
        items = spec.get("items", "str")
        inner = _resolve_type(items if isinstance(items, dict) else {"type": items},
                              f"{path}.items")
        return list[inner]

    if kind in ("dict", "object", "map"):
        return dict[str, Any]

    if kind in SCALARS:
        return SCALARS[kind]

    raise ContractError(
        f"{path}: unknown type {kind!r}. "
        f"Valid: {', '.join(sorted(set(SCALARS) | {'enum', 'list', 'dict'}))}")


def load_contract(path: str) -> tuple[type[BaseModel], str]:
    """Read a YAML contract and return (model, key_field)."""
    try:
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)          # safe_load: never construct objects
    except FileNotFoundError:
        raise ContractError(f"contract not found: {path}") from None
    except yaml.YAMLError as e:
        raise ContractError(f"{path}: invalid YAML: {e}") from None

    if not isinstance(spec, dict):
        raise ContractError(f"{path}: top level must be a mapping")
    if not isinstance(spec.get("fields"), dict) or not spec["fields"]:
        raise ContractError(f"{path}: missing a non-empty 'fields' mapping")

    extra = spec.get("extra", "forbid")
    if extra not in ("forbid", "ignore", "allow"):
        raise ContractError(f"{path}: 'extra' must be forbid|ignore|allow, got {extra!r}")

    fields: dict[str, tuple] = {}
    for fname, fspec in spec["fields"].items():
        where = f"{path}: field {fname!r}"
        if isinstance(fspec, str):
            fspec = {"type": fspec}           # shorthand:  name: str
        if not isinstance(fspec, dict):
            raise ContractError(f"{where}: must be a type name or a mapping")
        if unknown := set(fspec) - FIELD_KEYS:
            raise ContractError(
                f"{where}: unknown {'keys' if len(unknown) > 1 else 'key'} "
                f"{sorted(unknown)}. Valid: {sorted(FIELD_KEYS)}")

        typ = _resolve_type(fspec, where)
        if "default" in fspec:
            default = fspec["default"]
            if default is None:
                typ = typ | None
        elif fspec.get("required", True) is False:
            typ, default = typ | None, None
        else:
            default = ...                     # required
        fields[fname] = (typ, Field(default,
                                    **{k: v for k, v in fspec.items()
                                       if k in FIELD_KWARGS}))

    key = spec.get("key", "id")
    if key not in fields:
        raise ContractError(
            f"{path}: key {key!r} is not among fields {sorted(fields)}")

    model = create_model(spec.get("name", "Record"),
                         __config__=ConfigDict(extra=extra), **fields)
    return model, key


# =====================================================================
# TOON encoder  (AXI §1)
# =====================================================================
# Token-Oriented Object Notation, spec v4.1. No Python SDK exists, so this
# implements the encoder directly: §7.2 quoting, §11 comma delimiter,
# §12 two-space indentation.

_NUMERIC = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.I)
_FORCE_QUOTE = set(':"\\[]{},')


def _toon_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _toon_needs_quote(s: str) -> bool:
    """§7.2: the conditions under which an encoder MUST quote a string."""
    return (
        s == ""
        or s != s.strip(" \t")
        or s in ("true", "false", "null")
        or bool(_NUMERIC.match(s))
        or any(c in _FORCE_QUOTE for c in s)
        or any(ord(c) < 0x20 for c in s)
        or s.startswith(("-", "#"))
    )


def _toon_number(v: float) -> str:
    """§2 canonical decimal: no trailing fractional zeros, -0 becomes 0."""
    if v != v or v in (float("inf"), float("-inf")):
        return "null"                      # not representable; JSON agrees
    if v == int(v) and abs(v) < 1e16:
        return str(int(v) + 0)             # +0 collapses -0
    return repr(v)


def _toon_scalar(v, bare: bool = False) -> str:
    """bare=True is the multi-line list form, where no delimiter is active."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _toon_number(v)
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if bare:
        if s == "" or s != s.strip(" \t") or any(ord(c) < 0x20 for c in s):
            return f'"{_toon_escape(s)}"'
        return s
    return f'"{_toon_escape(s)}"' if _toon_needs_quote(s) else s


def _toon_key(k: str) -> str:
    return f'"{_toon_escape(k)}"' if _toon_needs_quote(k) else k


def _is_scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def toon(obj: dict) -> str:
    """Encode a dict as TOON. Lists of uniform flat dicts become tabular."""
    lines: list[str] = []
    _toon_dict(obj, 0, lines)
    return "\n".join(lines)


def _toon_dict(d: dict, depth: int, lines: list[str]) -> None:
    pad = "  " * depth
    for k, v in d.items():
        key = _toon_key(str(k))
        if isinstance(v, dict) and v:
            lines.append(f"{pad}{key}:")
            _toon_dict(v, depth + 1, lines)
        elif isinstance(v, dict):
            lines.append(f"{pad}{key}: {{}}")
        elif isinstance(v, list):
            _toon_list(key, v, depth, lines)
        else:
            lines.append(f"{pad}{key}: {_toon_scalar(v)}")


def _toon_list(key: str, items: list, depth: int, lines: list[str]) -> None:
    pad = "  " * depth
    inner = "  " * (depth + 1)

    if not items:                                          # §9 empty array
        lines.append(f"{pad}{key}: []")
        return

    if all(isinstance(i, dict) for i in items):            # §9 tabular form
        fields: list[str] = []
        for i in items:
            fields.extend(f for f in i if f not in fields)
        if all(_is_scalar(i.get(f)) for i in items for f in fields):
            lines.append(f"{pad}{key}[{len(items)}]{{{','.join(fields)}}}:")
            for i in items:
                lines.append(inner + ",".join(_toon_scalar(i.get(f)) for f in fields))
            return

    if all(_is_scalar(i) for i in items):
        # Prose elements (spaces or the delimiter) take the multi-line form:
        # both are valid §9, but inline would quote and comma-join whole
        # sentences onto one line. Token lists stay inline.
        if any(isinstance(i, str) and ("," in i or " " in i) for i in items):
            lines.append(f"{pad}{key}[{len(items)}]:")
            for i in items:
                lines.append(inner + _toon_scalar(i, bare=True))
        else:
            lines.append(f"{pad}{key}[{len(items)}]: "
                         + ",".join(_toon_scalar(i) for i in items))
        return

    lines.append(f"{pad}{key}[{len(items)}]:")             # mixed: one per line
    for i in items:
        lines.append(inner + _toon_scalar(json.dumps(i, ensure_ascii=False)))

# =====================================================================
# AXI plumbing  (§6 structured errors, §10 identity)
# =====================================================================

DESCRIPTION = "Append-only JSONL record store with a runtime YAML contract"
# The name this was invoked as, so suggested commands are runnable as typed.
# Set in main(); importing this module must not pick up the importer's argv[0].
PROG = "datafile.py"
DETAIL_TRUNCATE = 800     # §3
CELL_TRUNCATE = 80
DEFAULT_LIMIT = 100       # §2: high enough to cover common cases in one call


class AxiError(Exception):
    def __init__(self, message: str, code: str, help: list[str] | None = None,
                 exit_code: int = 1, extra: dict | None = None):
        super().__init__(message)
        self.message, self.code = message, code
        self.help, self.exit_code, self.extra = help or [], exit_code, extra or {}


def emit(payload: dict) -> None:
    """All structured output goes to stdout (§6).

    Suggestions are authored against `datafile.py`; rewrite them to whatever name
    this was invoked as, so §9's "every suggestion is a complete command" holds
    for a globally installed alias too."""
    if PROG != "datafile.py" and isinstance(payload.get("help"), list):
        payload["help"] = [h.replace("datafile.py", PROG) for h in payload["help"]]
    text = toon(payload)
    if text:
        print(text)


def collapse_home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def fail(err: AxiError) -> int:
    out = {"error": err.message, "code": err.code}
    out.update(err.extra)
    if err.help:
        out["help"] = err.help
    emit(out)
    return err.exit_code


def truncate(value, limit: int):
    """§3: never omit, always preview. Returns (value, full_length_or_None)."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "...", len(value)
    return value, None


# ---------------------------------------------------------------- argparse

class _Usage(Exception):
    def __init__(self, message: str):
        self.message = message


class AxiParser(argparse.ArgumentParser):
    """§6: never print argparse's own text to stderr; raise for structuring."""

    def __init__(self, *a, **kw):
        # Prefix matching would silently accept `--ful` for `--full`, which is
        # the exact "misread flag" hazard §6 forbids. Unknown means unknown.
        kw.setdefault("allow_abbrev", False)
        super().__init__(*a, **kw)

    def error(self, message):
        raise _Usage(message)

    def exit(self, status=0, message=None):
        if status:
            raise _Usage(message or "usage error")
        raise SystemExit(0)


def _flags_of(parser: argparse.ArgumentParser) -> list[str]:
    out: list[str] = []
    for a in parser._actions:
        out.extend(s for s in a.option_strings if s.startswith("--"))
    return sorted(set(out))


# ------------------------------------------------------- store discovery (§8)

CONTRACT_PATTERNS = ("schema-{}.yaml", "schema-{}.yml",
                     "{}.schema.yaml", "{}.schema.yml", "{}.yaml", "{}.yml")


def find_contract(store_path: str) -> str | None:
    d = os.path.dirname(os.path.abspath(store_path))
    stem = os.path.basename(store_path)
    stem = stem[:-6] if stem.endswith(".jsonl") else stem
    for pat in CONTRACT_PATTERNS:
        cand = os.path.join(d, pat.format(stem))
        if os.path.isfile(cand):
            return os.path.relpath(cand, os.getcwd())
    return None


DISCOVER_DEPTH = 3          # session-start latency budget (§7)
DISCOVER_CAP = 20
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "target",
             "dist", "build", "vendor", "site-packages"}


def discover_stores(directory: str = ".", depth: int = DISCOVER_DEPTH,
                    cap: int | None = DISCOVER_CAP) -> tuple[list[dict], bool]:
    """Walk for .jsonl stores. Returns (rows, truncated). Bounded because this
    runs at every session start - an unbounded walk would tax every session."""
    root = os.path.abspath(directory)
    found: list[str] = []
    truncated = False

    for cur, dirs, files in os.walk(root):
        rel_depth = 0 if cur == root else cur[len(root) + 1:].count(os.sep) + 1
        if rel_depth >= depth:
            dirs[:] = []
        else:
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            if cap is not None and len(found) >= cap:
                truncated = True
                break
            found.append(os.path.join(cur, name))
        if truncated:
            break

    rows = []
    for path in found:
        contract = find_contract(path)
        rel = os.path.relpath(path, root)
        row = {"file": rel, "contract": contract, "records": 0}
        if contract:
            try:
                model, key = load_contract(contract)
                row["records"] = len(Store(path, model, key=key).keys())
            except (ContractError, OSError):
                row["contract"] = f"{contract} (invalid)"
        rows.append(row)
    return rows, truncated


# =====================================================================
# Commands
# =====================================================================

def cmd_home() -> int:
    """§8 content first, §10 identity header."""
    out = {"bin": collapse_home(os.path.abspath(__file__)),
           "description": DESCRIPTION,
           "version": VERSION}
    stores, truncated = discover_stores(".")
    if not stores:                                          # §5 definitive empty
        out["stores"] = f"0 jsonl stores found in {collapse_home(os.getcwd())}"
        out["help"] = [
            "Write a contract to schema-<name>.yaml, then run "
            "`datafile.py -f <name>.jsonl -c schema-<name>.yaml put '{...}'`",
            "Run `datafile.py --help` for the full command list",
        ]
        emit(out)
        return 0
    out["stores"] = stores
    first = stores[0]
    ref = f"-f {first['file']} -c {first['contract']}"
    out["help"] = [                                          # §9 disclosure
        f"Run `datafile.py {ref} list` to see records",
        f"Run `datafile.py {ref} get <id>` for one record",
        f"Run `datafile.py {ref} validate` to check every line against its contract",
    ]
    if truncated:                                            # §9 reveal the cap
        out["help"].insert(0, f"Showing the first {DISCOVER_CAP} stores; run "
                              f"`datafile.py stores --all` to see every one")
    emit(out)
    return 0


def cmd_stores(args) -> int:
    """Explicit discovery: the escape hatch behind the home view's cap."""
    depth = 99 if args.all else args.depth
    cap = None if args.all else DISCOVER_CAP
    rows, truncated = discover_stores(".", depth=depth, cap=cap)
    if not rows:
        emit({"stores": f"0 jsonl stores found under {collapse_home(os.getcwd())} "
                        f"within {depth} levels",
              "help": ["Run `datafile.py stores --all` to search without a depth limit"]})
        return 0
    out = {"count": len(rows), "stores": rows}
    if truncated:
        out["help"] = ["Run `datafile.py stores --all` to see every store"]
    emit(out)
    return 0


def _open(args) -> tuple[Store, type[BaseModel], str]:
    if not args.file:
        raise AxiError("--file is required", "USAGE_ERROR",
                       ["Run `datafile.py` to list stores in this directory"], 2)
    contract = args.contract or find_contract(args.file)
    if not contract:
        raise AxiError(
            f"no contract given and none found for {args.file}", "USAGE_ERROR",
            [f"Run `datafile.py -f {args.file} -c <contract.yaml> {args.cmd}`",
             f"Or create {os.path.basename(args.file)[:-6]}.yaml next to it"], 2)
    try:
        model, key = load_contract(contract)
    except ContractError as e:
        raise AxiError(str(e), "CONTRACT_ERROR",
                       ["Run `datafile.py -c <contract> schema` to inspect a contract"],
                       2) from None
    return Store(args.file, model, key=key), model, key


def _default_fields(model, key: str) -> list[str]:
    """§2: smallest schema that lets an agent decide what to do next.
    Required scalars first - optional fields are usually null, and a column of
    nulls costs tokens without helping the agent decide anything."""
    def usable(name, info):
        t = str(info.annotation)
        return name != key and "list[" not in t and "dict[" not in t
    req = [f for f, i in model.model_fields.items() if usable(f, i) and i.is_required()]
    opt = [f for f, i in model.model_fields.items() if usable(f, i) and not i.is_required()]
    return [key, *(req + opt)[:3]]


def cmd_list(args) -> int:
    s, model, key = _open(args)

    if args.fields:                          # a usage error should not read the log
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        unknown = [f for f in fields if f not in model.model_fields]
        if unknown:
            raise AxiError(
                f"unknown field(s) {unknown} for this contract", "USAGE_ERROR",
                [f"Valid fields: {', '.join(model.model_fields)}"], 2)
    else:
        fields = _default_fields(model, key)

    shown, total, bad = s.page(args.limit)

    if not total:                                          # §5
        out = {"records": f"0 records in {args.file}"}
        if bad:
            out["bad"] = bad
        out["help"] = [f"Run `datafile.py -f {args.file} put '{{...}}'` to add one"]
        if bad:
            out["help"].append(f"Run `datafile.py -f {args.file} validate` to see "
                               f"{bad} unreadable line(s)")
        emit(out)
        return 0

    rows, clipped = [], False
    for r in shown:
        d = r.model_dump(mode="json")
        row = {}
        for f in fields:
            v = d.get(f)
            if not _is_scalar(v):
                v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            v, full = truncate(v, CELL_TRUNCATE)
            clipped = clipped or full is not None
            row[f] = v
        rows.append(row)

    out: dict = {"count": f"{len(shown)} of {total} total"}   # §4 aggregate
    if bad:
        out["bad"] = bad
    out[os.path.basename(args.file)[:-6] or "records"] = rows
    help_lines = [f"Run `datafile.py -f {args.file} get <id>` for one record with all fields"]
    if len(shown) == args.limit and args.limit < total:       # §9 reveal truncation
        help_lines.append(f"Run `datafile.py -f {args.file} list --limit {total}` "
                          f"for all {total} records")
    if clipped:
        help_lines.append(f"Run `datafile.py -f {args.file} get <id> --full` "
                          f"for untruncated values")
    if bad:
        help_lines.append(f"Run `datafile.py -f {args.file} validate` to see "
                          f"{bad} unreadable line(s)")
    out["help"] = help_lines
    emit(out)
    return 0


def cmd_get(args) -> int:
    s, _model, key = _open(args)
    rec = s.get(args.id)
    if rec is None:
        raise AxiError(f"no record with {key} {args.id!r} in {args.file}", "NOT_FOUND",
                       [f"Run `datafile.py -f {args.file} list` to see available ids"], 1)
    d = rec.model_dump(mode="json")
    truncated = []
    if not args.full:
        for k, v in list(d.items()):
            v2, full = truncate(v, DETAIL_TRUNCATE)
            if full is not None:
                d[k], _ = v2, truncated.append({"field": k, "shown": DETAIL_TRUNCATE,
                                                "total": full})
    out: dict = {"record": d}
    if truncated:                                            # §3
        out["truncated"] = truncated
        out["help"] = [f"Run `datafile.py -f {args.file} get {args.id} --full` "
                       f"to see complete values"]
    emit(out)
    return 0


def cmd_put(args) -> int:
    s, model, key = _open(args)
    written, rejected = [], []
    for rec in _records(args):
        try:
            obj = model.model_validate(rec)
        except ValidationError as e:
            rejected.append({"id": str(rec.get(key, "<no key>")),
                             "problem": _fmt(e)})
            continue
        rid = getattr(obj, key)
        existing = s.get(rid)
        if existing is not None and existing.model_dump() == obj.model_dump():
            written.append({"id": str(rid), "op": "unchanged"})   # §6 idempotent
            continue
        s.put(obj)
        written.append({"id": str(rid), "op": "updated" if existing else "created"})

    out: dict = {}
    if rejected:
        out["error"] = f"{len(rejected)} record(s) rejected by contract"
        out["code"] = "CONTRACT_VIOLATION"
    out["written"], out["rejected_count"] = len(written), len(rejected)
    if written:
        out["records"] = written
    if rejected:
        out["rejected"] = rejected
        out["help"] = [f"Run `datafile.py -f {args.file} schema` to see the contract",
                       f"Run `datafile.py -f {args.file} list` to see current records"]
    emit(out)
    return 2 if rejected else 0


def cmd_delete(args) -> int:
    s, _model, _key = _open(args)
    if s.get(args.id) is None:                               # §6 idempotent no-op
        emit({"delete": f"{args.id} already absent (no-op)"})
        return 0
    s.delete(args.id)
    emit({"delete": f"{args.id} removed",
          "help": [f"Run `datafile.py -f {args.file} compact` to reclaim its space"]})
    return 0


def cmd_keys(args) -> int:
    s, _model, _key = _open(args)
    ks = s.keys()
    if not ks:                                               # §5
        emit({"keys": f"0 records in {args.file}",
              "help": [f"Run `datafile.py -f {args.file} put '{{...}}'` to add one"]})
        return 0
    shown = ks[:args.limit]
    out: dict = {"count": f"{len(shown)} of {len(ks)} total", "keys": shown}
    if len(shown) < len(ks):                                 # §9 reveal truncation
        out["help"] = [f"Run `datafile.py -f {args.file} keys --limit {len(ks)}` "
                       f"for all {len(ks)} ids"]
    emit(out)
    return 0


def cmd_validate(args) -> int:
    s, _model, _key = _open(args)
    alive, bad = s.load()
    out: dict = {"valid": len(alive), "bad": len(bad)}
    if bad:
        out["issues"] = [{"line": b.line if b.line is not None else "-",
                          "byte": b.offset, "reason": b.reason} for b in bad]
        out["help"] = [f"Run `datafile.py -f {args.file} repair` to quarantine bad "
                       f"lines and compact",
                       f"Run `datafile.py -f {args.file} schema` to see what the "
                       f"contract requires"]
    emit(out)
    return 1 if (bad and args.strict) else 0


def cmd_repair(args) -> int:
    s, _model, _key = _open(args)
    kept, dropped = s.repair()
    out = {"kept": kept, "quarantined": dropped}
    if dropped:
        out["quarantine"] = f"{args.file}.quarantine"
        out["help"] = [f"Run `datafile.py -f {args.file} validate` to confirm the "
                       f"store is clean"]
    emit(out)
    return 0


def cmd_compact(args) -> int:
    s, _model, _key = _open(args)
    before = os.path.getsize(args.file) if os.path.exists(args.file) else 0
    kept, _ = s.compact()
    after = os.path.getsize(args.file) if os.path.exists(args.file) else 0
    emit({"kept": kept, "reclaimed_bytes": before - after})
    return 0


SEGMENT_CONTRACT = """\
name: Segment
key: segment
extra: forbid
fields:
  segment:   {type: str}
  records:   {type: int, ge: 0}
  bytes:     {type: int, ge: 0}
  gz_bytes:  {type: int, ge: 0}
  sha256:    {type: str}
  rolled_at: {type: datetime}
"""

SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2,
              "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3}


def parse_size(text: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", str(text).strip(), re.I)
    if not m or m.group(2).lower() not in SIZE_UNITS:
        raise AxiError(f"cannot read {text!r} as a size", "USAGE_ERROR",
                       ["Use a byte count or a suffix: 50MB, 512k, 2GB"], 2)
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2).lower()])


def cmd_roll(args) -> int:
    s, _, _ = _open(args)
    threshold = parse_size(args.if_larger_than)
    size = os.path.getsize(args.file) if os.path.exists(args.file) else 0
    if size < max(threshold, 1):                             # §6 idempotent no-op
        emit({"roll": f"{args.file} is {size:,} bytes, under "
                      f"{args.if_larger_than} (no-op)"})
        return 0

    adir = args.archive_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.file)) or ".", "archive")

    if not args.force:
        dups, tombs = s.append_only_violations()
        if dups or tombs:
            raise AxiError(
                f"{args.file} is not append-only: {dups} updated key(s) and "
                f"{tombs} tombstone(s)", "UNSAFE_ROLL",
                ["Segments are never folded back in, so a record whose latest "
                 "version lands in one becomes unreachable",
                 f"Run `datafile.py -f {args.file} compact` first, then roll",
                 f"Run `datafile.py -f {args.file} roll --force` to archive anyway"],
                2, {"updated_keys": dups, "tombstones": tombs})

    try:
        row = s.roll(adir, min_bytes=threshold)
    except OSError as e:
        raise AxiError(f"cannot archive into {adir}: {e}", "IO_ERROR",
                       ["The archive directory must be on the same filesystem "
                        "as the store"], 1) from None

    contract = os.path.join(adir, "schema-manifest.yaml")
    if not os.path.exists(contract):        # makes the manifest self-describing
        Store._atomic_write(contract, SEGMENT_CONTRACT)

    emit({"rolled": row["segment"], "records": row["records"],
          "bytes": row["bytes"], "gz_bytes": row["gz_bytes"],
          "help": [f"Run `datafile.py -f {os.path.join(adir, 'manifest.jsonl')} "
                   f"list` to see every segment",
                   f"Run `gunzip -c {os.path.join(adir, row['segment'])}` to read "
                   f"this one"]})
    return 0


def cmd_schema(args) -> int:
    _s, model, key = _open(args)
    if args.json_schema:
        emit({"schema": json.dumps(model.model_json_schema(), indent=None)})
        return 0
    import dataclasses
    fields = []
    for name, f in model.model_fields.items():
        cons = []
        for m in f.metadata:            # annotated_types uses slotted dataclasses
            if dataclasses.is_dataclass(m):
                d = {fl.name: getattr(m, fl.name) for fl in dataclasses.fields(m)}
            else:
                d = getattr(m, "__dict__", None)
            if d:
                cons.extend(f"{k}={v}" for k, v in d.items() if v is not None)
            else:
                cons.append(type(m).__name__)
        fields.append({"name": name,
                       "type": _type_name(f.annotation),
                       "required": f.is_required(),
                       "rules": "; ".join(cons) or None})
    emit({"contract": args.contract or find_contract(args.file),
          "model": model.__name__, "key": key,
          "extra": model.model_config.get("extra", "ignore"),
          "fields": fields})
    return 0


def _type_name(ann) -> str:
    txt = str(ann).replace("typing.", "").replace("<class '", "").replace("'>", "")
    txt = txt.replace("pydantic.networks.", "").replace("datetime.", "")
    return txt.replace("Optional[", "").rstrip("]") if txt.startswith("Optional[") else txt


def _fmt(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in x['loc']) or '<root>'}: {x['msg']}"
        for x in e.errors())


def _records(args) -> list[dict]:
    """Collect records from a JSON argument, stdin JSONL, and/or --set pairs."""
    overrides = {}
    for pair in args.set:
        if "=" not in pair:
            raise AxiError(f"--set expects KEY=VALUE, got {pair!r}", "USAGE_ERROR",
                           ["Run `datafile.py put --set id=abc --set name=Ada`"], 2)
        k, _, v = pair.partition("=")
        try:
            overrides[k] = json.loads(v)      # 36 -> int; bare words stay strings
        except json.JSONDecodeError:
            overrides[k] = v

    if args.record == "-":
        out = []
        for n, line in enumerate(sys.stdin, 1):
            if line.strip():
                try:
                    out.append({**json.loads(line), **overrides})
                except json.JSONDecodeError as e:
                    raise AxiError(f"stdin line {n}: invalid json: {e.msg}",
                                   "USAGE_ERROR",
                                   ["Each stdin line must be one JSON object"], 2) from None
        return out
    if args.record:
        try:
            return [{**json.loads(args.record), **overrides}]
        except json.JSONDecodeError as e:
            raise AxiError(f"invalid json: {e.msg}", "USAGE_ERROR",
                           ["Run `datafile.py put '{\"id\":\"a1\",...}'`",
                            "Or build the record with --set key=value"], 2) from None
    if not overrides:
        raise AxiError("put needs a JSON record, '-' for stdin, or --set",
                       "USAGE_ERROR",
                       ["Run `datafile.py put '{\"id\":\"a1\"}'`",
                        "Run `datafile.py put --set id=a1 --set name=Ada`",
                        "Run `cat data.jsonl | datafile.py put -`"], 2)
    return [overrides]


# ------------------------------------------------------------ setup (§7)

TOOL_NAME = "jsonl-store"           # stable identity: plugin file, skill, alias
# A managed hook is recognised by any of these appearing in its command, so an
# entry installed as `uv run /path/datafile.py` is still found after the script is
# symlinked onto PATH as `jsonl-store`, and vice versa.
MARKERS = ("datafile.py", TOOL_NAME)
HOOK_TIMEOUT = 10
OPENCODE_PREFIX = "axi managed opencode plugin:"


def _hook_targets(scope: str) -> dict:
    home = os.path.expanduser("~")
    root = home if scope == "user" else os.getcwd()
    return {
        "claude": os.path.join(root, ".claude", "settings.json"),
        "codex": os.path.join(root, ".codex", "hooks.json"),
        # Repo-level Codex hooks still require the USER-level feature flag.
        "codex_config": os.path.join(home, ".codex", "config.toml"),
        "opencode": os.path.join(root, ".opencode", "plugins") if scope == "project"
        else os.path.join(home, ".config", "opencode", "plugins"),
    }


def _is_managed(hook: dict) -> bool:
    cmd = hook.get("command") or ""
    return any(m in cmd for m in MARKERS)


def _hook_update(settings: dict, command: str) -> tuple[dict, str]:
    """Claude Code and Codex share the hooks.SessionStart group shape."""
    updated = json.loads(json.dumps(settings))
    changed = False
    hooks = updated.setdefault("hooks", {})

    legacy = hooks.get("session_start")          # migrate the flat legacy form
    if isinstance(legacy, list):
        kept = [h for h in legacy if not _is_managed(h)]
        if len(kept) != len(legacy):
            changed = True
            if kept:
                hooks["session_start"] = kept
            else:
                del hooks["session_start"]

    if not isinstance(hooks.get("SessionStart"), list):
        hooks["SessionStart"] = []
        changed = True

    for group in hooks["SessionStart"]:
        for hook in group.get("hooks", []) or []:
            if not _is_managed(hook):
                continue
            if (hook.get("command") == command and hook.get("type") == "command"
                    and hook.get("timeout") == HOOK_TIMEOUT and not changed):
                return settings, "unchanged (no-op)"
            hook.update(type="command", command=command, timeout=HOOK_TIMEOUT)
            return updated, "repaired"

    hooks["SessionStart"].append({"matcher": "", "hooks": [
        {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]})
    return updated, "installed"


def _hook_removal(settings: dict) -> tuple[dict, bool]:
    updated = json.loads(json.dumps(settings))
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return settings, False
    changed = False

    if isinstance(hooks.get("session_start"), list):
        kept = [h for h in hooks["session_start"] if not _is_managed(h)]
        if len(kept) != len(hooks["session_start"]):
            changed = True
            hooks["session_start"] = kept if kept else None
            if kept is None or not kept:
                hooks.pop("session_start", None)

    groups = hooks.get("SessionStart")
    if isinstance(groups, list):
        remaining = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) or [] if not _is_managed(h)]
            if len(inner) != len(group.get("hooks", []) or []):
                changed = True
                if not inner:
                    continue
                group = {**group, "hooks": inner}
            remaining.append(group)
        if changed:
            if remaining:
                hooks["SessionStart"] = remaining
            else:
                hooks.pop("SessionStart", None)
    if changed and not hooks:
        updated.pop("hooks", None)
    return (updated, True) if changed else (settings, False)


def codex_config_update(content: str) -> tuple[str, bool]:
    """Ensure [features] hooks = true, preserving everything else."""
    nl = "\r\n" if "\r\n" in content else "\n"
    if not content.strip():
        return f"[features]{nl}hooks = true{nl}", True

    lines = content.split("\n")
    lines = [ln.rstrip("\r") for ln in lines]
    out = list(lines)
    in_features = saw_features = False

    for i, line in enumerate(out):
        m = re.match(r"^\s*(\[{1,2})([^\]]+)(\]{1,2})\s*(?:#.*)?$", line)
        if m:
            if not ((m.group(1) == "[" and m.group(3) == "]")
                    or (m.group(1) == "[[" and m.group(3) == "]]")):
                continue
            if in_features:                       # leaving [features] unset
                at = i                            # step back over trailing blanks
                while at > 0 and not out[at - 1].strip():
                    at -= 1
                out.insert(at, "hooks = true")
                return nl.join(out), True
            in_features = m.group(2).strip() == "features"
            saw_features = saw_features or in_features
            continue
        if not in_features:
            continue
        flag = re.match(r"^\s*hooks\s*=\s*(true|false)\s*(?:#.*)?$", line)
        if not flag:
            continue
        if flag.group(1) == "true":
            return content, False
        out[i] = line.replace("false", "true", 1)
        return nl.join(out), True

    if saw_features:
        suffix = "" if content.endswith(nl) else nl
        return f"{content}{suffix}hooks = true{nl}", True
    sep = nl if content.endswith(nl) else nl + nl
    return f"{content}{sep}[features]{nl}hooks = true{nl}", True


def _opencode_plugin(argv: list[str]) -> str:
    """OpenCode plugin that injects the home view as ambient system context."""
    return f"""// {OPENCODE_PREFIX} {TOOL_NAME}
// Generated by datafile.py. Safe to edit only if you remove the marker above.
import {{ spawn }} from "node:child_process";

const argv = {json.dumps(argv)};
const ambientHeader = {json.dumps(f"## AXI ambient context: {TOOL_NAME}")};
const timeoutMs = {HOOK_TIMEOUT * 1000};

function runHomeView(cwd) {{
  return new Promise((resolve) => {{
    const child = spawn(argv[0], argv.slice(1), {{
      cwd: cwd || process.cwd(), env: process.env, shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    }});
    let stdout = "", stderr = "", settled = false;
    const timer = setTimeout(() => {{
      if (settled) return;
      settled = true; child.kill("SIGTERM");
      resolve("error: {TOOL_NAME} ambient context timed out after " + timeoutMs + "ms");
    }}, timeoutMs);
    child.stdout?.setEncoding("utf-8");
    child.stderr?.setEncoding("utf-8");
    child.stdout?.on("data", (c) => {{ stdout += c; }});
    child.stderr?.on("data", (c) => {{ stderr += c; }});
    child.on("error", (e) => {{
      if (settled) return;
      settled = true; clearTimeout(timer);
      resolve("error: {TOOL_NAME} ambient context failed: " + e.message);
    }});
    child.on("close", (code) => {{
      if (settled) return;
      settled = true; clearTimeout(timer);
      if (code === 0) return resolve(stdout.trim());
      resolve("error: {TOOL_NAME} ambient context failed: "
        + (stderr || stdout || "exit " + code).trim());
    }});
  }});
}}

export const jsonlStoreAmbientContext = async ({{ directory }}) => {{
  const cache = new Map();
  return {{
    "experimental.chat.system.transform": async (input, output) => {{
      const id = input.sessionID ?? "__global__";
      let view = cache.get(id);
      if (view === undefined) {{ view = await runHomeView(directory); cache.set(id, view); }}
      if (view.length === 0) return;
      output.system.push(ambientHeader + "\\n" + view);
    }},
  }};
}};
"""


def _path_alias(me: str) -> str | None:
    """Any executable on PATH that resolves to this script (§7 portable commands)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.access(p, os.X_OK) and os.path.realpath(p) == me:
                    return n
            except OSError:
                continue
    return None


def _hook_argv(uv: str, me: str) -> list[str]:
    alias = _path_alias(os.path.realpath(me))
    if alias and os.access(me, os.X_OK):
        with open(me, encoding="utf-8") as f:
            if f.readline().startswith("#!"):
                return [alias]          # portable: survives the script moving
    return [uv, "run", me]              # fall back to the absolute path


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError) as e:
        raise AxiError(f"cannot read {collapse_home(path)}: {e}", "SETUP_ERROR",
                       ["Fix or remove that file, then re-run `datafile.py setup`"],
                       1) from None


def _read_text(path: str) -> str | None:
    """Whole-file read, or None when the file is not there."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(path: str, text: str) -> None:
    d = os.path.dirname(path)          # empty for a bare filename in cwd
    if d:
        os.makedirs(d, exist_ok=True)
    Store._atomic_write(path, text)


def cmd_setup(args) -> int:
    import shutil
    uv = shutil.which("uv")
    if not uv:
        raise AxiError("uv not found on PATH", "SETUP_ERROR",
                       ["Install uv, then re-run `datafile.py setup`"], 1)
    me = os.path.abspath(__file__)
    argv = _hook_argv(uv, me)
    command = " ".join(argv)
    t = _hook_targets(args.scope)
    apps = ["claude", "codex", "opencode"] if args.app == "all" else [args.app]
    rows, extra = [], {}

    def status_of(app: str) -> str:
        if app == "opencode":
            p = os.path.join(t["opencode"], f"axi-{TOOL_NAME}.js")
            if not os.path.exists(p):
                return "absent"
            with open(p, encoding="utf-8") as f:
                head = f.readline()   # first line only: this runs per app
            return "installed" if OPENCODE_PREFIX in head else "absent"
        data = _read_json(t[app])
        groups = (data.get("hooks") or {}).get("SessionStart") or []
        found = any(_is_managed(h) for g in groups for h in (g.get("hooks") or []))
        return "installed" if found else "absent"

    if args.status:
        for app in apps:
            path = (os.path.join(t["opencode"], f"axi-{TOOL_NAME}.js")
                    if app == "opencode" else t[app])
            rows.append({"app": app, "status": status_of(app),
                         "path": collapse_home(path)})
        flag = codex_config_update(
            _read_text(t["codex_config"]) or "")[1]
        emit({"scope": args.scope, "targets": rows,
              "codex_hooks_feature": "disabled" if flag else "enabled",
              "help": ["Run `datafile.py setup` to install",
                       "Run `datafile.py setup --uninstall` to remove"]})
        return 0

    for app in apps:
        if app == "opencode":
            path = os.path.join(t["opencode"], f"axi-{TOOL_NAME}.js")
            if args.uninstall:
                if status_of("opencode") == "absent":
                    rows.append({"app": app, "status": "already absent (no-op)",
                                 "path": collapse_home(path)})
                else:
                    os.unlink(path)
                    rows.append({"app": app, "status": "removed",
                                 "path": collapse_home(path)})
                continue
            want = _opencode_plugin(argv)
            have = _read_text(path)
            if have == want:
                rows.append({"app": app, "status": "unchanged (no-op)",
                             "path": collapse_home(path)})
            else:
                _write(path, want)
                rows.append({"app": app,
                             "status": "repaired" if have else "installed",
                             "path": collapse_home(path)})
            continue

        path = t[app]
        data = _read_json(path)
        if args.uninstall:
            updated, ch = _hook_removal(data)
            status = "removed" if ch else "already absent (no-op)"
        else:
            updated, status = _hook_update(data, command)
        if not status.endswith("(no-op)"):
            _write(path, json.dumps(updated, indent=2) + "\n")
        rows.append({"app": app, "status": status, "path": collapse_home(path)})

        if app == "codex" and not args.uninstall:
            cfg = t["codex_config"]
            current = _read_text(cfg) or ""
            new, ch = codex_config_update(current)
            if ch:
                _write(cfg, new)
            extra["codex_hooks_feature"] = (
                f"enabled in {collapse_home(cfg)}" if ch
                else f"already enabled in {collapse_home(cfg)}")

    out = {"scope": args.scope, "targets": rows}
    out.update(extra)
    out["help"] = (["Run `datafile.py setup --status` to verify"] if args.uninstall else
                   ["Run `datafile.py setup --status` to verify",
                    "Run `datafile.py setup --uninstall` to remove",
                    "Run `datafile.py skill` to generate the installable skill"])
    emit(out)
    return 0


# --------------------------------------------------- installable skill (§7)

SKILL_NAME = "jsonl-store"
SKILL_PATH = os.path.join(".agents", "skills", SKILL_NAME, "SKILL.md")


# Where each agent discovers skills. Both follow the Agent Skills standard
# (agentskills.io): a directory per skill containing SKILL.md.
#   claude -> ~/.claude/skills/<name>/SKILL.md
#   pi     -> ~/.agents/skills/<name>/SKILL.md, and .agents/skills/ walking up
#             from cwd. pi does NOT read ~/.claude/skills unless you add it to
#             its `skills` setting, so it needs its own copy.
SKILL_APPS = {"claude": (".claude", "skills"), "pi": (".agents", "skills")}


def skill_install_path(scope: str, app: str) -> str:
    root = os.path.expanduser("~") if scope == "user" else os.getcwd()
    return os.path.join(root, *SKILL_APPS[app], SKILL_NAME, "SKILL.md")
SKILL_TRIGGER = (
    "Read or write append-only JSONL data files whose records are validated "
    "against a YAML-defined contract. Use when adding, updating, deleting, "
    "listing, or repairing rows in a .jsonl store, when a JSONL file has "
    "malformed lines to recover, or when defining a schema for one."
)


def render_skill() -> str:
    """Generated from the parser and the contract loader, so the skill cannot
    drift from the CLI. Carries no live state - a skill is static (§7)."""
    parser, subs = build_parser()
    lines = [
        "---",
        f"name: {SKILL_NAME}",
        "description: >-",
    ]
    words, row = SKILL_TRIGGER.split(), ""
    for w in words:
        if len(row) + len(w) + 1 > 76:
            lines.append(f"  {row}")
            row = w
        else:
            row = f"{row} {w}".strip()
    lines.append(f"  {row}")
    lines += [
        "---",
        "",
        f"# {SKILL_NAME}",
        "",
        __import__("textwrap").fill(
            f"{DESCRIPTION}. Records are appended to a log, updated by appending"
            " a new version, and deleted with a tombstone; reads fold the log so"
            " the last write wins. Every write is validated against the"
            " contract before it is written.", width=76),
        "",
        "## Invocation",
        "",
        "`datafile.py` is a single self-contained script. Run it with `uv`, which",
        "installs its declared dependencies automatically:",
        "",
        "```sh",
        "uv run /path/to/datafile.py --file data.jsonl --contract schema.yaml list",
        "```",
        "",
        "Install this skill with `datafile.py skill --install`. It is written to",
        "each agent's skills directory; add `--scope project` to install it for",
        "one repository instead of your account.",
        "",
        "Examples below write `datafile.py` for brevity; prefix them with",
        "`uv run /path/to/`. Running it with no arguments prints the stores in",
        "the current directory and is the cheapest way to orient.",
        "",
        "## Commands",
        "",
        "| command | purpose | flags |",
        "| --- | --- | --- |",
    ]
    for name, sp in subs.items():
        flags = []
        for a in sp._actions:
            opts = [o for o in a.option_strings if o.startswith("--")]
            if not opts or opts[0] == "--help":
                continue
            f = opts[0]
            if a.default not in (None, False) and a.default != []:
                f += f" (default: {a.default})"
            flags.append(f)
        purpose = next((c.help for c in parser._subparsers._group_actions[0]._choices_actions
                        if c.dest == name), "")
        lines.append(f"| `{name}` | {purpose} | {', '.join(flags) or '-'} |")

    lines += ["", "## Examples", ""]
    for name, sp in subs.items():
        ex = (sp.epilog or "").replace("examples:", "").strip()
        if ex:
            lines.append(f"**{name}**")
            lines.append("")
            lines.append("```sh")
            lines += [ln.strip() for ln in ex.splitlines() if ln.strip()]
            lines.append("```")
            lines.append("")

    lines += [
        "## Contract format",
        "",
        "A contract is a YAML file describing one record type.",
        "",
        "```yaml",
        "name: User",
        "key: id                 # which field is the primary key",
        "extra: forbid           # forbid | ignore | allow",
        "fields:",
        "  id:    {type: str, pattern: '^[a-z][a-z0-9-]{1,31}$'}",
        "  age:   {type: int, ge: 0, le: 150}",
        "  email: {type: email, required: false}",
        "  role:  {type: enum, values: [admin, member, guest], default: member}",
        "  tags:  {type: list, items: str, default: []}",
        "  note:  str            # shorthand for {type: str}",
        "```",
        "",
        f"Types: {', '.join(sorted(set(SCALARS) | {'enum', 'list', 'dict'}))}.",
        "",
        f"Constraints: {', '.join(sorted(FIELD_KWARGS))}.",
        "",
        "`required: false` widens the type to allow null. Supplying `default:`",
        "makes a field optional while keeping its type non-null. Unknown types,",
        "constraints, and keys are rejected when the contract loads.",
        "",
        "## Output and exit codes",
        "",
        "Output is TOON on stdout, including errors, which carry a `code:` and",
        "`help:` suggestions. Nothing is written to stderr.",
        "",
        "| exit | meaning |",
        "| --- | --- |",
        "| 0 | success, including idempotent no-ops |",
        "| 1 | the request could not be satisfied (not found, setup failure) |",
        "| 2 | usage error or a record rejected by the contract |",
        "",
        "`validate` exits 0 and reports `bad: N`; pass `--strict` to exit 1 when",
        "any line is unreadable, which is the form to use in CI.",
        "",
    ]
    return "\n".join(lines)


def cmd_skill(args) -> int:
    if args.out and args.install:
        raise AxiError("--out and --install choose different destinations",
                       "USAGE_ERROR",
                       ["Run `datafile.py skill --install` to install for an agent",
                        "Run `datafile.py skill --out <path>` to write a copy"], 2)
    apps = list(SKILL_APPS) if args.app == "all" else [args.app]
    want = render_skill()

    if args.install or args.uninstall:
        rows = []
        for app in apps:
            path = skill_install_path(args.scope, app)
            rows.append({"app": app, "status": _skill_place(path, want, args),
                         "path": collapse_home(os.path.dirname(path))})
        out = {"scope": args.scope, "targets": rows}
        out["help"] = (["Run `datafile.py skill --install` to reinstall"]
                       if args.uninstall else
                       [f"Restart your agent session to pick up {SKILL_NAME}",
                        "Run `datafile.py skill --uninstall` to remove it"])
        emit(out)
        return 0

    out = args.out or SKILL_PATH
    have = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            have = f.read()

    if args.check:
        if have is None:
            raise AxiError(f"{out} does not exist", "SKILL_STALE",
                           ["Run `datafile.py skill` to generate it"], 1)
        if have != want:
            raise AxiError(f"{out} is out of date", "SKILL_STALE",
                           ["Run `datafile.py skill` to regenerate it"], 1)
        emit({"skill": f"{out} is up to date"})
        return 0

    if have == want:
        emit({"skill": f"{collapse_home(out)} unchanged (no-op)"})
        return 0
    _write(out, want)
    emit({"skill": f"{'regenerated' if have else 'generated'} {collapse_home(out)}",
          "lines": len(want.splitlines()),
          "help": ["Run `datafile.py skill --install` to install it for your agents",
                   "Run `datafile.py skill --check` in CI to fail on a stale skill"]})
    return 0


def _skill_place(path: str, want: str, args) -> str:
    """Install, repair, or remove one agent's copy. Returns its status."""
    if args.uninstall:
        if not os.path.exists(path):
            return "already absent (no-op)"
        if f"name: {SKILL_NAME}" not in (_read_text(path) or ""):
            raise AxiError(f"{collapse_home(path)} was not generated by this tool",
                           "SKILL_FOREIGN", ["Remove it by hand if you are sure"], 1)
        os.unlink(path)
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(path))
        return "removed"

    have = _read_text(path)
    if have == want:
        return "unchanged (no-op)"
    _write(path, want)
    return "regenerated" if have else "installed"


# ------------------------------------------------------- pi package (§7)

PI_PACKAGE_DIR = "pi-" + TOOL_NAME
PI_PACKAGE_VERSION = VERSION


def _pi_extension(argv: list[str]) -> str:
    """A pi extension that injects the home view as ambient context.

    pi has no command-hook mechanism like Claude Code or Codex; extensions are
    TypeScript modules. `session_start` captures the view once, and
    `before_agent_start` appends it to the system prompt each turn.
    """
    return f"""// Generated by {TOOL_NAME}. Regenerate with `datafile.py pi-package`.
import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";
import {{ spawn }} from "node:child_process";

// Tried in order. The bare name works when {TOOL_NAME} is on PATH (the
// portable case, so this package works on someone else's machine); the second
// entry is the absolute path baked in when the package was generated.
const CANDIDATES: string[][] = {json.dumps([[TOOL_NAME], argv])};
const TIMEOUT_MS = {HOOK_TIMEOUT * 1000};
const HEADER = {json.dumps(f"## AXI ambient context: {TOOL_NAME}")};

function tryOne(argv: string[], cwd: string): Promise<string | null> {{
  return new Promise((resolve) => {{
    const child = spawn(argv[0], argv.slice(1), {{
      cwd,
      env: process.env,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    }});
    let out = "";
    let settled = false;
    const done = (value: string | null) => {{
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    }};
    const timer = setTimeout(() => {{
      child.kill("SIGTERM");
      done("");
    }}, TIMEOUT_MS);   // timed out: give up rather than try the next candidate
    child.stdout?.setEncoding("utf-8");
    child.stdout?.on("data", (chunk) => {{
      out += chunk;
    }});
    // null means "this candidate is not usable, try the next one".
    child.on("error", () => done(null));
    child.on("close", (code) => done(code === 0 ? out.trim() : null));
  }});
}}

async function homeView(cwd: string): Promise<string> {{
  for (const argv of CANDIDATES) {{
    const result = await tryOne(argv, cwd);
    // Ambient context is best-effort: a failure must not corrupt the prompt.
    if (result !== null) return result;
  }}
  return "";
}}

export default function (pi: ExtensionAPI) {{
  let ambient = "";

  pi.on("session_start", async (_event, ctx) => {{
    ambient = await homeView(ctx.cwd);
  }});

  pi.on("before_agent_start", async (event) => {{
    if (!ambient) return;
    return {{ systemPrompt: `${{event.systemPrompt}}\\n\\n${{HEADER}}\\n${{ambient}}` }};
  }});
}}
"""


def _pi_manifest() -> str:
    return json.dumps({
        "name": PI_PACKAGE_DIR,
        "version": PI_PACKAGE_VERSION,
        "type": "module",          # the extension is ESM TypeScript
        "description": DESCRIPTION,
        "keywords": ["pi-package", "jsonl", "contract", "axi"],
        "pi": {"extensions": ["./extensions"], "skills": ["./skills"]},
        "peerDependencies": {"@earendil-works/pi-coding-agent": "*"},
        "private": True,
    }, indent=2) + "\n"


def _pi_readme(argv: list[str]) -> str:
    # No absolute paths here: the README must be byte-identical wherever it is
    # generated, or `pi-package --check` fails spuriously in CI.
    return f"""# {PI_PACKAGE_DIR}

A [pi package](https://pi.dev) for {TOOL_NAME}: {DESCRIPTION}.

Generated by `datafile.py pi-package`. Do not edit by hand - regenerate instead.
`datafile.py pi-package --check` catches drift, but only on the machine that
generated the package: the absolute fallback path below is baked in at
generation time, so the check fails anywhere else. In CI, check the skill
instead - it carries no machine-specific paths.

## Install

```sh
pi install /path/to/{PI_PACKAGE_DIR}
```

Add `-l` to install into this project's `.pi/settings.json` instead of your
account.

## What it provides

- **skills/{SKILL_NAME}/SKILL.md** - loaded on demand when a task involves a
  `.jsonl` store with a YAML contract.
- **extensions/ambient-context.ts** - captures the store overview at
  `session_start` and appends it to the system prompt, so a session opens
  already knowing which stores exist.

The extension shells out to `{TOOL_NAME}` if it is on `PATH`, otherwise to the
absolute path baked in when this package was generated:

```
{" ".join(argv)}
```

For the package to work on another machine, install `{TOOL_NAME}` on `PATH`
there. Otherwise regenerate the package on that machine.
"""


def _pi_files(argv: list[str]) -> dict[str, str]:
    return {
        os.path.join("package.json"): _pi_manifest(),
        os.path.join("README.md"): _pi_readme(argv),
        os.path.join("extensions", "ambient-context.ts"): _pi_extension(argv),
        os.path.join("skills", SKILL_NAME, "SKILL.md"): render_skill(),
    }


def cmd_pi_package(args) -> int:
    import shutil
    uv = shutil.which("uv")
    if not uv:
        raise AxiError("uv not found on PATH", "SETUP_ERROR",
                       ["Install uv, then re-run `datafile.py pi-package`"], 1)
    root = args.out or PI_PACKAGE_DIR
    files = _pi_files(_hook_argv(uv, os.path.abspath(__file__)))

    stale = []
    for rel, want in files.items():
        path = os.path.join(root, rel)
        have = _read_text(path)
        if have != want:
            stale.append(rel)

    if args.check:
        if stale:
            raise AxiError(f"{collapse_home(root)} is out of date", "PACKAGE_STALE",
                           ["Run `datafile.py pi-package` to regenerate it"],
                           1, {"stale": sorted(stale)})
        emit({"package": f"{collapse_home(root)} is up to date"})
        return 0

    if not stale:
        emit({"package": f"{collapse_home(root)} unchanged (no-op)"})
        return 0
    for rel, want in files.items():
        _write(os.path.join(root, rel), want)
    emit({"package": collapse_home(root),
          "files": sorted(files),
          "help": [f"Run `pi install {os.path.abspath(root)}` to install it",
                   "Run `datafile.py pi-package --check` to catch drift on this "
                   "machine (it bakes in an absolute path, so it fails elsewhere)"]})
    return 0


# ------------------------------------------------------------------- main

def build_parser() -> tuple[AxiParser, dict]:
    p = AxiParser(prog=PROG, description=DESCRIPTION)
    p.add_argument("--file", "-f", help="path to the .jsonl store")
    p.add_argument("--contract", "-c",
                   help="path to the YAML contract (auto-detected if omitted)")
    p.add_argument("--version", "-v", "-V", action="store_true",
                   help="print version and exit")
    sub = p.add_subparsers(dest="cmd", parser_class=AxiParser)
    subs: dict[str, AxiParser] = {}

    def add(name, help_text, epilog):
        sp = sub.add_parser(name, help=help_text, epilog=epilog,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        subs[name] = sp
        return sp

    sp = add("put", "insert or update records",
             "examples:\n"
             "  datafile.py -f u.jsonl put '{\"id\":\"a1\",\"name\":\"Ada\"}'\n"
             "  datafile.py -f u.jsonl put --set id=a1 --set name=Ada\n"
             "  cat dump.jsonl | datafile.py -f u.jsonl put -")
    sp.add_argument("record", nargs="?", help="JSON object, or - for JSONL on stdin")
    sp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="set one field; repeatable; merges over the JSON")

    sp = add("get", "read one record",
             "examples:\n  datafile.py -f u.jsonl get a1\n"
             "  datafile.py -f u.jsonl get a1 --full")
    sp.add_argument("id")
    sp.add_argument("--full", action="store_true",
                    help=f"do not truncate values (default: {DETAIL_TRUNCATE} chars)")

    sp = add("list", "list records",
             "examples:\n  datafile.py -f u.jsonl list\n"
             "  datafile.py -f u.jsonl list --fields id,name,age --limit 500")
    sp.add_argument("--fields", help="comma-separated fields (default: key + first 3)")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max records (default: {DEFAULT_LIMIT})")

    sp = add("keys", "list record ids only",
             "examples:\n  datafile.py -f u.jsonl keys\n"
             "  datafile.py -f u.jsonl keys --limit 5000")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max ids (default: {DEFAULT_LIMIT})")

    sp = add("stores", "find .jsonl stores and their contracts",
             "examples:\n  datafile.py stores\n  datafile.py stores --depth 6\n"
             "  datafile.py stores --all")
    sp.add_argument("--depth", type=int, default=DISCOVER_DEPTH,
                    help=f"directory levels to search (default: {DISCOVER_DEPTH})")
    sp.add_argument("--all", action="store_true",
                    help="no depth or count limit")

    sp = add("delete", "remove one record",
             "examples:\n  datafile.py -f u.jsonl delete a1")
    sp.add_argument("id")

    sp = add("validate", "check every line against the contract",
             "examples:\n  datafile.py -f u.jsonl validate\n"
             "  datafile.py -f u.jsonl validate --strict   # exit 1 if any bad line")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when unreadable lines exist (default: exit 0)")

    add("repair", "quarantine unreadable lines, then compact",
        "examples:\n  datafile.py -f u.jsonl repair")
    add("compact", "drop dead versions and tombstones",
        "examples:\n  datafile.py -f u.jsonl compact")

    sp = add("roll", "archive the active log into a compressed segment",
             "examples:\n  datafile.py -f events.jsonl roll --if-larger-than 50MB\n"
             "  datafile.py -f events.jsonl roll --archive-dir /data/cold")
    sp.add_argument("--if-larger-than", default="0", metavar="SIZE",
                    help="no-op unless the log is at least this big (e.g. 50MB)")
    sp.add_argument("--archive-dir", metavar="DIR",
                    help="where segments go (default: <store dir>/archive)")
    sp.add_argument("--force", action="store_true",
                    help="archive even when the log has updates or tombstones")

    sp = add("schema", "show the resolved contract",
             "examples:\n  datafile.py -c schema-user.yaml schema\n"
             "  datafile.py -c schema-user.yaml schema --json-schema")
    sp.add_argument("--json-schema", action="store_true",
                    help="emit standard JSON Schema instead of the summary")

    sp = add("skill", "generate the installable agent skill",
             "examples:\n  datafile.py skill\n  datafile.py skill --install\n"
             "  datafile.py skill --install --app pi\n"
             "  datafile.py skill --check")
    sp.add_argument("--out", help=f"output path (default: {SKILL_PATH})")
    sp.add_argument("--install", action="store_true",
                    help="write it where the agent reads skills from")
    sp.add_argument("--uninstall", action="store_true",
                    help="remove the installed skill")
    sp.add_argument("--scope", choices=["user", "project"], default="user",
                    help="user (home) or project (./); default: user")
    sp.add_argument("--app", choices=["all", "claude", "pi"], default="all",
                    help="which agent to install for; default: all")
    sp.add_argument("--check", action="store_true",
                    help="exit 1 if the committed skill is stale; writes nothing")

    sp = add("pi-package", "generate a pi package (skill + ambient context)",
             "examples:\n  datafile.py pi-package\n"
             "  datafile.py pi-package --out ./dist/pi\n"
             "  datafile.py pi-package --check")
    sp.add_argument("--out", help=f"package directory (default: {PI_PACKAGE_DIR})")
    sp.add_argument("--check", action="store_true",
                    help="exit 1 if the committed package is stale; writes nothing")

    sp = add("setup", "install session-start integrations (ambient context)",
             "examples:\n  datafile.py setup\n  datafile.py setup --status\n"
             "  datafile.py setup --app codex --scope project\n"
             "  datafile.py setup --uninstall")
    sp.add_argument("--scope", choices=["user", "project"], default="user",
                    help="user (home config) or project (./); default: user")
    sp.add_argument("--app", choices=["all", "claude", "codex", "opencode"],
                    default="all", help="which agent to target; default: all")
    sp.add_argument("--status", action="store_true",
                    help="report what is installed without changing anything")
    sp.add_argument("--uninstall", action="store_true", help="remove the integration")
    return p, subs


COMMANDS = {"put": cmd_put, "get": cmd_get, "list": cmd_list, "keys": cmd_keys,
            "delete": cmd_delete, "validate": cmd_validate, "repair": cmd_repair,
            "compact": cmd_compact, "roll": cmd_roll,
            "schema": cmd_schema, "setup": cmd_setup,
            "skill": cmd_skill, "stores": cmd_stores,
            "pi-package": cmd_pi_package}


def main(argv=None) -> int:
    global PROG
    if argv is None:
        PROG = os.path.basename(sys.argv[0]) or "datafile.py"
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_home()                                    # §8

    parser, subs = build_parser()
    try:
        try:
            args = parser.parse_args(argv)
        except _Usage as u:
            cmd = next((a for a in argv if a in subs), None)
            valid = [*_flags_of(subs[cmd]), "--file", "--contract"] if cmd \
                else _flags_of(parser)
            raise AxiError(
                u.message, "USAGE_ERROR",
                [f"valid flags for `{cmd or PROG}`: {', '.join(sorted(set(valid)))}",
                 f"Run `{PROG} {cmd or ''} --help` for the full reference".replace("  ", " ")],
                2) from None

        if args.version:
            print(VERSION)
            return 0
        if not args.cmd:
            return cmd_home()
        if args.cmd in ("setup", "skill", "stores", "pi-package"):
            return COMMANDS[args.cmd](args)
        return COMMANDS[args.cmd](args)

    except AxiError as e:
        return fail(e)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 1
    except Exception as e:                                   # §6 never leak a traceback
        return fail(AxiError(f"{type(e).__name__}: {e}", "INTERNAL_ERROR",
                             ["Run `datafile.py -f <file> validate` to check the store"], 1))


if __name__ == "__main__":
    raise SystemExit(main())
