# datafile runbook

A task-oriented guide for someone who has never run this tool. The
[README](README.md) explains *why* the design is what it is; this file explains
*what to type*, in order, and what to do when something goes wrong.

Work through sections 1-3 and you have a working store in about five minutes.
Sections 4 onward are day-2 operations: keep them for reference.

---

## 1. Install and verify

**Prerequisite:** [uv](https://docs.astral.sh/uv/). Nothing else. Python and the
dependencies (pydantic, PyYAML, toon) are declared inline in `datafile.py` via
PEP 723 and are fetched automatically on first run.

```sh
git clone <this repo> && cd datafile
uv run datafile.py --version
```

The first run downloads dependencies and takes a few seconds. Later runs are
fast.

**Put it on your PATH** so you can type `datafile` from any directory:

```sh
just install          # symlinks ~/.local/bin/datafile -> ./datafile.py
datafile --version    # confirm
```

If `just install` reports that the bindir is not on your `PATH`, add the line it
prints to your shell profile and open a new shell. To install elsewhere:
`just bindir=/usr/local/bin install`. To remove: `just uninstall`.

The install is a **symlink, not a copy**, so the command always tracks your
checkout. `git pull` updates the installed tool with no reinstall step.

> The rest of this runbook writes `datafile`. If you skipped the install step,
> write `uv run datafile.py` instead everywhere.

**Sanity check.** Run `datafile` with no arguments in any directory:

```
bin: ~/vvonkledge/sandbox/datafile/datafile.py
description: Append-only JSONL record store with a runtime YAML contract
version: 0.1.0
stores: 0 jsonl stores found in /path/to/here
help[2]:
  Write a contract to schema-<name>.yaml, then run `datafile.py -f <name>.jsonl -c schema-<name>.yaml put '{...}'`
  Run `datafile.py --help` for the full command list
```

The bare command is always safe and always tells you what exists here and what
to do next. When you are lost, run it.

---

## 2. Your first store

A store is two files you control:

| File | What it is |
| --- | --- |
| `users.jsonl` | the data, one JSON record per line |
| `schema-users.yaml` | the contract every record must satisfy |

### Step 1 - write the contract

Nothing can be written until a contract exists. Create `schema-users.yaml`:

```yaml
name: User
key: id                 # the primary key field
extra: forbid           # reject fields not listed below

fields:
  id:    {type: str, pattern: '^[a-z][a-z0-9-]{1,31}$'}
  name:  {type: str, min_length: 1, max_length: 100}
  age:   {type: int, ge: 0, le: 150}
  role:  {type: enum, values: [admin, member, guest], default: member}
```

The contract is **found automatically** for `users.jsonl` because of the file
name. Any of these work, checked in this order:

```
schema-<name>.yaml   schema-<name>.yml   <name>.schema.yaml
<name>.schema.yml    <name>.yaml         <name>.yml
```

Name it anything else and you must pass `-c <path>` on every command. Prefer
`schema-<name>.yaml` and never think about it again.

### Step 2 - write a record

```sh
datafile -f users.jsonl put '{"id":"ada","name":"Ada Lovelace","age":36}'
```

```
written: 1
rejected_count: 0
records[1]{id,op}:
  ada,created
```

Or without quoting JSON in your shell:

```sh
datafile -f users.jsonl put --set id=bob --set name=Bob --set age=41 --set role=admin
```

`--set` values are JSON-decoded when they parse as JSON, so `age=41` becomes an
integer and `tags=["sre"]` becomes a list. Anything else stays a string.

### Step 3 - read it back

```sh
datafile -f users.jsonl list
```

```
count: 2 of 2 total
users[2]{id,name,age,role}:
  ada,Ada Lovelace,36,member
  bob,Bob,41,admin
```

`ada` came back with `role: member` even though the input omitted it. Defaults
are materialised at write time, not at read time.

```sh
datafile -f users.jsonl get bob     # one record, every field
datafile -f users.jsonl keys        # ids only
datafile -f users.jsonl schema      # the resolved contract
```

That is the whole loop. You now have a store.

---

## 3. Everyday operations

| I want to | Command |
| --- | --- |
| See what stores exist here | `datafile` (no arguments) or `datafile stores` |
| Insert or update one record | `datafile -f s.jsonl put '{...}'` |
| Update a field | `put` the whole record again with the new value |
| Load a file of records | `cat dump.jsonl \| datafile -f s.jsonl put -` |
| Read one record | `datafile -f s.jsonl get <id>` |
| Read many | `datafile -f s.jsonl list --fields id,name --limit 500` |
| Delete | `datafile -f s.jsonl delete <id>` |
| Check the file is intact | `datafile -f s.jsonl validate` |

**`put` is insert-or-update.** There is no separate update command. Send the
full record; last write wins. The `op` column tells you which happened:

```
records[1]{id,op}:
  bob,updated          # created | updated | unchanged
```

**Mutations are idempotent.** Re-writing an identical record reports `unchanged`
and does not grow the file. Deleting an absent id is a no-op with exit 0. Both
are safe to retry, and safe to run from a script that may run twice.

**`list` truncates by default.** The default limit is 100 records and the
default columns are the key plus the first three fields. `count: 2 of 2 total`
tells you whether you are seeing everything. Raise `--limit` when you are not.

**Bulk loads are partial, not atomic.** With `put -`, valid records are written
and invalid ones are rejected in the same run. Exit code is 2 if *any* record
was rejected, but the good ones are already committed:

```
error: 1 record(s) rejected by contract
written: 2
rejected_count: 1
records[2]{id,op}:
  cy,created
  dee,created
rejected[1]{id,problem}:
  BAD!,"id: String should match pattern '^[a-z][a-z0-9-]{1,31}$'"
```

Fix the rejected lines and re-run the same file. The already-written records
come back `unchanged`, so re-running the whole input is the correct recovery.

---

## 4. Reading output, and scripting against it

All output, **including errors, goes to stdout** in
[TOON](https://toonformat.dev). Nothing is ever written to stderr. If you are
capturing output, capture stdout.

Branch on the exit code, not on the text:

| Exit | Meaning | Typical cause |
| --- | --- | --- |
| 0 | success, including no-ops | anything worked |
| 1 | request could not be satisfied | id not found, setup failed |
| 2 | usage error or contract rejection | bad flags, bad contract, bad record |

```sh
if datafile -f users.jsonl get "$id" >/dev/null; then
    echo "exists"
fi
```

Every error carries a machine-readable `code` and a suggested next command:

```
error: no record with id 'ada' in users.jsonl
code: NOT_FOUND
help[1]:
  Run `datafile.py -f users.jsonl list` to see available ids
```

For CI, `validate --strict` exits 1 if any line is unreadable:

```sh
datafile -f users.jsonl validate --strict
```

---

## 5. Changing a contract

**This is the sharpest edge in the tool. Read this section before editing a
contract that already has data behind it.**

The contract is applied at **read** time as well as write time. Records already
on disk are re-validated against whatever the contract says today. So a contract
edit can retroactively invalidate data you already wrote.

Adding a required field to a store with 3 existing records:

```yaml
  email: {type: email}      # required, because no `required: false`
```

```sh
datafile -f users.jsonl list
```

```
count: 0 of 3 total
bad: 3
users: []
```

Every existing record is now unreadable. **Do not run `repair` here.** It would
quarantine all three as bad lines.

### Safe changes

| Change | Safe on existing data? |
| --- | --- |
| Add a field with `required: false` | yes, reads as `null` |
| Add a field with a `default` | yes, the default fills in |
| Loosen a constraint (raise `le`, drop `pattern`) | yes |
| Add a required field with no default | **no**, invalidates every old record |
| Tighten a constraint | **no**, if any stored value now violates it |
| Change `extra: ignore` to `extra: forbid` | **no**, if old records carry extras |
| Rename a field | **no**, it is a drop plus an add |

### The procedure

1. Edit the contract.
2. Run `datafile -f s.jsonl validate` **before** anything else. `bad: 0` means
   you are done.
3. If lines went bad, either revert the contract, or make the new field
   `required: false` / give it a `default`, then backfill:
   `datafile -f s.jsonl list --limit 10000` to see the records, and `put` them
   again with the new field set.
4. Only run `repair` once you have decided the bad lines are genuinely garbage.
   It is not reversible from the store, though the quarantine file keeps the
   original text.

**A typo in a contract is a hard error, never a silent ignore:**

```
error: "schema-users.yaml: field 'name': unknown key ['minlength'].
  Valid: ['default', 'description', 'ge', 'gt', 'items', 'le', 'lt', 'max_length', ...]"
code: CONTRACT_ERROR
```

That is deliberate. A silently ignored constraint is the worst thing a contract
file can do.

---

## 6. Keeping a store healthy

Four maintenance commands. They are not interchangeable.

| Command | Use when | Destroys data? |
| --- | --- | --- |
| `validate` | always safe, run it first | no, read-only |
| `compact` | the file has grown from updates and deletes | no, drops only dead versions |
| `repair` | `validate` shows `bad: n` and you accept losing those lines | yes, moves bad lines out |
| `roll` | an append-only log has grown too large to keep hot | no, but archives are not read back |

### validate - always start here

```sh
datafile -f users.jsonl validate
```

```
valid: 1
bad: 3
issues[3]{line,byte,reason}:
  2,50,"invalid json: Expecting ',' delimiter at col 24"
  3,97,"contract violation: name: String should have at least 1 character"
  4,145,"torn tail: no trailing newline"
```

Three distinct failure modes, and **none of them stops the store from
working**. Good records still read fine; `list` reports `bad: 3` alongside them.
There is no emergency. Diagnose before acting.

- **invalid json** - a line was mangled, by an editor or a bad external writer.
- **contract violation** - the line parses but breaks the schema. Usually this
  means the *contract* changed, not the data. Go to section 5 first.
- **torn tail** - a crash mid-append left half a line. Only ever the last line.

### compact - reclaim space

Updates append a new version and deletes append a tombstone, so the file grows
even when the record count does not. `compact` rewrites the survivors:

```sh
datafile -f users.jsonl compact
```

```
kept: 1
reclaimed_bytes: 139
```

Safe to run any time, safe to run on a schedule. It is atomic: temp file, fsync,
rename. A crash mid-compaction leaves the original untouched.

### repair - quarantine the unreadable

```sh
datafile -f users.jsonl repair
```

```
kept: 1
quarantined: 3
quarantine: users.jsonl.quarantine
```

Bad lines move to `<store>.quarantine`, one JSON object each, carrying the
original text so you can fix and re-inject:

```json
{"offset": 50, "line": 2, "reason": "invalid json: ...", "raw": "{\"id\":\"cy\",...}"}
```

Fix them, then feed them back through `put -`.

### roll - archive an append-only log

For logs that only grow (events, audit trails), `compact` has nothing to
reclaim. `roll` seals the current file into a timestamped gzip segment and
starts the store empty:

```sh
datafile -f events.jsonl roll --if-larger-than 50MB
```

Under the threshold it is a no-op with exit 0, so it is safe on a cron:

```
roll: "events.jsonl is 560 bytes, under 50MB (no-op)"
```

Once it fires you get `archive/events-<timestamp>.jsonl.gz`, plus an
`archive/manifest.jsonl` that is itself a readable store:

```sh
datafile -f archive/manifest.jsonl list
gunzip -c archive/events-20260826T052329Z.jsonl.gz
```

**`roll` refuses a store that is not append-only,** exit 2:

```
error: "users.jsonl is not append-only: 0 updated key(s) and 1 tombstone(s)"
code: UNSAFE_ROLL
```

Segments are never folded back into reads. If a record's newest version lands in
an archive while an older version stays in the active log, reads would silently
return the stale one. Run `compact` first, then `roll`. Use `--force` only if
you have understood and accepted that.

### Sidecar files

A live store grows companions. None of them are source, all are rebuilt on
demand, and all are already in `.gitignore`:

| File | Purpose | Safe to delete? |
| --- | --- | --- |
| `<store>.idx` | id to byte-offset index | yes, rebuilds on next read |
| `<store>.lock` | flock target for writers | yes, when nothing is running |
| `<store>.quarantine` | output of `repair` | only after you have salvaged it |

Commit the `.jsonl` and the `schema-*.yaml`. Nothing else.

---

## 7. Troubleshooting

| Symptom | Code | What to do |
| --- | --- | --- |
| `no contract given and none found` | `USAGE_ERROR` | Name it `schema-<store>.yaml` next to the store, or pass `-c <path>` |
| `unknown key ['minlength']` | `CONTRACT_ERROR` | Typo in the contract. The error lists every valid key |
| `record(s) rejected by contract` | `CONTRACT_VIOLATION` | The `problem` column names the field and rule. `datafile -f s.jsonl schema` shows what is required |
| `no record with id 'x'` | `NOT_FOUND` | `datafile -f s.jsonl keys` to see what exists |
| `is not append-only` | `UNSAFE_ROLL` | `compact` first, then `roll` |
| `list` shows fewer rows than expected | - | Check `count: n of m total`. Raise `--limit`, or `validate` if it also prints `bad: n` |
| Everything went `bad` at once | - | You almost certainly edited the contract. Section 5 |
| A write appears to hang | - | Another `datafile` process holds the write lock on that store. Wait, or find it with `lsof <store>.lock` |
| Stale data after an external edit | - | The `.idx` is validated against inode and size, so this should self-heal. If not, delete `<store>.idx` |

**Concurrency.** Locking is advisory `flock` on the sidecar. It only excludes
other processes that also take the lock, meaning other `datafile` invocations.
Anything else editing the `.jsonl` directly bypasses it. One writer at a time is
the supported model; concurrent readers are fine.

**Durability.** A committed write survives a process crash or a kernel panic. On
macOS it can still be lost to a power cut, because `fsync` there does not flush
the drive's write cache and `datafile` does not issue `F_FULLFSYNC`. On Linux the
guarantee holds. The README's [Durability](README.md#durability) section explains
the trade and points at the line to change if you need the stronger one.

**Size.** Reads load the whole file into memory. Comfortable to a few hundred
MB. Past that, or if you need secondary indexes or real multi-writer
concurrency, the README's "When not to use this" section makes the case for
SQLite instead.

---

## 8. Agent integration

If you want an AI agent to use these stores, install one of the two paths. You
need one, not both.

```sh
datafile skill --install     # a skill, loaded on demand when a task matches
datafile setup               # session-start context: agent knows your stores upfront
datafile setup --status      # read-only check of what is installed
datafile setup --uninstall
```

`setup` targets Claude Code, Codex, and OpenCode. `skill --install` targets
Claude Code and pi. Both are generated from the CLI's own parser, so they cannot
drift from the tool; `skill --check` exits 1 if a committed copy is stale, which
is how CI gates it.

---

## 9. Working on datafile itself

```sh
uv run test_datafile.py                                       # the suite
uv run test_datafile.py -q                                    # what CI runs
uv run test_datafile.py --cov=. --cov-branch --cov-report=term-missing
uvx ruff check .                                              # lint
uv run datafile.py skill --check --scope project --app pi     # drift gate
uvx --from commitizen cz check --rev-range origin/main..HEAD   # commit messages
```

The suite holds 100% line and branch coverage, so **a new branch without a test
will fail CI**. Coverage is not the real bar though: the code is also guarded by
hand-written mutation tests. Break the torn-tail guard, the lock mode, the inode
check, or an off-by-one, and a specifically named test fails. If you change
behaviour in those areas, expect to update a named test and be sure you meant
to.

`.github/workflows/ci.yml` runs the commit message gate, lint, tests, and both
drift gates, then releases from `main` (section 10). Both gates are portable:
neither the skill nor the pi package carries a path from the machine that
generated it, so the same commit checks clean in any checkout. The pi package
does carry the version, but `cz bump` rewrites it in the same commit that bumps
`VERSION`, so a release cannot stale it either.

The whole tool is one file, `datafile.py`. Section markers (`§8` and similar) in
its comments refer to the design sections.

---

## 10. Commits, versions, and releases

Versions and `CHANGELOG.md` are derived from commit messages by
[commitizen](https://commitizen-tools.github.io/commitizen/). Nothing about a
release is written by hand.

**`CHANGELOG.md`, `VERSION` in `datafile.py`, the `version` in
`pi-datafile/package.json`, and the `version` in `.cz.toml` are all generated.
Do not edit any of them.** `cz bump` owns all four and overwrites hand edits on
the next release.

### Writing a commit

Messages must follow
[Conventional Commits](https://www.conventionalcommits.org/). CI rejects a pull
request whose commits do not, because a non-conventional message contributes
nothing to the changelog and leaves the release with nothing to act on.

```
<type>(<optional scope>): <description>
```

```sh
git commit -m "feat(roll): add --if-larger-than threshold"
git commit -m "fix: correct the byte offset in validate output"
git commit -m "docs: clarify contract evolution in the runbook"
```

If you would rather be prompted through it:

```sh
uvx --from commitizen cz commit
```

Accepted types, and what each does to the version:

| Type | Version effect | Appears in changelog |
| --- | --- | --- |
| `feat` | minor, `0.1.0` -> `0.2.0` | yes, under **Feat** |
| `fix` | patch, `0.1.0` -> `0.1.1` | yes, under **Fix** |
| `perf` | patch | yes |
| `refactor` | patch | yes |
| `docs` `test` `ci` `build` `style` `chore` `revert` | none | no |

A breaking change is `feat!:` or a `BREAKING CHANGE:` footer, and takes the
major: `0.1.0` -> `1.0.0`. This repo uses standard SemVer, so **the first
breaking change promotes datafile to 1.0.0** and commits it to a stable public
interface. That is a deliberate choice, not an accident of the tooling. If you
are not ready to declare 1.0, do not mark the change breaking.

Merge commits are exempt, so the merge-commit pull request flow needs no special
handling.

### Cutting a release

You do not. Merging to `main` does it:

1. CI runs lint, tests, and the skill drift gate.
2. If those pass, the `bump` job runs `cz bump`, which rewrites the version
   everywhere, regenerates `CHANGELOG.md`, commits, and tags `vX.Y.Z`.
3. It pushes the commit and tag back to `main`.

A push with no `feat`, `fix`, `perf`, or `refactor` since the last tag releases
nothing and succeeds anyway. That is exit code 21, `NO_COMMITS_TO_BUMP`, which
the job treats as success rather than failure.

The bump commit carries `[skip ci]`, and pushes made with `GITHUB_TOKEN` do not
trigger workflows, so the job cannot retrigger itself. Skipping CI on it is safe
because it only touches version strings and the changelog, and the tests compare
against the imported `VERSION` constant rather than a literal.

### Previewing a release before you merge

Both are read-only and write nothing:

```sh
uvx --from commitizen cz bump --dry-run       # what version would this become
uvx --from commitizen cz changelog --dry-run  # what the changelog entry would say
```

To check your branch would pass the CI message gate:

```sh
uvx --from commitizen cz check --rev-range origin/main..HEAD
```

Exit 0 passes. Exit 14 means a message is malformed, and the error prints the
offending commit alongside the pattern it had to match.

### If the bump job fails

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Permission to ... denied` / 403 on push | The repo's default `GITHUB_TOKEN` is read-only | Settings -> Actions -> General -> Workflow permissions -> "Read and write permissions" |
| `changelog_start_rev` error / unknown revision | The `v0.1.0` baseline tag is missing from the remote | `git push origin v0.1.0` |
| Version moved but no tag on the remote | The push half-completed | `git push --follow-tags` from an up-to-date `main` |
| Bump job never ran | The push was not to `main`, or the head commit carried `[skip ci]` | Expected. Only `main` releases |

---

## Where to go next

- [README.md](README.md) - the design rationale, full type and constraint
  reference, and the honest case for reaching for SQLite instead.
- `datafile <command> --help` - every subcommand carries its own flags,
  defaults, and worked examples.
- `datafile` with no arguments - when in doubt, it tells you what is here and
  what to run.
