# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The published documentation's build and guard, without Doxygen.

``scripts/build_reference.py`` builds the site as it is published and
``scripts/check_reference.py`` guards what would go out; CI's docs job runs
both on a real build. This holds what needs no Doxygen: that the guard sees
each defect it names (its self-test), that it reads the private-value rules
of the gitleaks files and lets their fakes through, that the build's rewrites
do what they say on a page written by hand, and that the overlay and the
header keep the host's settings.

Host-only: the scripts imported, a temporary tree written.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import build_reference  # noqa: E402
import check_reference  # noqa: E402


def test_the_guard_sees_every_planted_defect() -> None:
    assert check_reference.selftest() == 0


def test_the_guard_reads_the_gitleaks_rules_and_their_fakes() -> None:
    assert len(check_reference.PRIVATE_RULES) >= 4
    # A vendor's address, built here so that no tracked line holds one.
    vendor = ":".join(("dc", "54", "75", "aa", "bb", "01"))
    assert check_reference.private_values(vendor) != []
    # mac-address lets gitleaks' fakes through; kleidos-mac-address lets none.
    fake = ":".join(("02", "00", "00", "12", "34", "56"))
    said = " ".join(check_reference.private_values(fake))
    assert "kleidos-mac-address" in said and "(mac-address:" not in said
    assert check_reference.private_values("/dev/cu." + "usbmodem" + "5A7A0123451") != []


def test_the_guard_says_each_kind_of_private_value() -> None:
    said = " ".join(
        check_reference.private_values(
            "192.168.0.10 10.1.2.3 127.0.0.1 8.8.8.8 bench.local /Users/someone/x"
            " /home/someone/y someone@example.org mail@kleidos.dev"
            ' <a href="https://github.com/jmrplens/kleidos/blob/main/x">x</a>'
        )
    )
    for what in (
        "192.168.0.10",
        "10.1.2.3",
        "bench.local",
        "/Users/someone",
        "/home/someone",
        "someone@example.org",
        "jmrplens/kleidos, which is not public",
    ):
        assert what in said, what
    assert "127.0.0.1" not in said and "8.8.8.8" not in said and "mail@kleidos.dev" not in said


def test_a_page_of_the_site_gets_its_landmarks_and_title() -> None:
    raw = (
        "<html><head><title>Usage · Kleidos bench meter</title></head><body><header>"
        '<nav class="api-nav"><a href="index.html">Overview</a>'
        '<a href="usage_page.html">Usage</a></nav></header>'
        '<div id="doc-content"><div class="header"><div class="headertitle">'
        '<div class="title">Using the meter</div></div></div><div class="contents">'
        '<h1 class="doxsection">Part</h1>'
        '<p><span class="tt"><a href="file:line">file:line</a></span> <a href="https://">x</a></p>'
        '<div class="image"><img src="live.png" alt=""/>'
        '<div class="caption">The live view</div></div>'
        "</div></main></body></html>"
    )
    page = build_reference.mark_current("usage_page.html", build_reference.rewrite_page(raw))
    assert '<main id="doc-content">' in page
    assert '<h1 class="title">Using the meter</h1>' in page
    assert '<h2 class="doxsection">Part</h2>' in page
    assert 'href="file:line"' not in page and 'href="https://"' not in page
    # The caption is the image's text too, and a paragraph, so the text after
    # it keeps its distance.
    assert (
        '<div class="image"><img src="live.png" alt="The live view"/>'
        '<p class="caption">The live view</p></div>'
    ) in page
    assert '<a href="usage_page.html" aria-current="page">' in page
    assert '<a href="index.html">' in page


def test_an_image_s_text_is_its_caption_in_plain_words() -> None:
    raw = (
        '<div class="image">\n<img src="a.png" alt=""/>\n<div class="caption">\n'
        'The <span class="tt">Live</span> view &amp; its "band"</div></div>'
        '<div class="image">\n<img src="b.png" alt=""/>\n</div>'
    )
    page = build_reference.rewrite_page(raw)
    assert '<img src="a.png" alt="The Live view &amp; its &quot;band&quot;"/>' in page
    assert '<p class="caption">\nThe <span class="tt">Live</span> view &amp; its "band"</p>' in page
    # An image with no caption keeps its empty text.
    assert '<img src="b.png" alt=""/>' in page


