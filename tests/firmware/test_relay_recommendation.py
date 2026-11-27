# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which relay to fit as Relay 2: one recommendation, said the same everywhere.

The panel's Pins view carries the recommendation as its one note (its
``<aside data-recommendation>``): Relay 2 should be a solid-state PhotoMOS
such as the Relay 2 Click's LCA717, not an electromechanical relay such as
the HW-316's, and what the solid-state relay's off-state leakage costs a
reading. These tests hold:

- each paragraph of the note stands, word for word, in
  docs/relay2-click-wiring.md and docs/rig-wiring.md, under the heading the
  note has (markup aside: a code span is a ``<code>`` on the page and
  backticks in Markdown; a power of ten is a superscript on the page and
  ``10^-8`` in Markdown, which the published documentation's faces draw);
- every sentence of the note that gives a figure of the data sheet, or one
  worked from it, cites the data sheet's revision and its page;
- the figures are the arithmetic of the data sheet's bound and of the rig:
  1 uA at 30 V read as a resistance, a branch of 0.1 ohm, 10 uA and the
  INA228's full scale;
- the guard sees a paragraph that differs and a figure left uncited.

Host-only: the page and the documents read as text.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "host" / "kleidos_meter" / "panel" / "static" / "index.html"
DOCS = [ROOT / "docs" / "relay2-click-wiring.md", ROOT / "docs" / "rig-wiring.md"]

#: The data sheet every figure of the leakage comes from, by its revision.
REVISION = "DS-LCA717-R04"

#: A citation of it: the revision and a page.
CITATION = re.compile(rf"{REVISION}, page \d+")

#: A figure: a number and a unit, or a power of ten.
FIGURE = re.compile(r"\d(?:[\d.,]*)\s?(?:fA|pA|nA|µA|mA|µV|mV|V|MΩ|kΩ|Ω|°C|%)|× 10[⁻⁰-⁹]+")

# The data sheet's bound (page 2, "Off-State Leakage Current", at 25 °C) and the rig.
LEAK_MAX_A = 1e-6
LEAK_TEST_V = 30.0
BRANCH_OHM = 0.1  # the shunt's 0.100 ohm and Relay 1's contact, about
SMALL_A = 10e-6  # the reading the fractions are of
FULL_SCALE_V = 0.16384  # the INA228's shunt full scale at ADCRANGE 0


def _text(fragment: str) -> str:
    """A fragment of the page as a reader sees it: no tags, entities read, one space."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


#: The page's superscript signs, as a Markdown page writes a power: ``10⁻⁸`` is ``10^-8``.
SUPERSCRIPT = str.maketrans("⁻⁰¹²³⁴⁵⁶⁷⁸⁹", "-0123456789")


def as_markdown(paragraph: str) -> str:
    """A paragraph of the page with each power of ten written as Markdown writes it."""
    return re.sub(r"10([⁻⁰-⁹]+)", lambda m: "10^" + m.group(1).translate(SUPERSCRIPT), paragraph)


def _markdown_text(text: str) -> str:
    """A Markdown page as plain words: no code spans' backticks, no emphasis, one space."""
    return " ".join(text.replace("`", "").replace("**", "").split())


def note() -> tuple[str, list[str]]:
    """The note's heading and its paragraphs, as the page shows them."""
    page = PAGE.read_text(encoding="utf-8")
    asides = re.findall(r"<aside [^>]*data-recommendation[^>]*>(.*?)</aside>", page, re.DOTALL)
    assert len(asides) == 1, "the page has one recommendation note"
    heading = re.search(r"<h2[^>]*>(.*?)</h2>", asides[0], re.DOTALL)
    assert heading, "the note has its heading"
    paragraphs = [_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", asides[0], re.DOTALL)]
    return _text(heading[1]), paragraphs


def sentences(paragraph: str) -> list[str]:
    """*paragraph* cut at each full stop that ends a sentence."""
    return [s for s in re.split(r"(?<=[.])\s+(?=[A-Z])", paragraph) if s]


def uncited(paragraphs: list[str]) -> list[str]:
    """The sentences of the data sheet's paragraphs that give a figure and cite no page of it."""
    return [
        sentence
        for paragraph in paragraphs
        if REVISION in paragraph
        for sentence in sentences(paragraph)
        if FIGURE.search(sentence) and not CITATION.search(sentence)
    ]


def missing_from(document: str, heading: str, paragraphs: list[str]) -> list[str]:
    """What of the note *document* does not say word for word: the heading, then paragraphs."""
    words = _markdown_text(document)
    lacking = [] if re.search(rf"^#+ .*{re.escape(heading)}$", document, re.M) else [heading]
    return lacking + [p for p in paragraphs if as_markdown(p) not in words]


def test_the_note_names_the_parts_and_the_data_sheet() -> None:
    heading, paragraphs = note()
    assert heading == "Which relay to fit as Relay 2"
    assert len(paragraphs) == 2
    said = " ".join(paragraphs)
    for word in ("solid-state", "PhotoMOS", "LCA717", "HW-316", "10 mV", "leaks", REVISION):
        assert word in said, word
    assert "has not been measured on this rig" in paragraphs[1]


@pytest.mark.parametrize("document", DOCS, ids=lambda p: p.name)
def test_each_document_says_the_note_word_for_word(document: Path) -> None:
    heading, paragraphs = note()
    lacking = missing_from(document.read_text(encoding="utf-8"), heading, paragraphs)
    assert lacking == [], f"{document.name} does not say, as the panel does: {lacking}"


def test_every_figure_of_the_data_sheet_cites_its_revision_and_page() -> None:
    _, paragraphs = note()
    assert any(REVISION in p for p in paragraphs)
    assert uncited(paragraphs) == []


def test_the_figures_are_the_data_sheet_s_arithmetic() -> None:
    said = " ".join(note()[1])
    off_ohm = LEAK_TEST_V / LEAK_MAX_A
    small_v = SMALL_A * BRANCH_OHM
    small_leak = small_v / off_ohm
    full_leak = FULL_SCALE_V / off_ohm
    assert round(off_ohm / 1e6) == 30 and "at least 30 MΩ" in said
    assert round(small_v * 1e6) == 1 and "1 µV at 10 µA" in said
    assert f"{FULL_SCALE_V * 1e3:.0f} mV" == "164 mV" and "164 mV" in said
    assert f"{small_leak * 1e15:.0f} fA" == "33 fA" and "33 fA" in said
    assert f"{small_leak * 1e6:.1e}" == "3.3e-08" and "3.3 × 10⁻⁸ µA" in said
    assert f"{full_leak * 1e9:.1f} nA" == "5.5 nA" and "5.5 nA" in said
    assert f"{small_leak / SMALL_A:.1e}" == f"{BRANCH_OHM / off_ohm:.1e}" == "3.3e-09"
    assert "3.3 × 10⁻⁹ of either reading" in said
    assert pytest.approx(0.1) == LEAK_MAX_A / SMALL_A and "a tenth of a 10 µA reading" in said


def test_the_guard_sees_a_paragraph_that_differs_and_an_uncited_figure() -> None:
    heading, paragraphs = note()
    document = f"## {heading}\n\n" + "\n\n".join(as_markdown(p) for p in paragraphs)
    assert missing_from(document, heading, paragraphs) == []
    superscripts = f"## {heading}\n\n" + "\n\n".join(paragraphs)
    assert missing_from(superscripts, heading, paragraphs) == [paragraphs[1]]
    assert missing_from(document.replace("33 fA", "34 fA"), heading, paragraphs) == [paragraphs[1]]
    assert missing_from(document.replace(heading, "Relay 2"), heading, paragraphs) == [heading]
    cut = paragraphs[1].replace(f" ({REVISION}, page 2)", "", 1)
    assert cut != paragraphs[1] and uncited([cut]) != []
    assert uncited(["No data sheet here: 10 mV across it."]) == []
    assert sentences("A 1 µA bound. Then 30 V.") == ["A 1 µA bound.", "Then 30 V."]
