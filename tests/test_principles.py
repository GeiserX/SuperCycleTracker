"""The shared assistant principles: shipped with the app, unchanged, and read first, once."""

import hashlib

import pytest

from supercycletracker import assistant

# The shared assistant principles, byte for byte. A change to them lands here on purpose, never by
# accident: copy the new text into supercycletracker/principles/ and update the hash.
SHA256 = {
    "en": "20c8da6b8f6fb7456d126bfe5fb4516261299e10b7556aed1849e243f43856ac",
    "es": "49e40b0b7108c578813ee343cb93edc687ad0721646a6e219ce7845547ceb5ca",
}
HEADING = {"en": "## Principles", "es": "## Principios"}


@pytest.mark.parametrize("lang", sorted(SHA256))
def test_the_shipped_principles_are_the_shared_text(lang):
    raw = (assistant.PRINCIPLES / f"{lang}.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA256[lang]


@pytest.mark.parametrize("lang", sorted(SHA256))
@pytest.mark.parametrize("her_text", [False, True])
def test_the_rules_start_with_the_twelve_principles_exactly_once(lang, her_text):
    rules = assistant.rules("Lucía", lang, ["Marta"], "Prefer short answers.", her_text)
    text = (assistant.PRINCIPLES / f"{lang}.md").read_text(encoding="utf-8").rstrip()
    assert text.count("**") == 24  # twelve bold titles
    assert rules.startswith(text + "\n")
    assert rules.count(HEADING[lang]) == 1 and rules.count(text) == 1
    assert rules.index("You are the cycle companion of Lucía") > len(text)
    assert rules.rstrip().endswith("Prefer short answers.")


def test_a_language_without_a_copy_reads_the_english_principles():
    assert assistant.rules("Lucía", "de", ["Marta"]).startswith(HEADING["en"])
    assert assistant.rules("Lucía", "es-ES", ["Marta"]).startswith(HEADING["es"])


def test_the_bots_rules_no_longer_repeat_what_the_principles_say():
    own = assistant.rules("Lucía", "en", ["Marta"]).split("You are the cycle companion", 1)[1]
    own = " ".join(own.split()).lower()  # a reflowed or recased sentence is the same sentence
    for gone in (
        "mentioning you, by replying to one of your messages",
        "act only on what is addressed to you",
        "is not a request",
        "Answer them all in one message",
        "say what changes, if anything",
        "Never claim something is saved",
        "Never mention files, tools, blocks",
        "A tap is an action",
        "exactly the two words for yes and no",
        "never the same offer twice",
        "or nothing",
        "says that without an answer nothing is done",
        "those three ways",
    ):
        assert gone.lower() not in own, gone