def test_a_heading_the_filter_renamed_keeps_github_s_anchor_too() -> None:
    page = (
        '<h2 class="doxsection"><a class="anchor" id="scpi_page-the-pin-map"></a>\n'
        "The pin map</h2>"
        '<h2 class="doxsection"><a class="anchor" id="scpi_page-contents"></a>\nContents</h2>'
        '<p><a class="anchor" id="contents"></a></p>'
        '<h2 class="doxsection"><a class="anchor" id="usage_page-sources"></a>\nSources</h2>'
    )
    out = build_reference.github_anchors("scpi_page.html", page)
    assert (
        '<a class="anchor" id="scpi_page-the-pin-map"></a><a class="anchor" id="the-pin-map"></a>'
    ) in out
    # An id the page holds already is not written twice, and another page's
    # name is not this one's.
    assert out.count('id="contents"') == 1
    assert 'id="sources"' not in out


def test_an_index_loses_the_letters_that_lead_nowhere() -> None:
    page = (
        '<nav class="letters" aria-label="By letter"><a class="qindex" href="#index_a">A</a>'
        '<a class="qindex" href="#index_r">R</a></nav><h2><a id="index_r"></a>r</h2>'
    )
    kept = build_reference.drop_dead_letters(page)
    assert 'href="#index_a"' not in kept and 'href="#index_r"' in kept
    assert (
        build_reference.drop_dead_letters(
            '<nav class="letters" aria-label="By letter">'
            '<a class="qindex" href="#index_a">A</a></nav>'
        )
        == ""
    )


def test_the_images_are_the_doxyfile_s() -> None:
    images = build_reference.images_by_name(ROOT)
    assert "live-dark-1280.png" in images and "rig.svg" in images
    assert all(path.is_file() for path in images.values())


def _settings(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {
        m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Z_]+)\s*=\s*(.*)$", text, re.M)
    }


def test_the_overlay_publishes_no_source_no_script_and_the_host_s_design() -> None:
    settings = _settings(ROOT / "Doxyfile.public")
    for name in (
        "SOURCE_BROWSER",
        "INLINE_SOURCES",
        "VERBATIM_HEADERS",
        "SEARCHENGINE",
        "GENERATE_TREEVIEW",
    ):
        assert settings[name] == "NO", name
    assert settings["DISABLE_INDEX"] == "YES"
    assert settings["HTML_HEADER"] == "docs/published-reference/header.html"
    assert settings["HTML_FOOTER"] == "docs/published-reference/footer.html"
    assert settings["LAYOUT_FILE"] == "docs/published-reference/layout.xml"
    assert settings["HTML_OUTPUT"] == build_reference.HTML_OUTPUT
    overlay = (ROOT / "Doxyfile.public").read_text(encoding="utf-8")
    assert overlay.count("@INCLUDE               = Doxyfile") == 1
    assert overlay.count("INPUT                 += docs/published-reference/search.md") == 1


def test_the_header_links_the_host_s_sheets_and_holds_the_note_and_every_bar_entry() -> None:
    header = (ROOT / "docs" / "published-reference" / "header.html").read_text(encoding="utf-8")
    assert "$relpath^../assets/tokens.css" in header and "$relpath^../assets/api.css" in header
    assert check_reference.NOTE in header
    search = check_reference.SEARCH_SCRIPT.replace('"../', '"$relpath^../')
    assert header.count("<script") == 1 and header.count(search) == 1
    assert "style=" not in header
    assert header.count('<a class="search-open" href="$relpath^search_page.html">') == 1
    for target in re.findall(r'href="\$relpath\^([a-z_]+\.html)"', header):
        stem = target.removesuffix(".html")
        assert stem == "index" or stem.endswith("_page"), target
    footer = (ROOT / "docs" / "published-reference" / "footer.html").read_text(encoding="utf-8")
    assert footer.count("</main>") == 1 and "mailto:mail@kleidos.dev" in footer
