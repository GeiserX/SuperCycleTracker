"""The Apple Health importer on a fictional export (`fixtures/export_fictional.xml`)."""

import contextlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from supercycletracker import applehealth, cli, records
from supercycletracker.records import Day

FIXTURE = Path(__file__).parent / "fixtures" / "export_fictional.xml"
SOURCE = "apple-health-export-2028-05-20"
EARLIER = "apple-health-export-2028-01-30"


@pytest.fixture
def data(tmp_path):
    """A data dir whose log already holds a chat day and a day from an earlier import."""
    root = tmp_path / "data"
    (root / "cycle").mkdir(parents=True)
    (root / "cycle" / "profile.yaml").write_text(
        "name: Lucía\ntimezone: Europe/Madrid\nlanguage: es\n", encoding="utf-8"
    )
    log = {
        date(2028, 3, 8): Day(date=date(2028, 3, 8), source="telegram", flow="light"),
        date(2028, 1, 12): Day(date=date(2028, 1, 12), source=EARLIER, flow="heavy"),
    }
    records.save(root / "cycle" / "log.jsonl", records.derive_starts(log))
    return root


@pytest.fixture
def export_zip(tmp_path):
    """The export as the phone writes it: a localised member name, a bigger `_cda.xml` beside
    it and other bigger XML files that are not the export."""
    xml = FIXTURE.read_bytes()
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("apple_health_export/exportación.xml", xml)
        z.writestr("apple_health_export/export_cda.xml", xml + b"<!--" + b"x" * 50_000 + b"-->")
        z.writestr("apple_health_export/electrocardiograms/ecg.xml", b"<a/>" * 20_000)
        z.writestr("apple_health_export/workout-routes/route.xml", b"<b/>" * 20_000)
    return path


def log_of(data):
    return records.load(data / "cycle" / "log.jsonl")


def test_a_zip_with_a_localised_member_is_merged(data, export_zip):
    report = applehealth.merge(export_zip, data, "Europe/Madrid")
    log = log_of(data)
    flows = {d.isoformat(): x.flow for d, x in log.items()}
    assert flows == {
        "2028-01-10": "light",  # the current name form
        "2028-01-11": "heavy",  # the older name form and the integer 4 on one day: the heaviest
        "2028-01-12": "medium",  # the integer 3; replaces the earlier import's day
        "2028-01-13": "none",
        "2028-01-24": None,  # spotting only
        "2028-02-08": "light",  # 23:30 +0000 on 02-07 is 00:30 on 02-08 in Madrid
        "2028-02-09": "medium",
        "2028-02-10": "unspecified",  # the integer 1
        "2028-03-07": "medium",
        "2028-03-08": "light",  # the chat's day stays
    }
    assert log[date(2028, 1, 10)].symptoms == ("abdominal_cramps",)  # Headache NotPresent
    assert log[date(2028, 1, 11)].symptoms == ("fatigue",)  # Bloating 1, AppetiteChanges NoChange
    assert log[date(2028, 1, 24)].symptoms == ("spotting",)  # IntermenstrualBleeding
    assert log[date(2028, 3, 8)].source == "telegram"
    assert {x.source for d, x in log.items() if d != date(2028, 3, 8)} == {SOURCE}
    assert [d for d, x in log.items() if x.cycle_start] == [
        date(2028, 1, 10),
        date(2028, 2, 8),
        date(2028, 3, 7),
    ]
    assert report.records_read == 16  # flow and symptom records, not the step count
    assert report.days_written == 10  # the chat day's flag moved to 03-07
    assert report.days_protected == 1
    assert report.starts == 3
    assert (report.flagged, report.agree) == (4, 3)  # the export flags 01-12 too


def test_a_rerun_is_a_no_op(data, export_zip):
    applehealth.merge(export_zip, data, "Europe/Madrid")
    path = data / "cycle" / "log.jsonl"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    report = applehealth.merge(export_zip, data, "Europe/Madrid")
    assert report.days_written == 0
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_a_dry_run_writes_nothing(data, export_zip):
    path = data / "cycle" / "log.jsonl"
    before = path.read_bytes()
    report = applehealth.merge(export_zip, data, "Europe/Madrid", dry_run=True)
    assert report.days_written == 10
    assert path.read_bytes() == before


def test_the_plain_xml_reads_the_same_as_the_zip(data, export_zip, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "cycle").mkdir()
    (other / "cycle" / "log.jsonl").write_bytes((data / "cycle" / "log.jsonl").read_bytes())
    applehealth.merge(export_zip, data, "Europe/Madrid")
    applehealth.merge(FIXTURE, other, "Europe/Madrid")
    assert (other / "cycle" / "log.jsonl").read_bytes() == (
        data / "cycle" / "log.jsonl"
    ).read_bytes()


def test_the_cda_file_is_never_the_export(tmp_path):
    path = tmp_path / "only_cda.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("apple_health_export/export_cda.xml", FIXTURE.read_bytes())
    with pytest.raises(applehealth.ImportFailed, match="no_export_xml"):
        applehealth.parse(path, "Europe/Madrid")


def test_broken_xml_is_refused_without_quoting_it(tmp_path):
    path = tmp_path / "broken.xml"
    path.write_text("<HealthData><Record secret-word", encoding="utf-8")
    with pytest.raises(applehealth.ImportFailed) as e:
        applehealth.parse(path, "Europe/Madrid")
    assert str(e.value) == "bad_xml"


def test_the_cli_prints_counts_only(data, export_zip):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["import-apple-health", str(export_zip), "--data", str(data), "--dry-run"])
    assert code == 0, err.getvalue()
    text = out.getvalue()
    assert "records read: 16" in text
    assert "days would write: 10" in text
    assert "dry run: nothing written" in text
    assert "2028" not in text and "medium" not in text
