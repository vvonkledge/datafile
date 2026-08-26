# datafile

An append-only JSONL record store with a schema contract you inject at runtime.

One self-contained Python script. Records are validated against a YAML contract
before they are written, corrupt lines never take down the whole file, and every
mutation is crash-safe.

```sh
datafile.py -f users.jsonl put '{"id":"ada","name":"Ada Lovelace","age":36,"note":"founder"}'
```

```
written: 1
rejected_count: 0
records[1]{id,op}:
  ada,created
```

## Why

A filesystem has no concept of a "row". It gives you three operations: append,
overwrite in place at the same length, and truncate. There is no "insert 40
bytes at offset 900", so updating or deleting a record in the middle of a file
means rewriting everything after it.

`datafile` works with that constraint instead of against it:

- **Updates append a new version.** Reads fold the log, last write wins.
- **Deletes append a tombstone.** Nothing is rewritten.
- **Compaction** rewrites the surviving records into a fresh file and renames it
  into place atomically.

This is how Kafka, RocksDB, and Git work. Writes stay O(1), and a byte-level
corruption costs you one line rather than the whole file.

New here? [RUNBOOK.md](RUNBOOK.md) walks through install, a first store, and
day-2 operations step by step. This README covers the design and the reference.

## Install

Requires [uv](https://docs.astral.sh/uv/). Dependencies are declared inline
(PEP 723) and installed automatically on first run.

```sh
uv run datafile.py --help
```

To use it anywhere, put it on your `PATH`:

```sh
just install                              # ~/.local/bin/datafile -> ./datafile.py
just bindir=/usr/local/bin install        # somewhere else
just uninstall
datafile stores
```

Without [just](https://just.systems), that is `chmod +x datafile.py` and a
symlink into a directory on your `PATH`. The link is deliberate rather than a
copy: the command tracks the checkout instead of going stale behind it.

The script carries a `#!/usr/bin/env -S uv run --script` shebang, so the symlink
works without a wrapper. `uv tool install` does not accept a bare script, only a
packaged distribution.

## Quick start

Write a contract describing one record type:

```yaml
# schema-users.yaml
name: User
key: id                 # which field is the primary key
extra: forbid           # forbid | ignore | allow

fields:
  id:    {type: str, pattern: '^[a-z][a-z0-9-]{1,31}$'}
  name:  {type: str, min_length: 1, max_length: 100}
  age:   {type: int, ge: 0, le: 150}
  email: {type: email, required: false}
  role:  {type: enum, values: [admin, member, guest], default: member}
  tags:  {type: list, items: str, default: []}
  note:  str            # shorthand for {type: str}
```

The contract is found automatically for `users.jsonl` if it is named
`schema-users.yaml` next to it. Otherwise pass `-c <path>`.

```sh
datafile.py -f users.jsonl put '{"id":"ada","name":"Ada Lovelace","age":36,"note":"founder"}'
datafile.py -f users.jsonl put --set id=bob --set name=Bob --set age=41 --set note=ops
cat dump.jsonl | datafile.py -f users.jsonl put -
```

`--set` values are JSON-decoded when possible, so `age=41` becomes an integer
and `tags=["sre"]` becomes a list. Anything that is not valid JSON stays a
string.

```sh
datafile.py -f users.jsonl list
```

```
count: 2 of 2 total
users[2]{id,name,age,note}:
  ada,Ada Lovelace,36,founder
  bob,Bob,41,ops
help[1]:
  Run `datafile.py -f users.jsonl get <id>` for one record with all fields
```

```sh
datafile.py -f users.jsonl get bob
```

```
record:
  id: bob
  name: Bob
  age: 41
  email: null
  role: member
  tags: []
  note: ops
```

Defaults are materialised on write, so `role` and `tags` come back filled in
even though the input omitted them.

## The contract

Contracts are compiled into a Pydantic model at runtime, so validation happens
before anything is written.

**Types:** `str` `int` `float` `bool` `datetime` `date` `time` `uuid` `email`
`enum` `list` `dict` `any`

**Constraints:** `ge` `le` `gt` `lt` `multiple_of` `min_length` `max_length`
`pattern` `description` `title`

Field semantics:

| Written as | Meaning |
| --- | --- |
| `age: {type: int}` | required |
| `email: {required: false}` | optional, type widens to allow null |
| `role: {default: member}` | optional, type stays non-null |

Unknown types, unknown constraint keys, and a `key` that is not among the fields
are all hard errors when the contract loads. A typo that gets silently ignored
is the worst failure mode for a contract file, so there is no leniency:

```
contract error: schema.yaml: field 'id': unknown key ['minlength'].
  Valid: ['default', 'description', 'ge', 'gt', 'items', 'le', 'lt', ...]
```

`datafile.py -f users.jsonl schema` prints the resolved contract;
`--json-schema` emits standard JSON Schema for interop.

## Commands

| Command | What it does |
| --- | --- |
| *(no arguments)* | list the stores in this directory and how to act on them |
| `put` | insert or update; accepts JSON, `--set` pairs, or `-` for stdin |
| `get <id>` | one record, all fields |
| `list` | records as a table; `--fields`, `--limit` |
| `keys` | ids only; `--limit` |
| `stores` | find `.jsonl` stores and their contracts; `--depth`, `--all` |
| `delete <id>` | append a tombstone |
| `validate` | check every line; `--strict` to exit 1 on any bad line |
| `repair` | quarantine unreadable lines, then compact |
| `compact` | drop dead versions and tombstones |
| `roll` | seal the log into a compressed archive segment |
| `schema` | show the resolved contract |
| `skill` | generate the agent skill; `--install`, `--check` |
| `pi-package` | generate a pi package; `--check` |
| `setup` | install session-start integrations |

Every subcommand has `--help` with its flags, defaults, and examples.

## Output and exit codes

Output is [TOON](https://toonformat.dev) on stdout, including errors. Nothing is
written to stderr. TOON is roughly 40% cheaper than equivalent JSON for an agent
to read, and stays readable for humans.

| Exit | Meaning |
| --- | --- |
| 0 | success, including idempotent no-ops |
| 1 | the request could not be satisfied (not found, setup failure) |
| 2 | usage error, or a record rejected by the contract |

Errors are structured and carry a code plus a suggested next command:

```
error: 1 record(s) rejected by contract
code: CONTRACT_VIOLATION
written: 0
rejected_count: 1
rejected[1]{id,problem}:
  Eve!,"id: String should match pattern '^[a-z][a-z0-9-]{1,31}$'; name: String should have at least 1 character; age: Input should be less than or equal to 150"
```

Mutations are idempotent. Writing a record identical to the stored one reports
`unchanged` and does not grow the log; deleting an absent record is a no-op with
exit 0.

## Recovering broken data

Three failure modes are handled separately, and none of them stops the load:

| Problem | Cause |
| --- | --- |
| torn tail | a crash mid-append left a half-written last line |
| invalid json | a line was mangled |
| contract violation | a line parses but breaks the schema |

```sh
datafile.py -f users.jsonl validate
```

```
valid: 2
bad: 3
issues[3]{line,byte,reason}:
  3,187,"invalid json: Expecting ',' delimiter at col 27"
  4,227,"contract violation: name: String should have at least 1 character; age: Input should be less than or equal to 150"
  5,273,"torn tail: no trailing newline"
```

Byte offsets are reported alongside line numbers because offsets stay valid when
only part of the file is rescanned.

```sh
datafile.py -f users.jsonl repair
```

Good records survive, bad lines move to `users.jsonl.quarantine` with their
offset, line number, reason, and original text, so you can fix and re-inject
them.

Use `validate --strict` in CI to fail a build on any unreadable line.

## Archiving an append-only log

`compact` reclaims space inside the active file. For a log that only ever grows,
`roll` seals it instead: the current file becomes a timestamped gzip segment and
the store starts fresh and empty.

```sh
datafile.py -f events.jsonl roll --if-larger-than 50MB
```

```
roll: "events.jsonl is 62,781 bytes, under 50MB (no-op)"
```

Under the threshold it is a no-op with exit 0, so it is safe to run on a
schedule. Once it fires:

```
rolled: events-20260825T143217Z.jsonl.gz
records: 500
bytes: 62781
gz_bytes: 4192
```

The archive directory (default `<store dir>/archive`) gets the segment, a
`manifest.jsonl` row recording its record count, byte sizes, SHA-256, and
timestamp, and a `schema-manifest.yaml` so the manifest is itself a readable
store:

```sh
datafile.py -f archive/manifest.jsonl list
gunzip -c archive/events-20260825T143217Z.jsonl.gz
```

**`roll` refuses a log that is not append-only.** Segments are never folded back
in, so if a record's latest version lands in an archive while an older version
stays in the active log, reads would silently return the stale one:

```
error: "users.jsonl is not append-only: 1 updated key(s) and 1 tombstone(s)"
code: UNSAFE_ROLL
updated_keys: 1
tombstones: 1
help[3]:
  Segments are never folded back in, so a record whose latest version lands in one becomes unreachable
  Run `datafile.py -f users.jsonl compact` first, then roll
  Run `datafile.py -f users.jsonl roll --force` to archive anyway
```

That is exit 2. Run `compact` first, or `roll --force` if you accept the
consequence.

Only the two renames happen under the write lock; compression runs outside it,
so writers stall for a rename rather than for gzip.

## Agent integration

The tool follows the [AXI](https://github.com/kunchenguid/axi) conventions for
agent-facing CLIs, and ships two integration paths. You only need one.

**A skill**, loaded on demand when a task matches:

```sh
datafile.py skill --install              # Claude Code and pi
datafile.py skill --install --app pi
datafile.py skill --check                # CI: fail if the committed skill drifted
```

**Session-start context**, so an agent opens a session already knowing which
stores exist:

```sh
datafile.py setup                        # Claude Code, Codex, OpenCode
datafile.py setup --status               # read-only check
datafile.py setup --uninstall
```

**A pi package**, bundling both for [pi](https://pi.dev):

```sh
datafile.py pi-package
pi install /path/to/pi-datafile
```

The skill is generated from the CLI's own parser and contract loader, so it
cannot drift. `--check` fails if the committed copy is stale.

## How it works

- **Append-only log.** Updates append a new version, deletes append a tombstone,
  reads fold last-write-wins.
- **Offset index.** A sidecar `.idx` maps id to byte offset, validated against
  the log's inode and size. Restart costs O(new bytes), not O(file). On 50k
  records an indexed `get` is roughly 2,800x faster than folding the log.
- **Advisory locking.** `flock` on a sidecar `.lock` file. The sidecar is used
  because compaction replaces the data file's inode, and a lock held on the data
  file would stop excluding anyone.
- **Atomic rewrites.** Temp file, fsync, `os.replace`, then fsync the directory
  so the rename is persisted rather than left in the page cache. File permissions
  are preserved. How far that guarantee actually reaches depends on the platform,
  and on macOS it stops short: see [Durability](#durability).
- **Torn-tail guard.** An append checks the final byte first and closes an
  unterminated line, so a half-written record cannot swallow the next one.

## When not to use this

Reach for SQLite instead when:

- **You need more than one writer.** Locking here is advisory and cooperative.
  Anything that writes without taking the lock bypasses it.
- **The file outgrows memory.** `load()` reads the whole file. Fine to a few
  hundred MB.
- **You query by anything but the primary key.** There are no secondary indexes,
  so everything else is a full scan.
- **You need durability across a power cut on macOS.** `fsync` there does not
  reach the drive's media. See [Durability](#durability).

The question that decides it: does a human or an agent need to read and diff
this file directly? If yes, a JSONL log is greppable, diffable, and recoverable
line by line. If no, SQLite gives you indexes, transactions, and real
concurrency in one portable file.

## Durability

The write ordering is the part a log store has to get right, and it is testable:
appends are `write` then `flush` then `fsync`; rewrites are temp file, `fsync`,
`os.replace`, then `fsync` on the directory so the rename is persisted too; a
torn tail is closed before the next append rather than left to swallow it. The
suite covers those paths.

What a test suite cannot reach is the layer underneath, and on macOS that layer
does not do what the name suggests. **`fsync()` on macOS does not flush the
drive's write cache.** Apple's own header says as much:

```c
#define F_FULLFSYNC  51   /* fsync + ask the drive to flush to the media */
```

Only `fcntl(fd, F_FULLFSYNC)` asks the drive to commit to media, and `datafile`
does not issue it. The two are measurably distinct: on a local APFS volume,
`fsync` returns in roughly 28us and `F_FULLFSYNC` in roughly 3ms, a factor of
about 110.

So on macOS the guarantee stops at the drive. A record survives a process crash
or a kernel panic, because the data has reached the OS and the drive. It can
still be lost to a power cut that hits while the write sits in the drive's
volatile cache, even though `fsync` already returned. On Linux, `fsync` does
flush the device cache and the guarantee holds.

That is a deliberate default, not an oversight. Issuing `F_FULLFSYNC` on every
append would cost about 110x per write, which is the wrong trade for a store
whose reason to exist is a greppable file rather than a transactional database.
SQLite reaches the same conclusion and exposes it as `PRAGMA fullfsync`, off by
default. If you need the stronger guarantee on macOS, the change is to route the
five `os.fsync` calls in `datafile.py` through `fcntl(fd, F_FULLFSYNC)` on
Darwin, keeping `os.fsync` elsewhere.

One claim stays genuinely unprovable in software: whether the kernel or the drive
reorders writes across the rename. That is exactly what the directory `fsync`
defends against, and only power-loss testing on real hardware would settle it.

## Development

```sh
uv run test_datafile.py                                          # 287 tests
uv run test_datafile.py --cov=. --cov-branch --cov-report=term-missing
datafile.py skill --check && datafile.py pi-package --check      # drift gates
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) and
CI enforces it on pull requests. Merging to `main` runs
[commitizen](https://commitizen-tools.github.io/commitizen/), which bumps the
version, writes `CHANGELOG.md`, and tags the release. `CHANGELOG.md` and every
version string are generated, so do not edit them by hand. See
[RUNBOOK.md](RUNBOOK.md#10-commits-versions-and-releases).

The suite holds 100% line and branch coverage. Coverage alone does not prove
much, so the code is also checked by hand-written mutation tests: break the
torn-tail guard, the lock mode, the inode check, the append `fsync`, the
directory `fsync` after a rename, or an off-by-one, and a named test fails.

`TestDurability` covers the testable half of the durability claim: that the
append path issues exactly one `fsync` on the data file, that every rename is
bracketed by `fsync(file)` before and `fsync(dir)` after, that a crash at any
one of those boundaries keeps every committed record, and that a torn final
record is recoverable when cut at any byte, including mid-codepoint. What it
cannot cover is whether `fsync` reaches the medium, which is the subject of the
[Durability](#durability) section.

Python has no MC/DC tooling. `coverage.py` gives statement and branch coverage;
with short-circuit `and`/`or` neither tells you whether an individual
sub-condition drove a decision. Mutation testing is the practical substitute.
