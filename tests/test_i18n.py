import re
import string
from datetime import date

import pytest
import yaml

from supercycletracker import cycle, i18n, records
from supercycletracker.i18n import Translator, _load

ALLOWED_TAG = re.compile(r"</?(b|i|u|s|code)>")
ENTITY = re.compile(r"&(lt|gt|amp|quot);")


def walk(value, path=""):
    """Every string in a locale value, with where it sits."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")


def placeholders(s: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(s) if field is not None}


def shape(value):
    """The structure of a locale value with each string replaced by its placeholders."""
    if isinstance(value, str):
        return placeholders(value)
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return ("list", len(value))  # word lists may differ in content, not in kind
    return type(value).__name__


def _paired(s: str, opening: str, closing: str) -> bool:
    """Every `closing` has its own `opening` before it, and none is left open."""
    depth = 0
    for ch in s:
        depth += (ch == opening) - (ch == closing)
        if depth < 0:
            return False
    return depth == 0


def inverted_marks(strings: dict) -> list[str]:
    """Spanish strings where a «?» lacks its own «¿» before it, or a «!» its «¡»."""
    bad = []
    for key, value in strings.items():
        for path, s in walk(value, key):
            if not (_paired(s, "¿", "?") and _paired(s, "¡", "!")):
                bad.append(path)
    return bad


# --- the files -----------------------------------------------------------------------------


def test_english_and_spanish_ship():
    for locale in ("en", "es"):
        assert (i18n.LOCALES / f"{locale}.yaml").is_file()


@pytest.mark.parametrize("locale", ["es"])
def test_every_english_key_exists_with_the_same_placeholders(locale):
    en, other = _load("en"), _load(locale)
    assert set(en) == set(other)
    for key in en:
        if key.startswith("words_"):
            assert set(en[key]) == set(other[key]), key  # same meanings, own words
            continue
        assert shape(en[key]) == shape(other[key]), key


def test_spanish_questions_and_exclamations_open():
    assert inverted_marks(_load("es")) == []


def test_inverted_marks_check_can_fail():
    """Positive control: the check above reports a broken string, and a fixed one passes."""
    assert inverted_marks({"x": "Ha llegado?"}) == ["x"]
    assert inverted_marks({"x": {"y": ["Qué bien!"]}}) == ["x.y[0]"]
    assert inverted_marks({"x": "¿Algo más? Te duele?"}) == ["x"]  # second question unopened
    assert inverted_marks({"x": "Ha llegado?¿"}) == ["x"]  # opened after it closed
    assert inverted_marks({"x": "¿Ha llegado? ¡Qué bien! ¿Te duele?"}) == []


@pytest.mark.parametrize("locale", ["en", "es"])
def test_strings_are_safe_html(locale):
    for path, s in walk(_load(locale)):
        assert "<" not in ALLOWED_TAG.sub("", s), path
        assert "&" not in ENTITY.sub("", s), path


def test_every_phase_kind_has_a_phase_key():
    kinds = {k.removeprefix("phase_") for k in _load("en") if k.startswith("phase_")}
    assert kinds == set(cycle.KINDS)
    for locale in ("en", "es"):
        t = Translator(locale)
        for kind in cycle.KINDS:
            text = t(f"phase_{kind}", ws="W", we="E", n=3)
            assert text and not text.startswith("phase_")


def test_every_flow_and_symptom_has_a_label():
    for locale in ("en", "es"):
        t = Translator(locale)
        assert all(t.flow(f) for f in records.FLOWS)
        assert all(t.symptom(s) for s in records.SYMPTOMS)
        assert set(_load(locale)["flows"]) == set(records.FLOWS)
        assert set(_load(locale)["symptoms"]) == set(records.SYMPTOMS)
        assert set(_load(locale)["words_flow"]) <= set(records.FLOWS)


# --- the Translator ------------------------------------------------------------------------


def test_dates():
    wednesday = date(2027, 3, 10)
    es, en = Translator("es"), Translator("en")
    assert es.date(wednesday) == "miércoles 10 de marzo"
    assert en.date(wednesday) == "Wednesday 10 March"
    assert es.date(wednesday, "short") == "10 de marzo"
    assert es.date(wednesday, "pick") == "mié 10"
    with pytest.raises(ValueError):
        es.date(wednesday, "weird")


def test_labels_and_formatting():
    es = Translator("es")
    assert es.flow("medium") == "normal"
    assert es.flow("none") == "sin sangrado"
    assert es.symptom("abdominal_cramps") == "calambres"
    assert es("pain", n=4) == "dolor 4"
    assert es("ask_period", m="@someone", n=2) == "@someone, día 2 de la regla. ¿Cómo va hoy?"


def test_a_missing_label_falls_back_to_english_one_entry_at_a_time(tmp_path, monkeypatch):
    en = _load("en")
    partial = {"flows": {"light": "poquito"}, "symptoms": {"acne": "granos"}}
    (tmp_path / "en.yaml").write_text(yaml.safe_dump(en, allow_unicode=True), encoding="utf-8")
    (tmp_path / "xx.yaml").write_text(yaml.safe_dump(partial, allow_unicode=True))
    monkeypatch.setattr(i18n, "LOCALES", tmp_path)
    _load.cache_clear()
    try:
        t = Translator("xx")
        assert t.flow("light") == "poquito" and t.flow("heavy") == "heavy"
        assert t.symptom("acne") == "granos" and t.symptom("nausea") == "nausea"
    finally:
        _load.cache_clear()


def test_unknown_locale_falls_back_to_english():
    assert Translator("xx")("undo_none") == "Nothing to undo."
    assert Translator("xx").date(date(2027, 3, 10)) == "Wednesday 10 March"
