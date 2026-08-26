#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic[email]>=2.0", "pyyaml>=6.0", "pytest>=8.0",
#                 "pytest-cov>=5.0"]
# ///
"""Test suite for datafile.py.  Run:  uv run test_datafile.py"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import ClassVar

import pytest

HERE = Path(__file__).resolve().parent
DATAFILE_PY = HERE / "datafile.py"

_spec = importlib.util.spec_from_file_location("store_mod", DATAFILE_PY)
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)

CONTRACT = textwrap.dedent("""
    name: User
    key: id
    extra: forbid
    fields:
      id:    {type: str, pattern: '^[a-z][a-z0-9-]{1,31}$'}
      name:  {type: str, min_length: 1, max_length: 100}
      age:   {type: int, ge: 0, le: 150}
      email: {type: email, required: false}
      role:  {type: enum, values: [admin, member, guest], default: member}
      tags:  {type: list, items: str, default: []}
      note:  str
""")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "schema-users.yaml").write_text(CONTRACT)
    (tmp_path / "schema-u.yaml").write_text(CONTRACT)     # for -f u.jsonl
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "PROG", "datafile.py")
    return tmp_path


@pytest.fixture
def cli(capsys):
    def run(*argv):
        code = store.main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err
    return run


def rec(**kw):
    base = {"id": "ada", "name": "Ada", "age": 36, "note": "n"}
    base.update(kw)
    return json.dumps(base)


def make_store(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text(CONTRACT)
    model, key = store.load_contract(str(c))
    return store.Store(str(tmp_path / "d.jsonl"), model, key=key)


# ----------------------------------------------------------------- TOON (§1)

class TestToon:
    def test_spec_example(self):
        assert store.toon({
            "user": "Ada", "active": True, "score": 42, "email": "",
            "details": {"age": 30, "city": "Berlin"},
            "items": [{"id": 1, "name": "Widget"}, {"id": 2, "name": "Gadget"}],
            "tags": ["admin", "ops", "dev"],
        }) == (
            'user: Ada\nactive: true\nscore: 42\nemail: ""\n'
            "details:\n  age: 30\n  city: Berlin\n"
            "items[2]{id,name}:\n  1,Widget\n  2,Gadget\n"
            "tags[3]: admin,ops,dev"
        )

    def test_numeric_string_is_quoted(self):
        assert store.toon({"tasks": [{"id": "1", "t": "Fix"}]}) == \
            'tasks[1]{id,t}:\n  "1",Fix'

    @pytest.mark.parametrize("value,expected", [
        ("", '""'), ("true", '"true"'), ("false", '"false"'), ("null", '"null"'),
        ("42", '"42"'), ("-1.5e3", '"-1.5e3"'), ("a:b", '"a:b"'), ("a,b", '"a,b"'),
        ("a[b", '"a[b"'), ("a{b", '"a{b"'), ("-x", '"-x"'), ("#x", '"#x"'),
        (" x", '" x"'), ("x ", '"x "'), ("plain", "plain"),
        ("with space", "with space"),
    ])
    def test_quoting_rules(self, value, expected):
        assert store.toon({"k": value}) == f"k: {expected}"

    @pytest.mark.parametrize("value,expected", [
        ('say "hi"', '"say \\"hi\\""'),
        ("a\\b", '"a\\\\b"'),
        ("a\nb", '"a\\nb"'),
        ("a\tb", '"a\\tb"'),
        ("a\rb", '"a\\rb"'),
        ("a\x01b", '"a\\u0001b"'),
    ])
    def test_escapes(self, value, expected):
        assert store.toon({"k": value}) == f"k: {expected}"

    @pytest.mark.parametrize("value,expected", [
        (None, "null"), (True, "true"), (False, "false"),
        (42, "42"), (42.0, "42"), (42.5, "42.5"), (-0.0, "0"),
    ])
    def test_primitives(self, value, expected):
        assert store.toon({"k": value}) == f"k: {expected}"

    def test_empty_list(self):
        assert store.toon({"k": []}) == "k: []"

    def test_prose_list_is_multiline_not_comma_joined(self):
        assert store.toon({"help": ["Run a, then b", "Run c"]}) == \
            "help[2]:\n  Run a, then b\n  Run c"

    def test_token_list_stays_inline(self):
        assert store.toon({"keys": ["a", "b"]}) == "keys[2]: a,b"

    def test_nested_indentation_is_two_spaces(self):
        assert store.toon({"a": {"b": {"c": 1}}}) == "a:\n  b:\n    c: 1"


# --------------------------------------------------------------- contract

class TestContract:
    def test_valid(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(CONTRACT)
        model, key = store.load_contract(str(p))
        assert key == "id"
        got = model.model_validate(
            {"id": "ab", "name": "A", "age": 1, "note": "n"})
        assert got.role == "member" and got.tags == [] and got.email is None

    @pytest.mark.parametrize("body,fragment", [
        ("fields: {id: {type: strr}}", "unknown type"),
        ("fields: {id: {type: str, minlength: 3}}", "unknown key"),
        ("fields: {id: str}\nkey: uuid", "is not among fields"),
        ("fields: {id: {type: enum}}", "non-empty 'values'"),
        ("fields: {}", "non-empty 'fields'"),
        ("key: id", "non-empty 'fields'"),
        ("fields: {id: {type: str}}\nextra: maybe", "forbid|ignore|allow"),
        ("[1, 2]", "must be a mapping"),
        ("fields: {id: 5}", "must be a type name or a mapping"),
    ])
    def test_rejected(self, tmp_path, body, fragment):
        p = tmp_path / "c.yaml"
        p.write_text(body)
        with pytest.raises(store.ContractError) as e:
            store.load_contract(str(p))
        assert fragment in str(e.value)

    def test_missing_file(self, tmp_path):
        with pytest.raises(store.ContractError, match="not found"):
            store.load_contract(str(tmp_path / "nope.yaml"))

    def test_default_keeps_type_non_null(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(CONTRACT)
        model, _ = store.load_contract(str(p))
        with pytest.raises(Exception):
            model.model_validate({"id": "ab", "name": "A", "age": 1,
                                  "note": "n", "role": None})

    def test_required_false_allows_null(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(CONTRACT)
        model, _ = store.load_contract(str(p))
        assert model.model_validate(
            {"id": "ab", "name": "A", "age": 1, "note": "n",
             "email": None}).email is None


# --------------------------------------------------------------- store core

class TestStoreCore:
    def test_put_get_update_delete(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "Ada", "age": 36, "note": "n"})
        assert s.get("ada").name == "Ada"
        s.put({"id": "ada", "name": "Ada L", "age": 37, "note": "n"})
        assert s.get("ada").name == "Ada L"
        s.delete("ada")
        assert s.get("ada") is None
        assert s.keys() == []

    def test_log_is_append_only(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        s.put({"id": "ada", "name": "B", "age": 1, "note": "n"})
        s.delete("ada")
        assert len(Path(s.path).read_text().strip().splitlines()) == 3

    def test_corruption_is_isolated(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write('{"id":"bad","name":"B"  <-- broken\n')
            f.write('{"id":"leo","name":"","age":300,"note":"n"}\n')
            f.write('{"id":"tor","name":"T')
        alive, bad = s.load()
        assert sorted(alive) == ["ada"]
        assert len(bad) == 3
        reasons = " ".join(b.reason for b in bad)
        assert "invalid json" in reasons
        assert "contract violation" in reasons
        assert "torn tail" in reasons
        assert all(b.line is not None for b in bad)
        assert [b.line for b in bad] == [2, 3, 4]

    def test_contract_violation_reports_every_field(self, tmp_path):
        s = make_store(tmp_path)
        with open(s.path, "w") as f:
            f.write('{"id":"leo","name":"","age":300,"note":"n"}\n')
        _, bad = s.load()
        assert "name:" in bad[0].reason and "age:" in bad[0].reason

    def test_torn_tail_cannot_swallow_next_append(self, tmp_path):
        s = make_store(tmp_path)
        with open(s.path, "w") as f:
            f.write('{"id":"tor","name":"T')
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        assert s.get("ada") is not None

    def test_missing_key_is_a_bad_line(self, tmp_path):
        s = make_store(tmp_path)
        with open(s.path, "w") as f:
            f.write('{"name":"NoId"}\n')
        _, bad = s.load()
        assert "missing key" in bad[0].reason

    def test_compact_drops_dead_versions(self, tmp_path):
        s = make_store(tmp_path)
        for i in range(5):
            s.put({"id": "ada", "name": f"A{i}", "age": 1, "note": "n"})
        s.put({"id": "bob", "name": "B", "age": 1, "note": "n"})
        s.delete("bob")
        kept, _ = s.compact()
        assert kept == 1
        assert len(Path(s.path).read_text().strip().splitlines()) == 1
        assert s.get("ada").name == "A4"

    def test_repair_quarantines(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{garbage\n")
        kept, dropped = s.repair()
        assert (kept, dropped) == (1, 1)
        q = json.loads(Path(s.path + ".quarantine").read_text().strip())
        assert q["reason"].startswith("invalid json")
        assert q["raw"] == "{garbage\n"      # verbatim, so it can be re-injected
        assert q["line"] == 2 and q["offset"] > 0

    def test_compact_preserves_permissions(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        os.chmod(s.path, 0o640)
        s.compact()
        assert os.stat(s.path).st_mode & 0o777 == 0o640

    def test_index_survives_compaction(self, tmp_path):
        s = make_store(tmp_path)
        for i in range(3):
            s.put({"id": f"u{i}", "name": "A", "age": 1, "note": "n"})
        s.compact()
        fresh = store.Store(s.path, s.model, key=s.key)
        assert sorted(fresh.keys()) == ["u0", "u1", "u2"]
        assert fresh.get("u2").name == "A"

    def test_index_incremental_catchup(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        other = store.Store(s.path, s.model, key=s.key)
        assert other.keys() == ["ada"]
        s.put({"id": "bob", "name": "B", "age": 1, "note": "n"})
        assert sorted(other.keys()) == ["ada", "bob"]

    def test_index_offsets_point_at_latest_version(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "First", "age": 1, "note": "n"})
        s.put({"id": "ada", "name": "Second", "age": 1, "note": "n"})
        s.keys()
        with open(s.path, "rb") as f:
            f.seek(s._offsets["ada"])
            assert json.loads(f.readline())["name"] == "Second"

    def test_corrupt_index_is_only_a_cold_cache(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        s.keys()
        Path(s.idxpath).write_text("not json")
        fresh = store.Store(s.path, s.model, key=s.key)
        assert fresh.keys() == ["ada"]

    def test_missing_file_reads_empty(self, tmp_path):
        s = make_store(tmp_path)
        assert s.load() == ({}, [])
        assert s.keys() == []
        assert s.get("x") is None

    def test_crlf_is_tolerated(self, tmp_path):
        s = make_store(tmp_path)
        with open(s.path, "wb") as f:
            f.write(b'{"id":"ada","name":"A","age":1,"note":"n"}\r\n')
        alive, bad = s.load()
        assert bad == [] and "ada" in alive

    def test_put_rejects_record_without_key(self, tmp_path):
        s = make_store(tmp_path)
        with pytest.raises(Exception):
            s.put({"name": "A", "age": 1, "note": "n"})


class TestBoundedReads:
    """list and keys must cost the index, not the store (§2/§9)."""

    def test_page_materialises_only_the_limit(self, tmp_path, monkeypatch):
        s = make_store(tmp_path)
        for i in range(20):
            s.put({"id": f"u{i}", "name": "A", "age": 1, "note": "n"})

        calls = []
        real = s.model.model_validate_json
        monkeypatch.setattr(s.model, "model_validate_json",
                            lambda raw, *a, **k: (calls.append(1), real(raw))[1])

        records, total, bad = s.page(3)
        assert [r.id for r in records] == ["u0", "u1", "u2"]
        assert (total, bad) == (20, 0)
        assert len(calls) == 3          # not 20: the other 17 are never parsed

    def test_page_skips_and_counts_an_off_contract_row(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:                     # indexed, fails the contract
            f.write('{"id":"nope","name":"","age":300,"note":"n"}\n')
        records, total, bad = s.page(10)
        assert [r.id for r in records] == ["ok"] and (total, bad) == (2, 1)

    def test_unparseable_lines_counted_without_a_fold(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{bad\n{worse\n")
        assert s.page(10)[2] == 2

    def test_bad_count_survives_a_warm_sidecar(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{bad\n")
        assert s.page(10)[2] == 1
        assert json.loads(Path(s.idxpath).read_text())["bad"] == 1
        fresh = store.Store(s.path, s.model, key=s.key)  # cold process, warm index
        assert fresh.page(10)[2] == 1

    def test_sidecar_without_a_bad_count_is_a_cold_cache(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{bad\n")
        s.keys()
        d = json.loads(Path(s.idxpath).read_text())
        del d["bad"]                                     # a sidecar from an older build
        Path(s.idxpath).write_text(json.dumps(d))
        fresh = store.Store(s.path, s.model, key=s.key)
        assert fresh.page(10)[2] == 1                    # rebuilt, not trusted blindly

    def test_compact_clears_the_bad_count(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{bad\n")
        assert s.page(10)[2] == 1
        s.compact()
        assert s.page(10)[2] == 0

    def test_page_on_a_missing_store(self, tmp_path):
        assert make_store(tmp_path).page(10) == ([], 0, 0)

    def test_page_with_a_zero_limit_still_reports_the_total(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ok", "name": "A", "age": 1, "note": "n"})
        assert s.page(0) == ([], 1, 0)

    def test_keys_caps_output_and_reveals_the_total(self, workspace, cli):
        for i in range(5):
            cli("-f", "u.jsonl", "put", rec(id=f"u{i}"))
        code, out, _ = cli("-f", "u.jsonl", "keys", "--limit", "2")
        assert code == 0 and "count: 2 of 5 total" in out
        assert "keys[2]: u0,u1" in out
        assert "keys --limit 5" in out

    def test_keys_under_the_limit_says_nothing_about_paging(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        _, out, _ = cli("-f", "u.jsonl", "keys")
        assert "count: 1 of 1 total" in out and "--limit" not in out

    def test_keys_default_limit_matches_list(self, workspace, cli):
        for i in range(store.DEFAULT_LIMIT + 5):
            cli("-f", "u.jsonl", "put", rec(id=f"u{i:04d}"))
        _, out, _ = cli("-f", "u.jsonl", "keys")
        assert f"count: {store.DEFAULT_LIMIT} of {store.DEFAULT_LIMIT + 5} total" in out

    def test_list_does_not_fold_the_whole_store(self, workspace, cli, monkeypatch):
        for i in range(10):
            cli("-f", "u.jsonl", "put", rec(id=f"u{i}"))

        def boom(self):                 # the fold is what blew up memory
            raise AssertionError("list must not call Store.load()")
        monkeypatch.setattr(store.Store, "load", boom)

        code, out, _ = cli("-f", "u.jsonl", "list", "--limit", "2")
        assert code == 0 and "count: 2 of 10 total" in out

    def test_list_hides_the_cap_hint_when_the_limit_was_not_the_constraint(
            self, workspace, cli):
        """A row the page skipped is not a row a bigger --limit would reveal."""
        cli("-f", "u.jsonl", "put", rec())
        with open("u.jsonl", "a") as f:
            f.write('{"id":"nope","name":"","age":300,"note":"n"}\n')
        _, out, _ = cli("-f", "u.jsonl", "list")
        assert "count: 1 of 2 total" in out and "bad: 1" in out
        assert "--limit" not in out and "validate" in out

    def test_list_rejects_unknown_fields_before_reading(self, workspace, cli, monkeypatch):
        cli("-f", "u.jsonl", "put", rec())
        monkeypatch.setattr(store.Store, "page",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("read before validating --fields")))
        code, out, _ = cli("-f", "u.jsonl", "list", "--fields", "nope")
        assert code == 2 and "unknown field" in out


class TestConcurrency:
    def test_compaction_does_not_lose_a_concurrent_append(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text(CONTRACT)
        path = tmp_path / "d.jsonl"
        driver = tmp_path / "drv.py"
        driver.write_text(textwrap.dedent(f"""
            import sys, time, importlib.util
            sp = importlib.util.spec_from_file_location("s", {str(DATAFILE_PY)!r})
            st = importlib.util.module_from_spec(sp)
            sp.loader.exec_module(st)
            model, key = st.load_contract({str(c)!r})
            s = st.Store({str(path)!r}, model, key=key)
            if sys.argv[1] == "compactor":
                orig = st.Store._load_unlocked
                def slow(self):
                    r = orig(self)
                    time.sleep(1.0)
                    return r
                st.Store._load_unlocked = slow
                s.compact()
            else:
                time.sleep(0.4)
                s.put({{"id": "zoe", "name": "Z", "age": 1, "note": "n"}})
        """))
        model, key = store.load_contract(str(c))
        s = store.Store(str(path), model, key=key)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        procs = [subprocess.Popen([sys.executable, str(driver), role])
                 for role in ("compactor", "writer")]
        for p in procs:
            assert p.wait(timeout=60) == 0
        fresh = store.Store(str(path), model, key=key)
        assert sorted(fresh.keys()) == ["ada", "zoe"]


# ----------------------------------------------------------------- CLI (§6)

class TestCLI:
    def test_home_is_content_not_help(self, workspace, cli):
        code, out, err = cli()
        assert code == 0 and err == ""
        assert out.startswith("bin: ")
        assert "description: " in out and "usage:" not in out

    def test_home_empty_state_is_definitive(self, workspace, cli):
        assert "0 jsonl stores found in" in cli()[1]

    def test_put_get_roundtrip(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl", "put", rec())
        assert code == 0 and "ada,created" in out
        code, out, _ = cli("-f", "u.jsonl", "get", "ada")
        assert code == 0 and "name: Ada" in out

    def test_put_is_idempotent(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "put", rec())
        assert code == 0 and "ada,unchanged" in out

    def test_idempotent_put_does_not_grow_the_log(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        before = Path("u.jsonl").stat().st_size
        cli("-f", "u.jsonl", "put", rec())
        assert Path("u.jsonl").stat().st_size == before

    def test_put_reports_update(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "put", rec(name="Ada L"))
        assert code == 0 and "ada,updated" in out

    def test_delete_is_idempotent(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        assert cli("-f", "u.jsonl", "delete", "ada")[0] == 0
        code, out, _ = cli("-f", "u.jsonl", "delete", "ada")
        assert code == 0 and "already absent (no-op)" in out

    @pytest.mark.parametrize("payload,field", [
        (rec(id="BAD!"), "id:"), (rec(name=""), "name:"),
        (rec(age=999), "age:"), (rec(email="nope"), "email:"),
        (rec(role="wizard"), "role:"),
    ])
    def test_contract_violations_exit_2(self, workspace, cli, payload, field):
        code, out, err = cli("-f", "u.jsonl", "put", payload)
        assert code == 2
        assert err == ""
        assert "code: CONTRACT_VIOLATION" in out and field in out

    def test_rejected_record_is_not_written(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(age=999))
        assert not Path("u.jsonl").exists() or Path("u.jsonl").read_text() == ""

    def test_extra_field_forbidden(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl", "put", rec(oops=1))
        assert code == 2 and "Extra inputs are not permitted" in out

    def test_errors_go_to_stdout_never_stderr(self, workspace, cli):
        code, out, err = cli("-f", "u.jsonl", "get", "nobody")
        assert code == 1 and err == ""
        assert "error: " in out and "code: NOT_FOUND" in out

    def test_unknown_flag_rejected_with_valid_flags(self, workspace, cli):
        code, out, err = cli("-f", "u.jsonl", "list", "--stat", "x")
        assert code == 2 and err == ""
        assert "code: USAGE_ERROR" in out
        assert "--fields" in out and "--limit" in out

    def test_abbreviated_flag_rejected(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "get", "ada", "--ful")
        assert code == 2 and "USAGE_ERROR" in out

    def test_unknown_command_rejected(self, workspace, cli):
        assert cli("-f", "u.jsonl", "frobnicate")[0] == 2

    def test_unknown_field_rejected(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "list", "--fields", "nope")
        assert code == 2 and "unknown field" in out

    def test_bad_json_argument(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl", "put", "{oops")
        assert code == 2 and "invalid json" in out

    def test_put_with_no_input(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl", "put")
        assert code == 2 and "USAGE_ERROR" in out

    def test_list_default_schema_prefers_required(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        _, out, _ = cli("-f", "u.jsonl", "list")
        assert "u[1]{id,name,age,note}:" in out
        assert "email" not in out

    def test_list_reports_total_and_reveals_cap(self, workspace, cli):
        for i in range(3):
            cli("-f", "u.jsonl", "put", rec(id=f"u{i}"))
        _, out, _ = cli("-f", "u.jsonl", "list", "--limit", "2")
        assert "count: 2 of 3 total" in out
        assert "--limit 3" in out

    def test_list_surfaces_bad_line_count(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        with open("u.jsonl", "a") as f:
            f.write("{bad\n")
        _, out, _ = cli("-f", "u.jsonl", "list")
        assert "bad: 1" in out

    def test_get_truncates_with_size_hint(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(note="x" * 3000))
        _, out, _ = cli("-f", "u.jsonl", "get", "ada")
        assert "truncated[1]{field,shown,total}:" in out
        assert "note,800,3000" in out
        assert "--full" in out
        assert "truncated" not in cli("-f", "u.jsonl", "get", "ada", "--full")[1]

    def test_validate_exit_codes(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        assert cli("-f", "u.jsonl", "validate")[0] == 0
        with open("u.jsonl", "a") as f:
            f.write("{bad\n")
        code, out, _ = cli("-f", "u.jsonl", "validate")
        assert code == 0 and "bad: 1" in out
        assert cli("-f", "u.jsonl", "validate", "--strict")[0] == 1

    def test_repair_then_clean(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        with open("u.jsonl", "a") as f:
            f.write("{bad\n")
        code, out, _ = cli("-f", "u.jsonl", "repair")
        assert code == 0 and "quarantined: 1" in out
        assert "bad: 0" in cli("-f", "u.jsonl", "validate")[1]

    def test_empty_states(self, workspace, cli):
        Path("u.jsonl").write_text("")
        assert "0 records in u.jsonl" in cli("-f", "u.jsonl", "list")[1]
        assert "0 records in u.jsonl" in cli("-f", "u.jsonl", "keys")[1]

    def test_missing_contract_is_usage_error(self, workspace, cli):
        code, out, _ = cli("-f", "orphan.jsonl", "list")
        assert code == 2 and "no contract given" in out

    def test_contract_autodetected_by_convention(self, workspace, cli):
        assert cli("-f", "users.jsonl", "put", rec())[0] == 0

    def test_broken_contract_is_exit_2(self, workspace, cli):
        Path("schema-bad.yaml").write_text("fields: {id: {type: nope}}")
        code, out, _ = cli("-f", "u.jsonl", "-c", "schema-bad.yaml", "list")
        assert code == 2 and "CONTRACT_ERROR" in out

    def test_schema_lists_rules(self, workspace, cli):
        code, out, _ = cli("-f", "users.jsonl", "schema")
        assert code == 0 and "model: User" in out and "ge=0" in out

    def test_schema_json_schema_is_valid_json(self, workspace, cli):
        code, out, _ = cli("-f", "users.jsonl", "schema", "--json-schema")
        assert code == 0
        payload = out.split("schema: ", 1)[1].strip()
        if payload.startswith('"'):
            payload = json.loads(payload)
        assert json.loads(payload)["additionalProperties"] is False

    def test_stdin_bulk(self, workspace, cli, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            rec(id="aa") + "\n" + rec(id="bb") + "\n"))
        code, out, _ = cli("-f", "u.jsonl", "put", "-")
        assert code == 0 and "written: 2" in out

    def test_set_pairs_coerce_types(self, workspace, cli):
        code, _, _ = cli("-f", "u.jsonl", "put", "--set", "id=zz", "--set",
                         "name=Z", "--set", "age=5", "--set", "note=n")
        assert code == 0
        assert "age: 5" in cli("-f", "u.jsonl", "get", "zz")[1]

    def test_set_requires_key_value(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl", "put", "--set", "oops")
        assert code == 2 and "KEY=VALUE" in out

    def test_version(self, workspace, cli):
        code, out, _ = cli("--version")
        assert code == 0 and out.strip() == store.VERSION

    def test_suggestions_use_the_invoked_name(self, workspace, cli, monkeypatch):
        monkeypatch.setattr(store, "PROG", "datafile")
        _, out, _ = cli("-f", "users.jsonl", "get", "nobody")
        assert "Run `datafile " in out and "datafile.py" not in out


# ------------------------------------------------------------- discovery (§8)

class TestDiscovery:
    def _seed(self, cli, rel):
        d = Path(rel).parent
        d.mkdir(parents=True, exist_ok=True)
        (d / "schema-x.yaml").write_text(CONTRACT)
        assert cli("-f", rel, "-c", str(d / "schema-x.yaml"), "put", rec())[0] == 0

    def test_finds_nested_store(self, workspace, cli):
        self._seed(cli, "data/prod/x.jsonl")
        assert "data/prod/x.jsonl" in cli()[1]

    def test_depth_limit_and_all(self, workspace, cli):
        self._seed(cli, "a/b/c/d/e/x.jsonl")
        assert "x.jsonl" not in cli("stores")[1]
        assert "x.jsonl" in cli("stores", "--all")[1]
        assert "x.jsonl" in cli("stores", "--depth", "6")[1]

    def test_skips_heavy_directories(self, workspace, cli):
        self._seed(cli, "node_modules/pkg/x.jsonl")
        assert "node_modules" not in cli("stores", "--all")[1]

    def test_skips_hidden_directories(self, workspace, cli):
        self._seed(cli, ".git/objects/x.jsonl")
        assert ".git" not in cli("stores", "--all")[1]

    def test_empty_state(self, workspace, cli):
        code, out, _ = cli("stores")
        assert code == 0 and "0 jsonl stores found under" in out

    def test_reports_record_counts(self, workspace, cli):
        self._seed(cli, "data/x.jsonl")
        assert "data/x.jsonl,data/schema-x.yaml,1" in cli("stores")[1]


# ----------------------------------------------------------------- skill (§7)

class TestSkill:
    def test_generate_then_check_is_clean(self, workspace, cli):
        assert cli("skill", "--out", "S.md")[0] == 0
        assert cli("skill", "--check", "--out", "S.md")[0] == 0

    def test_check_fails_when_missing(self, workspace, cli):
        code, out, _ = cli("skill", "--check", "--out", "S.md")
        assert code == 1 and "SKILL_STALE" in out

    def test_check_fails_when_stale(self, workspace, cli):
        cli("skill", "--out", "S.md")
        Path("S.md").write_text(Path("S.md").read_text() + "drift\n")
        code, out, _ = cli("skill", "--check", "--out", "S.md")
        assert code == 1 and "out of date" in out

    def test_regenerate_is_idempotent(self, workspace, cli):
        cli("skill", "--out", "S.md")
        assert "unchanged (no-op)" in cli("skill", "--out", "S.md")[1]

    def test_tracks_cli_changes(self, workspace, cli):
        cli("skill", "--out", "S.md")
        before = Path("S.md").read_text()
        original = store.build_parser

        def patched():
            parser, subs = original()
            subs["keys"].add_argument("--sorted", action="store_true",
                                      help="sort ids")
            return parser, subs

        store.build_parser = patched
        try:
            assert cli("skill", "--check", "--out", "S.md")[0] == 1
            cli("skill", "--out", "S.md")
            assert "--sorted" in Path("S.md").read_text()
        finally:
            store.build_parser = original
        cli("skill", "--out", "S.md")
        assert Path("S.md").read_text() == before

    def test_carries_no_live_state(self, workspace, cli):
        Path("secret-store.jsonl").write_text("")
        cli("skill", "--out", "S.md")
        assert "secret-store" not in Path("S.md").read_text()

    def test_frontmatter_is_trigger_shaped(self, workspace, cli):
        cli("skill", "--out", "S.md")
        text = Path("S.md").read_text()
        assert text.startswith("---\nname: datafile\ndescription: >-")
        assert "Use when" in text.split("---")[1]

    def test_documents_every_command(self, workspace, cli):
        cli("skill", "--out", "S.md")
        text = Path("S.md").read_text()
        _, subs = store.build_parser()
        for name in subs:
            assert f"| `{name}` |" in text


# ----------------------------------------------------------------- setup (§7)

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


class TestSetup:
    def _settings(self, home):
        return json.loads((home / ".claude" / "settings.json").read_text())

    def _commands(self, home):
        groups = self._settings(home)["hooks"]["SessionStart"]
        return [h["command"] for g in groups for h in g["hooks"]]

    def test_status_before_install(self, fake_home, cli):
        code, out, _ = cli("setup", "--status")
        assert code == 0
        assert out.count(",absent,") == 3

    def test_install_all_three(self, fake_home, cli):
        assert cli("setup")[0] == 0
        assert (fake_home / ".claude" / "settings.json").exists()
        assert (fake_home / ".codex" / "hooks.json").exists()
        assert (fake_home / ".config" / "opencode" / "plugins"
                / "axi-datafile.js").exists()

    def test_install_is_idempotent(self, fake_home, cli):
        cli("setup")
        _, out, _ = cli("setup")
        assert out.count("unchanged (no-op)") == 3

    def test_hook_entry_shape(self, fake_home, cli):
        cli("setup", "--app", "claude")
        group = self._settings(fake_home)["hooks"]["SessionStart"][0]
        assert group["matcher"] == ""
        assert group["hooks"][0]["type"] == "command"
        assert group["hooks"][0]["timeout"] == store.HOOK_TIMEOUT

    def test_path_repair_does_not_duplicate(self, fake_home, cli):
        cli("setup", "--app", "claude")
        settings = self._settings(fake_home)
        settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] = \
            "/old/uv run /old/datafile.py"
        (fake_home / ".claude" / "settings.json").write_text(json.dumps(settings))
        _, out, _ = cli("setup", "--app", "claude")
        assert "repaired" in out
        assert len(self._commands(fake_home)) == 1
        assert "/old/" not in self._commands(fake_home)[0]

    def test_preserves_unrelated_config(self, fake_home, cli):
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(json.dumps({
            "theme": "dark",
            "hooks": {
                "SessionStart": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "/usr/bin/other-tool"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "guard"}]}],
            },
        }))
        cli("setup", "--app", "claude")
        data = self._settings(fake_home)
        assert data["theme"] == "dark"
        assert data["hooks"]["PreToolUse"]
        assert "/usr/bin/other-tool" in self._commands(fake_home)
        cli("setup", "--app", "claude", "--uninstall")
        data = self._settings(fake_home)
        assert data["theme"] == "dark"
        assert data["hooks"]["PreToolUse"]
        assert self._commands(fake_home) == ["/usr/bin/other-tool"]

    def test_uninstall_is_idempotent(self, fake_home, cli):
        cli("setup")
        assert cli("setup", "--uninstall")[0] == 0
        _, out, _ = cli("setup", "--uninstall")
        assert out.count("already absent (no-op)") == 3

    def test_cross_form_marker_matching(self, fake_home, cli):
        cli("setup", "--app", "claude")
        settings = self._settings(fake_home)
        settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "datafile"
        (fake_home / ".claude" / "settings.json").write_text(json.dumps(settings))
        cli("setup", "--app", "claude", "--uninstall")
        assert "hooks" not in self._settings(fake_home)

    def test_legacy_flat_session_start_is_migrated(self, fake_home, cli):
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"session_start": [
                {"type": "command", "command": "/x/uv run /y/datafile.py"}]}}))
        cli("setup", "--app", "claude")
        data = self._settings(fake_home)
        assert "session_start" not in data["hooks"]
        assert len(self._commands(fake_home)) == 1

    def test_unreadable_settings_is_structured_error(self, fake_home, cli):
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text("{not json")
        code, out, _ = cli("setup", "--app", "claude")
        assert code == 1 and "SETUP_ERROR" in out

    def test_bad_app_is_usage_error(self, fake_home, cli):
        assert cli("setup", "--app", "emacs")[0] == 2

    def test_opencode_plugin_is_marked_and_removable(self, fake_home, cli):
        cli("setup", "--app", "opencode")
        p = (fake_home / ".config" / "opencode" / "plugins"
             / "axi-datafile.js")
        assert p.read_text().startswith("// " + store.OPENCODE_PREFIX)
        cli("setup", "--app", "opencode", "--uninstall")
        assert not p.exists()

    def test_project_scope_writes_locally(self, fake_home, cli, tmp_path):
        cli("setup", "--app", "claude", "--scope", "project")
        assert (tmp_path / ".claude" / "settings.json").exists()
        assert not (fake_home / ".claude").exists()


class TestCodexToml:
    @pytest.mark.parametrize("before,expect_flag,changed", [
        ("", True, True),
        ("[features]\nhooks = true\n", True, False),
        ("[features]\nhooks = false\n", True, True),
        ("[features]\nweb_search = true\n", True, True),
        ('model = "gpt-5"\n', True, True),
    ])
    def test_flag_ends_up_true(self, before, expect_flag, changed):
        out, did = store.codex_config_update(before)
        assert did == changed
        assert "hooks = true" in out
        assert store.codex_config_update(out)[1] is False   # converges

    def test_preserves_other_sections(self):
        before = ('model = "gpt-5"\n\n[features]\nweb_search = true\n\n'
                  '[mcp_servers.example]\ncommand = "foo"\n')
        out, _ = store.codex_config_update(before)
        assert 'model = "gpt-5"' in out
        assert "web_search = true" in out
        assert "[mcp_servers.example]" in out
        assert 'command = "foo"' in out
        # the flag must land inside [features], before the next table header
        body = out.split("[features]")[1]
        assert body.index("hooks = true") < body.index("[mcp_servers.example]")


# ------------------------------------------------------- subprocess smoke (§10)

class TestInvocation:
    def test_version_fast_path_without_dependencies(self):
        """--version must answer before pydantic/yaml are imported."""
        r = subprocess.run([sys.executable, str(DATAFILE_PY), "--version"],
                           capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": "/nonexistent"})
        assert r.returncode == 0
        assert r.stdout.strip() == store.VERSION

    @pytest.mark.parametrize("flag", ["-v", "-V", "--version"])
    def test_all_version_flags(self, flag):
        r = subprocess.run([sys.executable, str(DATAFILE_PY), flag],
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip() == store.VERSION

    def test_script_has_uv_shebang_and_is_executable(self):
        assert DATAFILE_PY.read_text().splitlines()[0] == \
            "#!/usr/bin/env -S uv run --script"
        assert os.access(DATAFILE_PY, os.X_OK)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", *sys.argv[1:]]))


# ===================================================================
# Coverage gaps: error paths, edge cases, and defensive branches
# ===================================================================

class TestStoreEdges:
    def test_key_must_exist_on_model(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text(CONTRACT)
        model, _ = store.load_contract(str(c))
        with pytest.raises(ValueError, match="has no field"):
            store.Store(str(tmp_path / "d.jsonl"), model, key="nope")

    def test_blank_lines_are_skipped(self, tmp_path):
        s = make_store(tmp_path)
        with open(s.path, "w") as f:
            f.write("\n")
            f.write('{"id":"ada","name":"A","age":1,"note":"n"}\n')
            f.write("   \n")
        alive, bad = s.load()
        assert bad == [] and list(alive) == ["ada"]

    def test_get_returns_none_for_contract_violating_row(self, tmp_path):
        """keys() indexes any parseable row; get() still enforces the contract."""
        s = make_store(tmp_path)
        with open(s.path, "w") as f:
            f.write('{"id":"bad","name":"","age":300,"note":"n"}\n')
        assert s.keys() == ["bad"]
        assert s.get("bad") is None

    def test_atomic_write_cleans_up_on_failure(self, tmp_path, monkeypatch):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        before = set(os.listdir(tmp_path))

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            s.compact()
        assert set(os.listdir(tmp_path)) == before      # no .tmp- left behind


class TestContractEdges:
    def _load(self, tmp_path, body):
        p = tmp_path / "c.yaml"
        p.write_text(body)
        return store.load_contract(str(p))

    def test_field_without_type(self, tmp_path):
        with pytest.raises(store.ContractError, match="missing 'type'"):
            self._load(tmp_path, "fields: {id: {required: false}}")

    def test_dict_type(self, tmp_path):
        model, _ = self._load(
            tmp_path, "fields:\n  id: str\n  props: {type: dict, default: {}}")
        assert model.model_validate({"id": "a", "props": {"x": 1}}).props == {"x": 1}

    def test_invalid_yaml(self, tmp_path):
        with pytest.raises(store.ContractError, match="invalid YAML"):
            self._load(tmp_path, "fields: {id: str\n  bad: [unclosed")

    def test_explicit_null_default_widens_type(self, tmp_path):
        model, _ = self._load(
            tmp_path, "fields:\n  id: str\n  x: {type: int, default: null}")
        assert model.model_validate({"id": "a"}).x is None
        assert model.model_validate({"id": "a", "x": None}).x is None

    def test_nested_list_item_spec(self, tmp_path):
        model, _ = self._load(
            tmp_path,
            "fields:\n  id: str\n  xs: {type: list, items: {type: int, ge: 0}}")
        assert model.model_validate({"id": "a", "xs": [1, 2]}).xs == [1, 2]


class TestToonEdges:
    def test_non_finite_numbers_become_null(self):
        assert store.toon({"k": float("nan")}) == "k: null"
        assert store.toon({"k": float("inf")}) == "k: null"
        assert store.toon({"k": float("-inf")}) == "k: null"

    def test_empty_dict(self):
        assert store.toon({"k": {}}) == "k: {}"

    def test_multiline_element_needing_quotes(self):
        out = store.toon({"help": ["a, b", "  padded  "]})
        assert out == 'help[2]:\n  a, b\n  "  padded  "'

    def test_mixed_list_falls_back_to_one_per_line(self):
        out = store.toon({"k": [1, {"a": 1}]})
        assert out.startswith("k[2]:")
        assert len(out.splitlines()) == 3

    def test_table_with_non_scalar_cell_is_not_tabular(self):
        out = store.toon({"k": [{"a": [1, 2]}, {"a": [3]}]})
        assert "{a}" not in out

    def test_key_needing_quotes(self):
        assert store.toon({"a:b": 1}) == '"a:b": 1'


class TestCLIEdges:
    def test_help_exits_zero(self, workspace, cli):
        with pytest.raises(SystemExit) as e:
            cli("list", "--help")
        assert e.value.code == 0

    def test_no_subcommand_falls_back_to_home(self, workspace, cli):
        code, out, _ = cli("-f", "u.jsonl")
        assert code == 0 and out.startswith("bin: ")

    def test_file_required(self, workspace, cli):
        code, out, _ = cli("list")
        assert code == 2 and "--file is required" in out

    def test_keys_non_empty(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "keys")
        assert code == 0 and "count: 1" in out and "keys[1]: ada" in out

    def test_compact_reports_reclaimed_bytes(self, workspace, cli):
        for i in range(4):
            cli("-f", "u.jsonl", "put", rec(name=f"A{i}"))
        code, out, _ = cli("-f", "u.jsonl", "compact")
        assert code == 0 and "kept: 1" in out
        reclaimed = int(out.split("reclaimed_bytes: ")[1].split()[0])
        assert reclaimed > 0

    def test_empty_list_reports_bad_lines(self, workspace, cli):
        Path("u.jsonl").write_text("{bad\n")
        code, out, _ = cli("-f", "u.jsonl", "list")
        assert code == 0 and "0 records" in out and "bad: 1" in out
        assert "validate" in out

    def test_list_non_scalar_cell_is_compact_json(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(tags=["sre", "oncall"]))
        _, out, _ = cli("-f", "u.jsonl", "list", "--fields", "id,tags")
        assert '["sre","oncall"]' in out.replace("\\", "")

    def test_list_clips_long_cells_and_says_so(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(note="y" * 500))
        _, out, _ = cli("-f", "u.jsonl", "list")
        assert "..." in out and "--full" in out

    def test_stdin_bad_json_is_usage_error(self, workspace, cli, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{oops\n"))
        code, out, _ = cli("-f", "u.jsonl", "put", "-")
        assert code == 2 and "stdin line 1" in out

    def test_schema_falls_back_for_opaque_metadata(self, workspace, cli,
                                                   monkeypatch):
        class Slotted:
            __slots__ = ()

        from typing import Annotated

        from pydantic import BaseModel as BM

        class M(BM):
            id: Annotated[str, Slotted()]

        real = store.load_contract
        monkeypatch.setattr(store, "load_contract", lambda p: (M, "id"))
        try:
            code, out, _ = cli("-f", "u.jsonl", "schema")
        finally:
            monkeypatch.setattr(store, "load_contract", real)
        assert code == 0 and "Slotted" in out

    def test_broken_pipe_is_silent_success(self, workspace, cli, monkeypatch):
        monkeypatch.setitem(store.COMMANDS, "keys",
                            lambda a: (_ for _ in ()).throw(BrokenPipeError()))
        assert cli("-f", "u.jsonl", "keys")[0] == 0

    def test_keyboard_interrupt_exits_one(self, workspace, cli, monkeypatch):
        monkeypatch.setitem(store.COMMANDS, "keys",
                            lambda a: (_ for _ in ()).throw(KeyboardInterrupt()))
        assert cli("-f", "u.jsonl", "keys")[0] == 1

    def test_unexpected_exception_is_structured(self, workspace, cli, monkeypatch):
        monkeypatch.setitem(store.COMMANDS, "keys",
                            lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
        code, out, err = cli("-f", "u.jsonl", "keys")
        assert code == 1 and err == ""
        assert "INTERNAL_ERROR" in out and "RuntimeError: boom" in out
        assert "Traceback" not in out

    def test_prog_is_taken_from_argv_when_run_as_a_program(self, tmp_path,
                                                           monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/datafile", "--version"])
        try:
            assert store.main() == 0
            assert store.PROG == "datafile"
        finally:
            store.PROG = "datafile.py"
        capsys.readouterr()


class TestDiscoveryEdges:
    def _pair(self, d, stem):
        d.mkdir(parents=True, exist_ok=True)
        (d / f"schema-{stem}.yaml").write_text(CONTRACT)
        (d / f"{stem}.jsonl").write_text(
            '{"id":"ada","name":"A","age":1,"note":"n"}\n')

    def test_cap_is_revealed_in_home_and_stores(self, workspace, cli):
        for i in range(store.DISCOVER_CAP + 5):
            self._pair(workspace, f"s{i:02d}")
        _, home, _ = cli()
        assert f"Showing the first {store.DISCOVER_CAP} stores" in home
        _, out, _ = cli("stores")
        assert "see every store" in out
        _, everything, _ = cli("stores", "--all")
        assert everything.count(".jsonl,") == store.DISCOVER_CAP + 5

    def test_invalid_contract_is_flagged_not_fatal(self, workspace, cli):
        (workspace / "schema-broken.yaml").write_text("fields: {id: {type: nope}}")
        (workspace / "broken.jsonl").write_text("")
        code, out, _ = cli("stores")
        assert code == 0 and "(invalid)" in out


class TestSetupEdges:
    def _write_claude(self, home, payload):
        (home / ".claude").mkdir(exist_ok=True)
        (home / ".claude" / "settings.json").write_text(json.dumps(payload))

    def _read_claude(self, home):
        return json.loads((home / ".claude" / "settings.json").read_text())

    def test_legacy_entry_removed_but_siblings_kept_on_install(self, fake_home, cli):
        self._write_claude(fake_home, {"hooks": {"session_start": [
            {"type": "command", "command": "/x/uv run /y/datafile.py"},
            {"type": "command", "command": "/usr/bin/other"}]}})
        cli("setup", "--app", "claude")
        data = self._read_claude(fake_home)
        assert data["hooks"]["session_start"] == [
            {"type": "command", "command": "/usr/bin/other"}]

    def test_legacy_entry_removed_on_uninstall(self, fake_home, cli):
        self._write_claude(fake_home, {"hooks": {"session_start": [
            {"type": "command", "command": "/x/uv run /y/datafile.py"}]}})
        cli("setup", "--app", "claude", "--uninstall")
        assert "hooks" not in self._read_claude(fake_home)

    def test_legacy_uninstall_keeps_siblings(self, fake_home, cli):
        self._write_claude(fake_home, {"hooks": {"session_start": [
            {"type": "command", "command": "/x/uv run /y/datafile.py"},
            {"type": "command", "command": "/usr/bin/other"}]}})
        cli("setup", "--app", "claude", "--uninstall")
        assert self._read_claude(fake_home)["hooks"]["session_start"] == [
            {"type": "command", "command": "/usr/bin/other"}]

    def test_uninstall_keeps_other_hooks_in_the_same_group(self, fake_home, cli):
        self._write_claude(fake_home, {"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": "/x/datafile.py"},
                {"type": "command", "command": "/usr/bin/other"}]}]}})
        cli("setup", "--app", "claude", "--uninstall")
        groups = self._read_claude(fake_home)["hooks"]["SessionStart"]
        assert [h["command"] for g in groups for h in g["hooks"]] == ["/usr/bin/other"]

    def test_uv_missing_is_setup_error(self, fake_home, cli, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda n: None)
        code, out, _ = cli("setup", "--status")
        assert code == 1 and "uv not found" in out

    def test_uses_path_alias_when_one_resolves_here(self, fake_home, cli,
                                                    tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "datafile").symlink_to(DATAFILE_PY)
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
        cli("setup", "--app", "claude")
        cmds = [h["command"]
                for g in self._read_claude(fake_home)["hooks"]["SessionStart"]
                for h in g["hooks"]]
        assert cmds == ["datafile"]

    def test_unreadable_path_entries_are_skipped(self, monkeypatch):
        monkeypatch.setenv("PATH", "/does/not/exist")
        assert store._path_alias(str(DATAFILE_PY)) is None

    def test_path_scan_survives_oserror(self, tmp_path, monkeypatch):
        d = tmp_path / "bin"
        d.mkdir()
        (d / "thing").write_text("x")
        monkeypatch.setenv("PATH", str(d))
        real = os.path.realpath

        def boom(p):
            if p.endswith("thing"):
                raise OSError("gone")
            return real(p)

        monkeypatch.setattr(os.path, "realpath", boom)
        monkeypatch.setattr(os, "access", lambda *a: True)
        assert store._path_alias(str(DATAFILE_PY)) is None

    def test_listdir_oserror_is_skipped(self, tmp_path, monkeypatch):
        d = tmp_path / "bin"
        d.mkdir()
        monkeypatch.setenv("PATH", str(d))
        monkeypatch.setattr(os, "listdir",
                            lambda p: (_ for _ in ()).throw(OSError("nope")))
        assert store._path_alias(str(DATAFILE_PY)) is None


class TestCodexTomlEdges:
    def test_array_of_tables_and_malformed_headers(self):
        before = "[[servers]]\nname = \"a\"\n[not a header\n[features]\nx = 1\n"
        out, changed = store.codex_config_update(before)
        assert changed and "hooks = true" in out
        assert "[[servers]]" in out and "[not a header" in out

    def test_commented_header_is_handled(self):
        out, _ = store.codex_config_update("[features]  # comment\nx = 1\n")
        assert "hooks = true" in out

    def test_commented_flag_value(self):
        out, changed = store.codex_config_update(
            "[features]\nhooks = false  # off\n")
        assert changed and "hooks = true" in out

    def test_crlf_preserved(self):
        out, _ = store.codex_config_update("[features]\r\nx = 1\r\n")
        assert "\r\n" in out and "hooks = true" in out

    def test_whitespace_only_content(self):
        out, changed = store.codex_config_update("   \n\n")
        assert changed and out == "[features]\nhooks = true\n"


# ===================================================================
# Remaining branches: the "false" side of conditionals, and __main__
# ===================================================================

class TestBranchCompletion:
    def test_repair_with_nothing_to_quarantine(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        assert s.repair() == (1, 0)
        assert not Path(s.path + ".quarantine").exists()

    def test_cli_repair_clean_omits_quarantine_hint(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        code, out, _ = cli("-f", "u.jsonl", "repair")
        assert code == 0 and "quarantined: 0" in out
        assert "quarantine:" not in out and "help" not in out

    def test_emit_of_empty_payload_prints_nothing(self, capsys):
        store.emit({})
        assert capsys.readouterr().out == ""

    def test_error_without_suggestions_has_no_help(self, capsys):
        store.fail(store.AxiError("plain", "SOME_CODE"))
        out = capsys.readouterr().out
        assert "error: plain" in out and "help" not in out

    def test_parser_exit_nonzero_raises_usage(self):
        p = store.AxiParser(prog="x")
        with pytest.raises(store._Usage):
            p.exit(2, "bad")
        with pytest.raises(store._Usage):
            p.exit(2)

    def test_store_without_a_contract_is_listed_as_dash(self, workspace, cli):
        Path("orphan.jsonl").write_text("")
        _, out, _ = cli("stores")
        assert "orphan.jsonl,null,0" in out

    def test_stdin_blank_lines_are_skipped(self, workspace, cli, monkeypatch):
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO("\n" + rec() + "\n\n"))
        code, out, _ = cli("-f", "u.jsonl", "put", "-")
        assert code == 0 and "written: 1" in out

    def test_legacy_key_with_no_managed_entry_is_left_alone(self):
        settings = {"hooks": {"session_start": [
            {"type": "command", "command": "/usr/bin/other"}]}}
        updated, status = store._hook_update(settings, "cmd")
        assert updated["hooks"]["session_start"] == settings["hooks"]["session_start"]
        assert status == "installed"

    def test_removal_is_a_noop_when_nothing_is_managed(self):
        settings = {"hooks": {
            "session_start": [{"type": "command", "command": "/usr/bin/other"}],
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": "/usr/bin/other"}]}]}}
        updated, changed = store._hook_removal(settings)
        assert changed is False and updated is settings

    def test_removal_with_no_hooks_key(self):
        assert store._hook_removal({"theme": "dark"}) == ({"theme": "dark"}, False)

    def test_toml_mismatched_brackets_are_not_headers(self):
        out, changed = store.codex_config_update(
            "[oops]]\nx = 1\n[features]\ny = 2\n")
        assert changed and "hooks = true" in out
        assert "[oops]]" in out

    def test_hook_argv_falls_back_without_a_shebang(self, tmp_path, monkeypatch):
        target = tmp_path / "plain.py"
        target.write_text("print(1)\n")
        target.chmod(0o755)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "plain").symlink_to(target)
        monkeypatch.setenv("PATH", str(bindir))
        assert store._hook_argv("/uv", str(target)) == ["/uv", "run", str(target)]

    def test_skill_handles_a_subcommand_without_examples(self, workspace, cli,
                                                          monkeypatch):
        original = store.build_parser

        def patched():
            parser, subs = original()
            sub = parser._subparsers._group_actions[0]
            sub.add_parser("bare", help="no examples")
            subs["bare"] = sub.choices["bare"]
            return parser, subs

        monkeypatch.setattr(store, "build_parser", patched)
        assert cli("skill", "--out", "S.md")[0] == 0
        text = Path("S.md").read_text()
        assert "| `bare` |" in text
        assert "**bare**" not in text          # no examples block emitted


class TestModuleEntryPoints:
    """Lines that only run when datafile.py is executed as a program."""

    SOURCE = DATAFILE_PY.read_text()
    CODE = compile(SOURCE, str(DATAFILE_PY), "exec")

    def _run_as_main(self, argv):
        g = {"__name__": "__main__", "__file__": str(DATAFILE_PY),
             "__builtins__": __builtins__}
        real = sys.argv
        sys.argv = argv
        try:
            exec(self.CODE, g)
        finally:
            sys.argv = real

    def test_version_fast_path_runs_before_imports(self, capsys):
        with pytest.raises(SystemExit) as e:
            self._run_as_main(["datafile.py", "--version"])
        assert e.value.code == 0
        assert capsys.readouterr().out.strip() == store.VERSION

    def test_main_entry_point(self, workspace, capsys):
        with pytest.raises(SystemExit) as e:
            self._run_as_main(["datafile.py"])
        assert e.value.code == 0
        assert capsys.readouterr().out.startswith("bin: ")

    def _run_without(self, blocked, capsys):
        """Execute datafile.py with a dependency made unimportable."""
        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == blocked:
                    raise ModuleNotFoundError(f"No module named {name!r}",
                                              name=name)
                return

        saved = {k: v for k, v in sys.modules.items()
                 if k == blocked or k.startswith(blocked + ".")}
        for k in saved:
            del sys.modules[k]
        sys.meta_path.insert(0, Blocker())
        try:
            with pytest.raises(SystemExit) as e:
                self._run_as_main(["datafile.py", "keys"])
            assert e.value.code == 1
            out = capsys.readouterr().out
            assert "code: DEPENDENCY_ERROR" in out
            assert f"missing dependency {blocked}" in out
        finally:
            sys.meta_path.pop(0)
            sys.modules.update(saved)

    def test_missing_pydantic_is_structured(self, capsys):
        self._run_without("pydantic", capsys)

    def test_missing_yaml_is_structured(self, capsys):
        self._run_without("yaml", capsys)


class TestMutationGaps:
    """Faults that 100% line+branch coverage did not catch."""

    def test_index_detects_file_replaced_at_same_or_larger_size(self, tmp_path):
        """Inode change must force a rebuild; size alone is not enough."""
        s = make_store(tmp_path)
        s.put({"id": "aa", "name": "A", "age": 1, "note": "n"})
        s.put({"id": "bb", "name": "B", "age": 1, "note": "n"})
        assert sorted(s.keys()) == ["aa", "bb"]        # warm the index

        pad = "n" * 60
        replacement = tmp_path / "new.jsonl"
        replacement.write_text("".join(
            json.dumps({"id": i, "name": "X", "age": 1, "note": pad}) + "\n"
            for i in ("cc", "dd", "ee")))
        assert replacement.stat().st_size > Path(s.path).stat().st_size
        os.replace(replacement, s.path)                # new inode, larger file

        assert sorted(s.keys()) == ["cc", "dd", "ee"]
        assert s.get("aa") is None

    def test_discovery_depth_boundary_is_exact(self, workspace, cli):
        for rel in ("a/x.jsonl", "a/b/x.jsonl", "a/b/c/x.jsonl",
                    "a/b/c/d/x.jsonl"):
            d = Path(rel).parent
            d.mkdir(parents=True, exist_ok=True)
            (d / "schema-x.yaml").write_text(CONTRACT)
            Path(rel).write_text('{"id":"ada","name":"A","age":1,"note":"n"}\n')

        _, out, _ = cli("stores")                      # DISCOVER_DEPTH == 3
        assert "a/x.jsonl" in out
        assert "a/b/x.jsonl" in out
        assert "a/b/c/x.jsonl" in out
        assert "a/b/c/d/x.jsonl" not in out            # one level too deep
        assert "count: 3" in out

    def test_cap_shows_exactly_the_cap(self, workspace, cli):
        for i in range(store.DISCOVER_CAP + 5):
            (workspace / f"schema-s{i:02d}.yaml").write_text(CONTRACT)
            (workspace / f"s{i:02d}.jsonl").write_text(
                '{"id":"ada","name":"A","age":1,"note":"n"}\n')
        _, out, _ = cli("stores")
        assert f"count: {store.DISCOVER_CAP}" in out
        assert out.count(".jsonl,") == store.DISCOVER_CAP


class TestSkillInstall:
    def _path(self, home):
        return home / ".claude" / "skills" / "datafile" / "SKILL.md"

    def test_installs_where_the_agent_reads_skills(self, fake_home, cli):
        code, out, _ = cli("skill", "--install")
        assert code == 0 and "installed" in out
        assert self._path(fake_home).exists()
        assert self._path(fake_home).read_text().startswith("---\nname: datafile")

    def test_install_is_idempotent(self, fake_home, cli):
        cli("skill", "--install")
        for _ in range(2):
            code, out, _ = cli("skill", "--install")
            assert code == 0 and "unchanged (no-op)" in out

    def test_install_refreshes_stale_content(self, fake_home, cli):
        cli("skill", "--install")
        self._path(fake_home).write_text("---\nname: datafile\n---\nstale\n")
        code, out, _ = cli("skill", "--install")
        assert code == 0 and "regenerated" in out
        assert "stale" not in self._path(fake_home).read_text()

    def test_project_scope_stays_local(self, fake_home, cli, tmp_path):
        cli("skill", "--install", "--scope", "project")
        assert (tmp_path / ".claude" / "skills" / "datafile"
                / "SKILL.md").exists()
        assert not self._path(fake_home).exists()

    def test_uninstall_round_trip(self, fake_home, cli):
        cli("skill", "--install")
        code, out, _ = cli("skill", "--uninstall")
        assert code == 0 and "removed" in out
        assert not self._path(fake_home).exists()
        assert not self._path(fake_home).parent.exists()
        code, out, _ = cli("skill", "--uninstall")
        assert code == 0 and "already absent (no-op)" in out

    def test_uninstall_refuses_a_foreign_skill(self, fake_home, cli):
        p = self._path(fake_home)
        p.parent.mkdir(parents=True)
        p.write_text("---\nname: something-else\n---\n")
        code, out, _ = cli("skill", "--uninstall")
        assert code == 1 and "SKILL_FOREIGN" in out
        assert p.exists()

    def test_out_and_install_conflict(self, fake_home, cli):
        code, out, _ = cli("skill", "--install", "--out", "X.md")
        assert code == 2 and "USAGE_ERROR" in out
        assert not Path("X.md").exists()

    def test_installed_copy_matches_the_repo_copy(self, fake_home, cli, tmp_path):
        cli("skill", "--out", "repo.md")
        cli("skill", "--install")
        assert Path("repo.md").read_text() == self._path(fake_home).read_text()

    def test_skill_documents_its_own_install_command(self, fake_home, cli):
        cli("skill", "--install")
        assert "skill --install" in self._path(fake_home).read_text()


class TestSkillMultiAgent:
    """pi discovers .agents/skills; Claude Code discovers .claude/skills."""

    def _claude(self, home):
        return home / ".claude" / "skills" / "datafile" / "SKILL.md"

    def _pi(self, home):
        return home / ".agents" / "skills" / "datafile" / "SKILL.md"

    def test_install_covers_both_agents(self, fake_home, cli):
        code, out, _ = cli("skill", "--install")
        assert code == 0
        assert "claude,installed" in out and "pi,installed" in out
        assert self._claude(fake_home).exists() and self._pi(fake_home).exists()
        assert self._claude(fake_home).read_text() == self._pi(fake_home).read_text()

    def test_pi_only(self, fake_home, cli):
        cli("skill", "--install", "--app", "pi")
        assert self._pi(fake_home).exists()
        assert not self._claude(fake_home).exists()

    def test_claude_only(self, fake_home, cli):
        cli("skill", "--install", "--app", "claude")
        assert self._claude(fake_home).exists()
        assert not self._pi(fake_home).exists()

    def test_unknown_app_is_usage_error(self, fake_home, cli):
        assert cli("skill", "--install", "--app", "emacs")[0] == 2

    def test_pi_project_scope_matches_default_output_path(self, fake_home, cli,
                                                          tmp_path):
        """pi walks up from cwd looking for .agents/skills, which is also where
        the plain `skill` command writes - so they must agree."""
        cli("skill", "--install", "--app", "pi", "--scope", "project")
        installed = tmp_path / ".agents" / "skills" / "datafile" / "SKILL.md"
        assert installed.exists()
        assert str(installed).endswith(store.SKILL_PATH)

    def test_layout_follows_agent_skills_standard(self, fake_home, cli):
        cli("skill", "--install")
        for p in (self._claude(fake_home), self._pi(fake_home)):
            assert p.name == "SKILL.md"
            assert p.parent.name == store.SKILL_NAME
            assert p.read_text().startswith("---\nname: datafile\n")

    def test_partial_uninstall_reports_per_agent(self, fake_home, cli):
        cli("skill", "--install", "--app", "pi")
        code, out, _ = cli("skill", "--uninstall")
        assert code == 0
        assert "claude,already absent (no-op)" in out
        assert "pi,removed" in out

    def test_foreign_skill_blocks_uninstall_for_that_agent(self, fake_home, cli):
        p = self._pi(fake_home)
        p.parent.mkdir(parents=True)
        p.write_text("---\nname: someone-else\n---\n")
        code, out, _ = cli("skill", "--uninstall", "--app", "pi")
        assert code == 1 and "SKILL_FOREIGN" in out
        assert p.exists()


class TestPiPackage:
    FILES: ClassVar[set[str]] = {
        "package.json", "README.md", "extensions/ambient-context.ts",
        "skills/datafile/SKILL.md"}

    def test_generates_the_full_layout(self, workspace, cli):
        code, out, _ = cli("pi-package")
        assert code == 0
        root = Path(store.PI_PACKAGE_DIR)
        assert {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} \
            == self.FILES
        assert "pi install" in out

    def test_manifest_is_a_valid_pi_package(self, workspace, cli):
        cli("pi-package")
        d = json.loads(Path(store.PI_PACKAGE_DIR, "package.json").read_text())
        assert "pi-package" in d["keywords"]
        assert d["pi"] == {"extensions": ["./extensions"], "skills": ["./skills"]}
        assert d["type"] == "module"
        assert d["peerDependencies"]["@earendil-works/pi-coding-agent"] == "*"
        assert d["version"] == store.VERSION

    def test_bundled_skill_matches_the_generated_skill(self, workspace, cli):
        cli("pi-package")
        cli("skill", "--out", "ref.md")
        assert Path(store.PI_PACKAGE_DIR, "skills", store.SKILL_NAME,
                    "SKILL.md").read_text() == Path("ref.md").read_text()

    def test_skill_lands_on_a_pi_discovery_path(self, workspace, cli):
        """pi finds skills as <dir>/<name>/SKILL.md under a `skills` entry."""
        cli("pi-package")
        p = Path(store.PI_PACKAGE_DIR, "skills", store.SKILL_NAME, "SKILL.md")
        assert p.exists() and p.parent.name == store.SKILL_NAME

    def test_extension_prefers_path_then_absolute(self, workspace, cli):
        cli("pi-package")
        ts = Path(store.PI_PACKAGE_DIR, "extensions",
                  "ambient-context.ts").read_text()
        m = re.search(r"const CANDIDATES: string\[\]\[\] = (\[.*?\]);", ts, re.S)
        candidates = json.loads(m.group(1))
        assert candidates[0] == [store.TOOL_NAME]
        assert candidates[1][-1].endswith("datafile.py")
        assert 'pi.on("session_start"' in ts
        assert 'pi.on("before_agent_start"' in ts

    def test_package_does_not_depend_on_the_generating_path(self, workspace, cli,
                                                            monkeypatch):
        """Regression: `just install` collapsed the fallback to a second bare name.

        The absolute entry is the only thing a machine without datafile on PATH
        can fall back to, so what gets generated must not depend on whether the
        generating machine happens to have the script symlinked onto PATH.
        """
        ext = Path(store.PI_PACKAGE_DIR, "extensions", "ambient-context.ts")
        readme = Path(store.PI_PACKAGE_DIR, "README.md")

        # Both states are forced. Reading the real machine's PATH for either one
        # makes the test pass for the wrong reason on whichever machine already
        # matches it.
        monkeypatch.setattr(store, "_path_alias", lambda me: None)
        cli("pi-package")
        off = (ext.read_text(), readme.read_text())

        monkeypatch.setattr(store, "_path_alias", lambda me: store.TOOL_NAME)
        cli("pi-package")

        assert (ext.read_text(), readme.read_text()) == off

    def test_readme_has_no_absolute_paths_from_cwd(self, workspace, cli):
        """It must be byte-identical wherever generated, or --check is flaky."""
        cli("pi-package")
        text = Path(store.PI_PACKAGE_DIR, "README.md").read_text()
        assert str(workspace) not in text
        assert "/path/to/" in text

    def test_check_clean_stale_and_missing(self, workspace, cli):
        code, out, _ = cli("pi-package", "--check")
        assert code == 1 and "PACKAGE_STALE" in out
        cli("pi-package")
        assert cli("pi-package", "--check")[0] == 0
        Path(store.PI_PACKAGE_DIR, "README.md").write_text("drift")
        code, out, _ = cli("pi-package", "--check")
        assert code == 1 and "README.md" in out

    def test_regeneration_is_idempotent(self, workspace, cli):
        cli("pi-package")
        code, out, _ = cli("pi-package")
        assert code == 0 and "unchanged (no-op)" in out

    def test_repairs_a_tampered_file(self, workspace, cli):
        cli("pi-package")
        p = Path(store.PI_PACKAGE_DIR, "package.json")
        p.write_text("{}")
        assert cli("pi-package")[0] == 0
        assert json.loads(p.read_text())["name"] == store.PI_PACKAGE_DIR

    def test_custom_out_directory(self, workspace, cli):
        assert cli("pi-package", "--out", "dist/pi")[0] == 0
        assert Path("dist/pi/package.json").exists()
        assert not Path(store.PI_PACKAGE_DIR).exists()

    def test_uv_missing_is_setup_error(self, workspace, cli, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda n: None)
        code, out, _ = cli("pi-package")
        assert code == 1 and "uv not found" in out


# ------------------------------------------------------------ roll (archiving)

def _many(cli, n, prefix="u"):
    """n distinct records: an append-only log, no updates and no tombstones."""
    for i in range(n):
        cli("-f", "u.jsonl", "put", rec(id=f"{prefix}{i}", name=f"N{i}"))


class TestRoll:
    def test_under_threshold_is_a_noop(self, workspace, cli):
        _many(cli, 3)
        before = Path("u.jsonl").read_bytes()
        code, out, _ = cli("-f", "u.jsonl", "roll", "--if-larger-than", "50MB")
        assert code == 0 and "no-op" in out
        assert Path("u.jsonl").read_bytes() == before
        assert not Path("archive").exists()

    def test_missing_and_empty_logs_are_noops(self, workspace, cli):
        assert "no-op" in cli("-f", "u.jsonl", "roll")[1]
        Path("u.jsonl").write_bytes(b"")
        assert "no-op" in cli("-f", "u.jsonl", "roll")[1]

    def test_roll_empties_the_active_log(self, workspace, cli):
        _many(cli, 5)
        code, out, _ = cli("-f", "u.jsonl", "roll")
        assert code == 0 and "rolled: " in out
        assert Path("u.jsonl").stat().st_size == 0
        assert "0 records" in cli("-f", "u.jsonl", "list")[1]

    def test_segment_round_trips_byte_exact(self, workspace, cli):
        import gzip
        import hashlib
        _many(cli, 20)
        original = Path("u.jsonl").read_bytes()
        cli("-f", "u.jsonl", "roll")
        seg = next(Path("archive").glob("*.jsonl.gz"))
        assert gzip.decompress(seg.read_bytes()) == original
        row = json.loads(Path("archive/manifest.jsonl").read_text().splitlines()[0])
        assert row["records"] == 20
        assert row["bytes"] == len(original)
        assert row["sha256"] == hashlib.sha256(original).hexdigest()

    def test_no_records_are_lost_across_rolls(self, workspace, cli):
        import gzip
        total = 0
        for batch in range(3):
            _many(cli, 4, prefix=f"b{batch}x")
            total += 4
            cli("-f", "u.jsonl", "roll")
        rows = [json.loads(ln) for ln in
                Path("archive/manifest.jsonl").read_text().splitlines()]
        assert len(rows) == 3
        assert sum(r["records"] for r in rows) == total
        seen = set()
        for seg in Path("archive").glob("*.jsonl.gz"):
            for line in gzip.decompress(seg.read_bytes()).splitlines():
                seen.add(json.loads(line)["id"])
        assert len(seen) == total

    def test_manifest_is_self_describing(self, workspace, cli):
        _many(cli, 3)
        cli("-f", "u.jsonl", "roll")
        assert Path("archive/schema-manifest.yaml").exists()
        code, out, _ = cli("-f", "archive/manifest.jsonl", "list")
        assert code == 0 and "segment" in out       # resolved without -c

    def test_stale_index_is_dropped_on_roll(self, workspace, cli):
        _many(cli, 3)
        cli("-f", "u.jsonl", "get", "u0")           # warms u.jsonl.idx
        assert Path("u.jsonl.idx").exists()
        cli("-f", "u.jsonl", "roll")
        assert not Path("u.jsonl.idx").exists()
        code, out, _ = cli("-f", "u.jsonl", "get", "u0")
        assert code == 1 and "NOT_FOUND" in out     # gone, not a stale hit

    def test_refuses_a_log_with_updates(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(name="A"))
        cli("-f", "u.jsonl", "put", rec(name="B"))
        code, out, _ = cli("-f", "u.jsonl", "roll")
        assert code == 2 and "UNSAFE_ROLL" in out
        assert "updated_keys: 1" in out
        assert not Path("archive").exists()

    def test_refuses_a_log_with_tombstones(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec())
        cli("-f", "u.jsonl", "delete", "ada")
        code, out, _ = cli("-f", "u.jsonl", "roll")
        assert code == 2 and "tombstones: 1" in out
        assert "updated_keys: 0" in out             # a tombstone is not an update

    def test_force_overrides_the_guard(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(name="A"))
        cli("-f", "u.jsonl", "put", rec(name="B"))
        code, out, _ = cli("-f", "u.jsonl", "roll", "--force")
        assert code == 0 and "rolled: " in out

    def test_compact_then_roll_is_the_safe_path(self, workspace, cli):
        cli("-f", "u.jsonl", "put", rec(name="A"))
        cli("-f", "u.jsonl", "put", rec(name="B"))
        cli("-f", "u.jsonl", "compact")
        assert cli("-f", "u.jsonl", "roll")[0] == 0

    def test_archive_dir_is_configurable(self, workspace, cli):
        _many(cli, 3)
        cli("-f", "u.jsonl", "roll", "--archive-dir", "cold")
        assert list(Path("cold").glob("*.jsonl.gz"))

    def test_segments_in_the_same_second_do_not_collide(self, workspace, cli):
        for _ in range(3):
            _many(cli, 2, prefix=f"p{_}x")
            cli("-f", "u.jsonl", "roll")
        assert len(list(Path("archive").glob("*.jsonl.gz"))) == 3


class TestParseSize:
    @pytest.mark.parametrize("text,want", [
        ("0", 0), ("512", 512), ("512b", 512), ("1k", 1024), ("2KB", 2048),
        ("50MB", 50 * 1024 ** 2), ("1.5m", int(1.5 * 1024 ** 2)),
        ("2GB", 2 * 1024 ** 3), (" 50 MB ", 50 * 1024 ** 2),
    ])
    def test_accepts_sizes(self, text, want):
        assert store.parse_size(text) == want

    @pytest.mark.parametrize("text", ["", "MB", "50TB", "-1", "abc", "50 MB extra"])
    def test_rejects_junk(self, text):
        with pytest.raises(store.AxiError) as e:
            store.parse_size(text)
        assert e.value.code == "USAGE_ERROR"


class TestRollCoverageGaps:
    """Paths in `roll` that the existing suite did not reach."""

    def test_violation_scan_skips_unreadable_lines(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("{not json\n")
        assert s.append_only_violations() == (0, 0)

    def test_roll_on_a_missing_file_is_none(self, tmp_path):
        s = make_store(tmp_path)
        assert s.roll(str(tmp_path / "archive")) is None

    def test_roll_below_min_bytes_is_none(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        assert s.roll(str(tmp_path / "archive"), min_bytes=10_000_000) is None
        assert Path(s.path).exists()          # log left untouched

    def test_blank_lines_are_not_counted_as_records(self, tmp_path):
        s = make_store(tmp_path)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        with open(s.path, "a") as f:
            f.write("\n\n")
        row = s.roll(str(tmp_path / "archive"))
        assert row["records"] == 1

    def test_archive_io_failure_is_structured(self, workspace, cli, monkeypatch):
        cli("-f", "u.jsonl", "put", rec())
        monkeypatch.setattr(store.Store, "roll",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))
        code, out, err = cli("-f", "u.jsonl", "roll")
        assert code == 1 and err == ""
        assert "IO_ERROR" in out and "same filesystem" in out


# ------------------------------------------------------------ durability (§9)

class _FsyncTrace:
    """Records the durability-relevant syscalls in the order they are issued.

    Distinguishes an fsync on a directory fd from one on a file fd, because the
    ordering guarantee for a rewrite is specifically fsync(file) before the
    rename and fsync(dir) after it.
    """

    def __init__(self, monkeypatch):
        self.events = []
        self._dirfds = {}
        real_fsync, real_replace = os.fsync, os.replace
        real_open, real_close = os.open, os.close

        def fsync(fd):
            self.events.append(("fsync", "dir" if fd in self._dirfds else "file"))
            return real_fsync(fd)

        def replace(src, dst):
            self.events.append(("replace", os.path.basename(str(dst))))
            return real_replace(src, dst)

        def opn(path, *a, **k):
            fd = real_open(path, *a, **k)
            if os.path.isdir(path):
                self._dirfds[fd] = path
            return fd

        def close(fd):
            self._dirfds.pop(fd, None)
            return real_close(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "open", opn)
        monkeypatch.setattr(os, "close", close)

    @property
    def kinds(self):
        return [e[0] for e in self.events]


class TestDurability:
    """The write ordering is the testable half of the durability claim. What is
    not testable in software is whether fsync reaches the medium; see the
    Durability section of README.md, and test_fsync_is_deliberately_not_fullfsync
    below, which pins the decision the README documents.
    """

    def test_append_fsyncs_the_data_file_once(self, tmp_path, monkeypatch):
        s = make_store(tmp_path)
        t = _FsyncTrace(monkeypatch)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        assert t.events == [("fsync", "file")]
        assert os.path.getsize(s.path) > 0

    def test_rewrite_orders_fsync_then_rename_then_dir_fsync(self, tmp_path,
                                                             monkeypatch):
        s = make_store(tmp_path)
        for i in range(3):
            s.put({"id": "ada", "name": f"A{i}", "age": 1, "note": "n"})
        s.put({"id": "bob", "name": "B", "age": 1, "note": "n"})
        s.delete("bob")

        t = _FsyncTrace(monkeypatch)
        s.compact()

        # Every rename must be bracketed: the temp file durable before it, the
        # directory entry durable after it. Compaction rewrites the log and the
        # .idx sidecar, so there is more than one.
        renames = [i for i, k in enumerate(t.kinds) if k == "replace"]
        assert renames, "compact issued no rename"
        for r in renames:
            before = [e for e in t.events[:r] if e == ("fsync", "file")]
            after = [e for e in t.events[r:] if e == ("fsync", "dir")]
            assert before, f"rename at {r} was not preceded by fsync(file)"
            assert after, f"rename at {r} was not followed by fsync(dir)"

    @staticmethod
    def _abort_at(s, op, boundary):
        """Run op with the nth fsync/rename raising instead of completing.
        Returns False once boundary is past the end of the operation."""
        real_fsync, real_replace = os.fsync, os.replace
        hits = [0]

        def trip():
            hits[0] += 1
            if hits[0] == boundary + 1:
                raise OSError("simulated crash")

        def fsync(fd):
            trip()
            return real_fsync(fd)

        def replace(src, dst):
            trip()
            return real_replace(src, dst)

        os.fsync, os.replace = fsync, replace
        try:
            if op == "append":
                s.put({"id": "cy", "name": "C", "age": 1, "note": "n"})
            else:
                s.compact()
            return False
        except OSError:
            return True
        finally:
            os.fsync, os.replace = real_fsync, real_replace

    @pytest.mark.parametrize("op", ["append", "compact"])
    def test_crash_at_any_boundary_keeps_committed_records(self, tmp_path, op):
        """Abort at each fsync/rename boundary in turn, then reopen the store
        cold. A record committed before the crash must still read."""
        boundary = 0
        while True:
            d = tmp_path / f"{op}{boundary}"
            d.mkdir()
            s = make_store(d)
            s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
            if op == "compact":
                s.put({"id": "bob", "name": "B", "age": 1, "note": "n"})
                s.delete("bob")

            if not self._abort_at(s, op, boundary):      # boundaries exhausted
                assert boundary > 0, f"{op} issued no fsync or rename at all"
                break

            cold = store.Store(s.path, s.model, key=s.key)   # nothing in memory
            alive, _ = cold.load()
            assert "ada" in alive, \
                f"{op}: committed record lost by a crash at boundary {boundary}"
            boundary += 1

    def test_torn_write_at_every_byte_is_recoverable(self, tmp_path):
        """A power loss during an append presents as a partially written final
        record, not a clean abort. Cut at every byte inside that record and
        require that committed records still read and that the torn line cannot
        swallow the append that follows it.

        The record carries multi-byte UTF-8, so some cuts land mid-codepoint and
        leave bytes that are not valid UTF-8 at all.
        """
        srcdir = tmp_path / "src"
        srcdir.mkdir()
        s = make_store(srcdir)
        s.put({"id": "ada", "name": "A", "age": 1, "note": "n"})
        s.put({"id": "bob", "name": "B", "age": 1, "note": "n"})
        before = os.path.getsize(s.path)
        s.put({"id": "cy", "name": "C" + "é中\U0001f600" * 20,
               "age": 1, "note": "n"})
        full = Path(s.path).read_bytes()
        model, key = s.model, s.key                  # compile the contract once

        work = tmp_path / "work"
        work.mkdir()
        target = work / "d.jsonl"
        cuts = range(before, len(full))
        assert len(cuts) > 100, "expected a wide torn-write surface"

        for cut in cuts:
            for stale in work.glob("d.jsonl.*"):     # .idx and .lock sidecars
                stale.unlink()
            target.write_bytes(full[:cut])

            cold = store.Store(str(target), model, key=key)
            alive, _ = cold.load()
            assert "ada" in alive and "bob" in alive, \
                f"cut at byte {cut} lost a committed record"

            cold.put({"id": "zed", "name": "Z", "age": 1, "note": "n"})
            after = store.Store(str(target), model, key=key)
            alive2, _ = after.load()
            assert {"ada", "bob", "zed"} <= set(alive2), \
                f"cut at byte {cut}: torn tail swallowed the next append"

    def test_fsync_is_deliberately_not_fullfsync(self):
        """On macOS, fsync() does not flush the drive's write cache; only
        fcntl(fd, F_FULLFSYNC) does. datafile does not issue it, at roughly a
        110x saving per write. That is a documented trade, not an oversight.

        This pins the decision to the documentation: if the calls ever change,
        the Durability section of README.md has to change with them.
        """
        src = DATAFILE_PY.read_text()
        assert "F_FULLFSYNC" not in src
        assert src.count("os.fsync(") == 5
        readme = (HERE / "README.md").read_text()
        assert "## Durability" in readme and "F_FULLFSYNC" in readme
