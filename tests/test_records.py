import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from supercycletracker import records
from supercycletracker.records import BlockError, Day, Edit, apply, validate_block

FIXTURE = Path(__file__).parent / "fixtures" / "log_fictional.jsonl"
TODAY = date(2027, 3, 10)


def d(s: str) -> date:
    return date.fromisoformat(s)


def mk(*items: tuple[str, str | None]) -> dict[date, Day]:
    log = {d(s): Day(date=d(s), source="telegram", flow=flow) for s, flow in items}
    return records.derive_starts(log)


def edit(days=(), delete=()) -> Edit:
    return Edit(days={d(s): f for s, f in days}, delete=frozenset(d(s) for s in delete))


# --- the file ------------------------------------------------------------------------------


def test_fixture_round_trips_byte_identical(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    log = records.load(FIXTURE)
    assert records.dumps(log) == text
    out = tmp_path / "cycle" / "log.jsonl"
    records.save(out, log)
    assert out.read_bytes() == FIXTURE.read_bytes()
    assert [p.name for p in out.parent.iterdir()] == ["log.jsonl"]  # no temp file left


def test_fixture_has_a_dozen_starts_and_derive_reproduces_its_flags():
    log = records.load(FIXTURE)
    flagged = [x for x, day in log.items() if day.cycle_start]
    assert len(flagged) == 12
    stripped = {x: Day(**{**day.__dict__, "cycle_start": False}) for x, day in log.items()}
    assert records.derive_starts(stripped) == log


def test_key_order_and_non_ascii():
    day = Day(
        date=d("2027-01-02"),
        source="admin",
        flow="light",
        cycle_start=True,
        pain=3,
        symptoms=("headache", "bloating"),
        note="mañana más",
    )
    line = day.to_line()
    assert list(json.loads(line)) == list(records.KEYS)
    assert "mañana más" in line


def test_missing_file_is_an_empty_log(tmp_path):
    assert records.load(tmp_path / "nope.jsonl") == {}


def test_unknown_key_on_load_is_refused(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"date": "2027-01-02", "flow": "light", "x": 1, "source": "admin"}\n')
    with pytest.raises(ValueError):
        records.load(path)


@pytest.mark.parametrize(
    "lines, code",
    [
        (
            [
                '{"date": "2027-01-02", "flow": "light", "source": "admin"}',
                '{"date": "2027-01-02", "flow": "heavy", "source": "admin"}',
            ],
            "duplicate_date",
        ),
        (['{"date": "2027-01-02", "flow": "light"}'], "missing_key"),
        (['{"flow": "light", "source": "admin"}'], "missing_key"),
        # a hand edit pulled from the remote: every malformed shape is a ValueError reason code
        (["5"], "bad_line"),
        (['["2027-01-02"]'], "bad_line"),
        (["{not json}"], "bad_json"),
        (['{"date": "2027-13-02", "source": "admin"}'], "bad_date"),
        (['{"date": "2027-01-02", "source": 7}'], "bad_source"),
        (['{"date": "2027-01-02", "flow": "banana", "source": "admin"}'], "unknown_flow"),
        (['{"date": "2027-01-02", "pain": "x", "source": "admin"}'], "bad_pain"),
        (['{"date": "2027-01-02", "pain": 11, "source": "admin"}'], "bad_pain"),
        (['{"date": "2027-01-02", "pain": true, "source": "admin"}'], "bad_pain"),
        (['{"date": "2027-01-02", "symptoms": 3, "source": "admin"}'], "bad_symptoms"),
        (['{"date": "2027-01-02", "symptoms": ["acne", 4], "source": "admin"}'], "bad_symptoms"),
        (['{"date": "2027-01-02", "symptoms": ["itchy"], "source": "admin"}'], "unknown_symptom"),
        (['{"date": "2027-01-02", "note": 5, "source": "admin"}'], "bad_note"),
        (['{"date": "2027-01-02", "cycle_start": "yes", "source": "admin"}'], "bad_cycle_start"),
    ],
)
def test_a_bad_line_on_load_raises_a_reason_code(tmp_path, lines, code):
    path = tmp_path / "log.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    with pytest.raises(ValueError) as err:
        records.load(path)
    assert type(err.value) is ValueError and str(err.value) == code


def test_save_is_atomic_when_the_rename_fails(tmp_path, monkeypatch):
    out = tmp_path / "log.jsonl"
    records.save(out, records.load(FIXTURE))
    original = out.read_bytes()

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(records.os, "replace", boom)
    with pytest.raises(OSError):
        records.save(out, mk(("2027-01-01", "heavy")))
    assert out.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["log.jsonl"]  # the temp file is gone


def test_save_leaves_the_file_readable_by_its_owner_only(tmp_path):
    out = tmp_path / "log.jsonl"
    out.write_text("")
    out.chmod(0o644)
    records.save(out, mk(("2027-01-01", "heavy")))
    assert out.stat().st_mode & 0o777 == 0o600


# --- starts --------------------------------------------------------------------------------


def test_gap_rule_ten_days_is_the_same_cycle():
    log = mk(("2027-01-01", "medium"), ("2027-01-11", "light"))
    assert records.starts(log) == [d("2027-01-01")]
    assert not log[d("2027-01-11")].cycle_start


def test_gap_rule_eleven_days_is_a_new_cycle():
    log = mk(("2027-01-01", "medium"), ("2027-01-12", "light"))
    assert records.starts(log) == [d("2027-01-01"), d("2027-01-12")]
    assert log[d("2027-01-12")].cycle_start


def test_none_and_spotting_never_start_a_cycle():
    log = mk(("2027-01-01", "medium"), ("2027-01-05", "none"), ("2027-02-20", "none"))
    log[d("2027-01-18")] = Day(date=d("2027-01-18"), source="telegram", symptoms=("spotting",))
    log = records.derive_starts(log)
    assert records.starts(log) == [d("2027-01-01")]
    assert [x for x, day in log.items() if day.cycle_start] == [d("2027-01-01")]


def test_a_back_dated_bleed_moves_the_start():
    log = mk(("2027-01-05", "medium"))
    change = apply(log, edit(days=[("2027-01-03", {"flow": "light"})]), "telegram")
    assert records.starts(change.after) == [d("2027-01-03")]
    assert not change.after[d("2027-01-05")].cycle_start
    # the re-derived flag shows in `after`, but only the date the edit named is an item
    assert change.dates == (d("2027-01-03"),)


# --- applying ------------------------------------------------------------------------------


def test_replace_field_semantics():
    log = {
        d("2027-01-02"): Day(
            date=d("2027-01-02"),
            source="apple-health-export-2027-01-01",
            flow="medium",
            pain=4,
            symptoms=("headache",),
            note="algo",
        )
    }
    change = apply(log, edit(days=[("2027-01-02", {"pain": 6})]), "telegram")
    day = change.after[d("2027-01-02")]
    assert (day.flow, day.pain, day.symptoms, day.note) == ("medium", 6, ("headache",), "algo")
    assert day.source == "telegram"  # a chat write replaces an import tag

    change = apply(change.after, edit(days=[("2027-01-02", {"symptoms": []})]), "admin")
    assert change.after[d("2027-01-02")].symptoms == ()
    assert change.after[d("2027-01-02")].source == "admin"

    change = apply(change.after, edit(days=[("2027-01-02", {"note": ""})]), "admin")
    assert change.after[d("2027-01-02")].note is None


def test_a_day_left_with_no_fact_is_removed():
    log = {d("2027-01-02"): Day(date=d("2027-01-02"), source="telegram", symptoms=("acne",))}
    change = apply(log, edit(days=[("2027-01-02", {"symptoms": []})]), "telegram")
    assert change.after == {}
    assert change.items == ((d("2027-01-02"), log[d("2027-01-02")], None),)


def test_delete():
    log = mk(("2027-01-01", "medium"), ("2027-01-02", "light"))
    change = apply(log, edit(delete=["2027-01-02", "2027-01-20"]), "telegram")
    assert d("2027-01-02") not in change.after
    assert change.dates == (d("2027-01-02"),)  # deleting an absent date changes nothing


def test_untouched_lines_stay_byte_identical(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_bytes(FIXTURE.read_bytes())
    before = path.read_text(encoding="utf-8").splitlines()
    log = records.load(path)
    target = max(log)
    change = apply(log, edit(days=[(target.isoformat(), {"pain": 9})]), "telegram")
    records.save(path, change.after)
    after = path.read_text(encoding="utf-8").splitlines()
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(before) == len(after)
    assert changed == [len(before) - 1]


def test_a_change_with_before_equal_after_is_empty():
    log = mk(("2027-01-01", "medium"))
    change = apply(log, edit(days=[("2027-01-01", {"flow": "medium"})]), "telegram")
    assert not change
    assert change.items == ()
    assert change.after == log


def test_symptoms_are_stored_sorted_and_unique():
    change = apply(
        {}, edit(days=[("2027-01-01", {"symptoms": ["nausea", "acne", "nausea"]})]), "telegram"
    )
    assert change.after[d("2027-01-01")].symptoms == ("acne", "nausea")


def test_unknown_source_is_refused():
    with pytest.raises(ValueError):
        apply({}, edit(days=[("2027-01-01", {"flow": "light"})]), "somewhere")


# --- validating a block --------------------------------------------------------------------


def test_a_good_block():
    block = {
        "days": [
            {"date": "2027-03-09", "flow": "light", "pain": 2, "symptoms": ["headache"]},
            {"date": "2027-03-10", "flow": "none", "note": "  ya está  "},
        ],
        "delete": ["2027-03-01"],
    }
    e = validate_block(block, TODAY)
    assert e.days[d("2027-03-09")] == {"flow": "light", "pain": 2, "symptoms": ("headache",)}
    assert e.days[d("2027-03-10")] == {"flow": "none", "note": "ya está"}
    assert e.delete == {d("2027-03-01")}


def test_note_length_is_counted_after_trimming():
    e = validate_block(_day(note="  " + "x" * 500 + "  "), TODAY)
    assert e.days[d("2027-03-09")]["note"] == "x" * 500


def test_ninety_days_back_is_accepted():
    oldest = (TODAY - timedelta(days=90)).isoformat()
    assert validate_block({"days": [{"date": oldest, "flow": "light"}]}, TODAY).days


def _day(**fields):
    return {"days": [{"date": "2027-03-09", **fields}]}


REFUSED = [
    ("future date", {"days": [{"date": "2027-03-11", "flow": "light"}]}, "date_out_of_range"),
    ("91 days back", {"days": [{"date": "2026-12-09", "flow": "light"}]}, "date_out_of_range"),
    ("not a date", {"days": [{"date": "20270309", "flow": "light"}]}, "bad_date"),
    ("pain 11", _day(pain=11), "bad_pain"),
    ("pain as text", _day(pain="7"), "bad_pain"),
    ("pain as bool", _day(pain=True), "bad_pain"),
    ("unknown flow", _day(flow="torrential"), "unknown_flow"),
    ("unknown symptom", _day(symptoms=["headache", "zarzaparrilla"]), "unknown_symptom"),
    ("unknown day key", _day(flow="light", mood="zarzaparrilla"), "unknown_key"),
    ("cycle_start", _day(flow="light", cycle_start=True), "unknown_key"),
    ("source", _day(flow="light", source="admin"), "unknown_key"),
    ("unknown top key", {"days": [], "zarzaparrilla": 1}, "unknown_key"),
    ("note too long", _day(note="x" * 501), "note_too_long"),
    ("empty day", _day(), "empty_day"),
    (
        "15 days",
        {"days": [{"date": f"2027-03-{i:02d}", "flow": "light"} for i in range(1, 11)]}
        | {"delete": [f"2027-02-{i:02d}" for i in range(1, 6)]},
        "too_many_days",
    ),
    (
        "duplicate date",
        {"days": [{"date": "2027-03-09", "flow": "light"}], "delete": ["2027-03-09"]},
        "duplicate_date",
    ),
    ("not an object", ["zarzaparrilla"], "bad_block"),
]


@pytest.mark.parametrize("name,block,code", REFUSED, ids=[r[0] for r in REFUSED])
def test_refused_blocks_give_a_reason_code_without_the_value(name, block, code):
    with pytest.raises(BlockError) as err:
        validate_block(block, TODAY)
    assert err.value.code == code
    text = str(err.value)
    assert text == code
    for value in ("zarzaparrilla", "torrential", "2027-03", "2026-12", "11", "xxxx"):
        assert value not in text


def test_fourteen_days_is_the_limit():
    block = {"days": [{"date": f"2027-03-{i:02d}", "flow": "light"} for i in range(1, 11)]}
    block["delete"] = [f"2027-02-{i:02d}" for i in range(1, 5)]
    assert len(validate_block(block, TODAY).days) == 10
