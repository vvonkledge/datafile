---
name: datafile
description: >-
  Read or write append-only JSONL data files whose records are validated
  against a YAML-defined contract. Use when adding, updating, deleting,
  listing, or repairing rows in a .jsonl store, when a JSONL file has
  malformed lines to recover, or when defining a schema for one.
---

# datafile

Append-only JSONL record store with a runtime YAML contract. Records are
appended to a log, updated by appending a new version, and deleted with a
tombstone; reads fold the log so the last write wins. Every write is
validated against the contract before it is written.

## Invocation

`datafile.py` is a single self-contained script. Run it with `uv`, which
installs its declared dependencies automatically:

```sh
uv run /path/to/datafile.py --file data.jsonl --contract schema.yaml list
```

Install this skill with `datafile.py skill --install`. It is written to
each agent's skills directory; add `--scope project` to install it for
one repository instead of your account.

Examples below write `datafile.py` for brevity; prefix them with
`uv run /path/to/`. Running it with no arguments prints the stores in
the current directory and is the cheapest way to orient.

## Commands

| command | purpose | flags |
| --- | --- | --- |
| `put` | insert or update records | --set |
| `get` | read one record | --full, --json |
| `list` | list records | --fields, --limit (default: 100), --json |
| `keys` | list record ids only | --limit (default: 100) |
| `stores` | find .jsonl stores and their contracts | --depth (default: 3), --all |
| `delete` | remove one record | - |
| `validate` | check every line against the contract | --strict |
| `repair` | quarantine unreadable lines, then compact | - |
| `compact` | drop dead versions and tombstones | - |
| `roll` | archive the active log into a compressed segment | --if-larger-than (default: 0), --archive-dir, --force |
| `schema` | show the resolved contract | --json-schema |
| `skill` | generate the installable agent skill | --out, --install, --uninstall, --scope (default: user), --app (default: all), --check |
| `pi-package` | generate a pi package (skill + ambient context) | --out, --check |
| `setup` | install session-start integrations (ambient context) | --scope (default: user), --app (default: all), --status, --uninstall |

## Examples

**put**

```sh
datafile.py -f u.jsonl put '{"id":"a1","name":"Ada"}'
datafile.py -f u.jsonl put --set id=a1 --set name=Ada
cat dump.jsonl | datafile.py -f u.jsonl put -
```

**get**

```sh
datafile.py -f u.jsonl get a1
datafile.py -f u.jsonl get a1 --full
datafile.py -f u.jsonl get a1 --json
```

**list**

```sh
datafile.py -f u.jsonl list
datafile.py -f u.jsonl list --fields id,name,age --limit 500
datafile.py -f u.jsonl list --json
```

**keys**

```sh
datafile.py -f u.jsonl keys
datafile.py -f u.jsonl keys --limit 5000
```

**stores**

```sh
datafile.py stores
datafile.py stores --depth 6
datafile.py stores --all
```

**delete**

```sh
datafile.py -f u.jsonl delete a1
```

**validate**

```sh
datafile.py -f u.jsonl validate
datafile.py -f u.jsonl validate --strict   # exit 1 if any bad line
```

**repair**

```sh
datafile.py -f u.jsonl repair
```

**compact**

```sh
datafile.py -f u.jsonl compact
```

**roll**

```sh
datafile.py -f events.jsonl roll --if-larger-than 50MB
datafile.py -f events.jsonl roll --archive-dir /data/cold
```

**schema**

```sh
datafile.py -c schema-user.yaml schema
datafile.py -c schema-user.yaml schema --json-schema
```

**skill**

```sh
datafile.py skill
datafile.py skill --install
datafile.py skill --install --app pi
datafile.py skill --check
```

**pi-package**

```sh
datafile.py pi-package
datafile.py pi-package --out ./dist/pi
datafile.py pi-package --check
```

**setup**

```sh
datafile.py setup
datafile.py setup --status
datafile.py setup --app codex --scope project
datafile.py setup --uninstall
```

## Contract format

A contract is a YAML file describing one record type.

```yaml
name: User
key: id                 # which field is the primary key
extra: forbid           # forbid | ignore | allow
fields:
  id:    {type: str, pattern: '^[a-z][a-z0-9-]{1,31}$'}
  age:   {type: int, ge: 0, le: 150}
  email: {type: email, required: false}
  role:  {type: enum, values: [admin, member, guest], default: member}
  tags:  {type: list, items: str, default: []}
  note:  str            # shorthand for {type: str}
```

Types: any, bool, boolean, date, datetime, dict, email, enum, float, int, integer, list, number, str, string, text, time, uuid.

Constraints: description, ge, gt, le, lt, max_length, min_length, multiple_of, pattern, title.

`required: false` widens the type to allow null. Supplying `default:`
makes a field optional while keeping its type non-null. Unknown types,
constraints, and keys are rejected when the contract loads.

## Output and exit codes

Output is TOON on stdout, including errors, which carry a `code:` and
`help:` suggestions. Nothing is written to stderr.

`list --json` and `get --json` swap TOON for a single JSON document, for
a program that has to parse the output rather than read it: every field
of every live record untruncated, the `revision` (inode, size, mtime_ns)
of the store snapshot they were folded from, and a `bad_lines` entry for
every unreadable line. Failures stay JSON in that mode, with the same
exit codes.

| exit | meaning |
| --- | --- |
| 0 | success, including idempotent no-ops |
| 1 | the request could not be satisfied (not found, setup failure) |
| 2 | usage error or a record rejected by the contract |

`validate` exits 0 and reports `bad: N`; pass `--strict` to exit 1 when
any line is unreadable, which is the form to use in CI.
