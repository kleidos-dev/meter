# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""One link form, read the same on GitHub and on the documentation site.

The Markdown pages are the one source of the documentation site (``Doxyfile``)
and still read on GitHub. A link is written one way, the way GitHub reads it:
a path relative to the file, with a heading's anchor where it goes to one.
``scripts/doxygen_md_filter.py`` turns it into the site's link as Doxygen
reads each page. ``test_markdown_links.py`` holds that every such link has its
file and its heading; this guard holds what the site needs besides:

* the pages ``Doxyfile`` reads are the pages the filter knows, and the page
  names it gives are unique and none is a name of the firmware reference;
* a link to a Markdown file goes to a page of the site;
* no link into the tree is written as the site's (``.html``) or as GitHub's
  address of this repository's branch, which would read on one and not the
  other (a permalink to a commit, which ``scripts/check_doc_paths.py`` asks
  of a file that is gone, is no link into the tree);
* every image's file name is unique across ``IMAGE_PATH`` (Doxygen finds an
  image by its name) and every image a page names is there;
* what the filter makes of each kind of link, of the comments before the
  title and of a heading another page shares, and that a link to a file the
  repository does not hold becomes a reference Doxygen cannot resolve, so
  the gate (``mkdir -p build && doxygen Doxyfile``) fails on it at the
  page's line;
* a link whose text is its target's file reads on the site as that page's
  name, and every page has one;
* a page that describes the instrument as it is (any page but the records the
  index lists under "History") sends its reader nowhere a stranger cannot
  go: no git-ignored file, no private plan, no computer of the bench, no line
  of a commit the publication will not keep.

The links of the finished site are read again in its HTML, by
``scripts/check_doc_links.py`` and ``scripts/check_reference.py``.

Host-only: the files read as text.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import doxygen_md_filter as mdf  # noqa: E402

#: An inline link or image of a page, outside code: its target.
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _doxyfile_setting(name: str) -> list[str]:
    text = (ROOT / "Doxyfile").read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert match, f"Doxyfile has no {name}"
    return match.group(1).replace("\\\n", " ").split()


def _prose(text: str) -> list[tuple[int, str]]:
    """The lines outside fenced blocks, numbered, code spans blanked."""
    lines = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        lines.append((number, re.sub(r"(`+)(?:(?!\1).)+?\1", "", line)))
    return lines


def _links() -> list[tuple[str, int, str]]:
    found = []
    for page in mdf.page_files(ROOT):
        for number, line in _prose((ROOT / page).read_text(encoding="utf-8")):
            found += [(page, number, target) for target in LINK.findall(line)]
    return found


def test_the_doxyfile_reads_the_pages_the_filter_knows() -> None:
    read: set[str] = set()
    for entry in _doxyfile_setting("INPUT"):
        path = ROOT / entry
        if path.is_dir():
            read |= {f"{entry}/{p.name}" for p in path.glob("*.md")}
        elif entry.endswith(".md"):
            read.add(entry)
    assert read == set(mdf.page_files(ROOT))
    assert set(mdf.PAGE_DIRS) <= set(_doxyfile_setting("INPUT"))
    assert _doxyfile_setting("USE_MDFILE_AS_MAINPAGE") == [mdf.MAIN_PAGE]


def test_every_page_name_is_unique_and_no_name_of_the_reference() -> None:
    names = [mdf.page_name(page) for page in mdf.page_files(ROOT)]
    twice = [name for name, count in Counter(names).items() if count > 1]
    assert not twice, f"two pages named {twice}"
    groups = set(
        re.findall(r"@defgroup\s+(\w+)", (ROOT / "docs" / "groups.dox").read_text(encoding="utf-8"))
    )
    assert not set(names) & groups
    assert names.count("index") == 1


def test_a_markdown_link_goes_to_a_page_of_the_site() -> None:
    pages = set(mdf.page_files(ROOT))
    wrong = []
    for page, number, target in _links():
        path = target.partition("#")[0]
        if SCHEME.match(target) or not path.endswith(".md"):
            continue
        resolved = (ROOT / page).parent.joinpath(path).resolve().relative_to(ROOT).as_posix()
        if resolved not in pages:
            wrong.append(f"{page}:{number}: {target} is no page of the site (Doxyfile INPUT)")
    assert not wrong, "\n".join(wrong)


def test_no_link_is_written_as_the_site_or_as_this_repository_on_github() -> None:
    wrong = []
    for page, number, target in _links():
        path = target.partition("#")[0]
        if not SCHEME.match(target) and path.endswith(".html"):
            wrong.append(f"{page}:{number}: {target} (the site's form; write the .md)")
        if re.match(rf"{re.escape(mdf.REPOSITORY)}/(?:blob|tree)/{mdf.BRANCH}/", target):
            wrong.append(
                f"{page}:{number}: {target} (this repository on GitHub; write the relative path)"
            )
    assert not wrong, "\n".join(wrong)


def test_every_image_name_is_unique_across_the_image_path() -> None:
    names: list[str] = []
    for folder in _doxyfile_setting("IMAGE_PATH"):
        assert (ROOT / folder).is_dir(), f"IMAGE_PATH names {folder}, which is no folder"
        names += [
            p.name for p in (ROOT / folder).iterdir() if p.suffix.lower() in mdf.IMAGE_SUFFIXES
        ]
    twice = [name for name, count in Counter(names).items() if count > 1]
    assert not twice, f"two images named {twice}: Doxygen would take either"


def test_every_image_a_page_names_is_on_the_image_path() -> None:
    held = {
        p.resolve()
        for folder in _doxyfile_setting("IMAGE_PATH")
        for p in (ROOT / folder).iterdir()
        if p.suffix.lower() in mdf.IMAGE_SUFFIXES
    }
    wrong = []
    for page, number, target in _links():
        path = target.partition("#")[0]
        if SCHEME.match(target) or Path(path).suffix.lower() not in mdf.IMAGE_SUFFIXES:
            continue
        if (ROOT / page).parent.joinpath(path).resolve() not in held:
            wrong.append(f"{page}:{number}: {target} is in no folder of IMAGE_PATH")
    assert not wrong, "\n".join(wrong)


# --- The filter --------------------------------------------------------------

PAGES = frozenset(
    {"docs/README.md", "docs/usage.md", "docs/scpi.md", "README.md", "calibration/README.md"}
)


def _filter(text: str, source: str = "docs/usage.md", shared: frozenset[str] = frozenset()) -> str:
    return mdf.filtered(text, source, PAGES, ROOT, shared)


def test_the_filter_names_each_page_after_its_file() -> None:
    assert mdf.page_name("docs/rig-wiring.md") == "rig_wiring_page"
    assert mdf.page_name("calibration/README.md") == "calibration_page"
    assert mdf.page_name("README.md") == "readme_page"
    assert mdf.page_name("calibration/records/radio-ab-2026-11-21.md") == (
        "calibration_record_radio_ab_2026_11_21_page"
    )
    assert mdf.page_name("docs/README.md") == "index"


def test_the_filter_blanks_the_comments_before_the_title_and_keeps_every_line() -> None:
    text = "\n".join(
        [
            "<!-- the holder's line -->",
            "<!-- the license's line -->",
            "",
            "<!-- BEGIN GENERATED: banner -->",
            "<picture><img src='a.svg' alt='a'></picture>",
            "<!-- END GENERATED: banner -->",
            "",
            "# Using the meter",
            "<!-- a comment in the text stays -->",
        ]
    )
    out = _filter(text).split("\n")
    assert len(out) == len(text.split("\n"))
    assert out[:7] == [""] * 7
    assert out[7] == "# Using the meter {#usage_page}"
    assert out[8] == "<!-- a comment in the text stays -->"


def test_the_main_page_gets_no_name() -> None:
    assert _filter("# The documents", "docs/README.md") == "# The documents"


def test_the_filter_writes_each_kind_of_link_as_the_site_reads_it() -> None:
    line = (
        "[a](scpi.md#the-pin-map) [b](../README.md) [c](#install)"
        " [d](images/panel/live-dark-1280.png) ![e](images/panel/live-dark-390.png)"
        " [f](firmware.dox) [g](images/panel/) [h](../firmware/src/main.cpp)"
        " [i](https://kleidos.dev/) [j](mailto:mail@kleidos.dev) [k](scpi.md#24-the-own)"
    )
    out = _filter(line)
    assert '<a href="scpi_page.html#the-pin-map">a</a>' in out
    assert '<a href="readme_page.html">b</a>' in out
    assert '<a href="#install">c</a>' in out
    assert '<a href="live-dark-1280.png">d</a>' in out
    assert "![e](live-dark-390.png)" in out
    assert '<a href="firmware_page.html">f</a>' in out
    assert f"[g]({mdf.REPOSITORY}/tree/main/docs/images/panel)" in out
    assert f"[h]({mdf.REPOSITORY}/blob/main/firmware/src/main.cpp)" in out
    assert "[i](https://kleidos.dev/)" in out and "[j](mailto:mail@kleidos.dev)" in out
    assert '<a href="scpi_page.html#autotoc_md24-the-own">k</a>' in out


def test_a_link_to_a_file_that_is_not_there_fails_the_doxygen_gate() -> None:
    gone: list[tuple[int, str, str]] = []
    text = (
        "# A page\n\nSee [the protocol](scpi-gone.md) and [the code](../firmware/src/main.cpp).\n"
    )
    out = mdf.filtered(text, "docs/usage.md", PAGES, ROOT, frozenset(), gone)
    assert 'See the protocol @ref "docs/scpi-gone.md" and' in out
    assert f"[the code]({mdf.REPOSITORY}/blob/main/firmware/src/main.cpp)" in out
    assert gone == [(3, "scpi-gone.md", "docs/scpi-gone.md")]
    assert mdf.missing_file("docs/usage.md", "scpi.md#no-such-heading", PAGES, ROOT) is None
    assert mdf.missing_file("docs/usage.md", "images/gone.png", PAGES, ROOT) is None
    assert mdf.missing_file("docs/usage.md", "../firmware/src/gone.cpp#L4", PAGES, ROOT) == (
        "firmware/src/gone.cpp"
    )
    assert mdf.warning("/x/docs/usage.md", 3, "scpi-gone.md", "docs/scpi-gone.md") == (
        "/x/docs/usage.md:3: warning: the link scpi-gone.md names docs/scpi-gone.md,"
        " which is not in the repository"
    )


def test_a_link_in_code_is_no_link_and_code_in_a_link_stays_in_it() -> None:
    line = '`[x](scpi.md)` and [`scpi.md`, "The pin map"](scpi.md#the-pin-map)'
    assert (
        _filter(line)
        == '`[x](scpi.md)` and <a href="scpi_page.html#the-pin-map">`scpi.md`, "The pin map"</a>'
    )
    fenced = "```text\n[x](scpi.md)\n```"
    assert _filter(fenced) == fenced


def test_a_shared_heading_is_named_by_its_page_and_so_is_a_link_to_it() -> None:
    text = (
        "# Using\n\n## What you need\n\n## Install\n\n"
        "See [w](#what-you-need) and [p](scpi.md#what-you-need)."
    )
    out = _filter(text, shared=frozenset({"what-you-need"})).split("\n")
    assert out[2] == "## What you need {#usage_page-what-you-need}"
    assert out[4] == "## Install"
    assert out[6] == (
        'See <a href="#usage_page-what-you-need">w</a> and'
        ' <a href="scpi_page.html#scpi_page-what-you-need">p</a>.'
    )


def test_the_anchors_are_github_s() -> None:
    assert (
        mdf.github_slug("2.4 The own I2C master (FW-13, FW-14)")
        == "24-the-own-i2c-master-fw-13-fw-14"
    )
    assert mdf.github_slug("Is the number good: `q=`, `path=` and `check=`") == (
        "is-the-number-good-q-path-and-check"
    )
    ids = mdf.heading_ids("# T\n## Notes\n```\n## not a heading\n```\n## Notes\n")
    assert [slug for _, slug in ids] == ["notes", "notes-1"]
    assert mdf.heading_id("7-the-two-usb-c-ports") == "autotoc_md7-the-two-usb-c-ports"


def test_the_shared_anchors_are_found_across_the_pages() -> None:
    shared = mdf.shared_ids(ROOT)
    assert "contents" in shared
    for anchor in shared:
        holders = [
            page
            for page in mdf.page_files(ROOT)
            if anchor
            in {slug for _, slug in mdf.heading_ids((ROOT / page).read_text(encoding="utf-8"))}
        ]
        assert holders, anchor


def test_every_page_has_a_name_for_a_link_that_names_its_file() -> None:
    assert set(mdf.LINK_NAMES) == set(mdf.page_files(ROOT)) | set(mdf.DOX_PAGES)
    names = list(mdf.LINK_NAMES.values())
    assert len(names) == len(set(names)), "two pages share a link name"
    assert all(name == name.strip() and name[:1].islower() for name in names)


def test_a_link_whose_text_is_its_file_reads_as_the_page_s_name() -> None:
    out = _filter(
        "See [usage.md](usage.md), [`scpi.md`](scpi.md), [README.md](../README.md)"
        " and [the pin map](scpi.md)."
    )
    assert '<a href="usage_page.html">the usage guide</a>' in out
    assert '<a href="scpi_page.html">the SCPI command set</a>' in out
    assert "`scpi.md`" not in out
    assert '<a href="readme_page.html">the repository\'s README</a>' in out
    assert '<a href="scpi_page.html">the pin map</a>' in out
    # The path from the root, or the file's name alone, is the file too.
    assert _filter("A [docs/scpi.md](scpi.md) b") == (
        'A <a href="scpi_page.html">the SCPI command set</a> b'
    )
    every = frozenset(mdf.page_files(ROOT))
    line = "x [2026-11-20.md](../calibration/records/2026-11-20.md)"
    assert mdf.filtered(line, "docs/README.md", every, ROOT) == (
        'x <a href="calibration_record_2026_11_20_page.html">the record of 2026-11-20</a>'
    )


def test_a_page_s_name_that_opens_a_sentence_a_cell_or_an_item_is_capitalized() -> None:
    assert _filter("[usage.md](usage.md) is first.").startswith(
        '<a href="usage_page.html">The usage guide</a>'
    )
    assert '| <a href="scpi_page.html">The SCPI command set</a> |' in _filter(
        "| [scpi.md](scpi.md) | The protocol |"
    )
    assert _filter("- [scpi.md](scpi.md)") == '- <a href="scpi_page.html">The SCPI command set</a>'
    assert _filter("Done. [scpi.md](scpi.md) next") == (
        'Done. <a href="scpi_page.html">The SCPI command set</a> next'
    )
    assert _filter("in [scpi.md](scpi.md)") == (
        'in <a href="scpi_page.html">the SCPI command set</a>'
    )
    assert _filter("- **[scpi.md](scpi.md) rules**") == (
        '- **<a href="scpi_page.html">The SCPI command set</a> rules**'
    )


def test_a_link_to_a_heading_whose_text_is_its_file_names_the_heading_and_the_page() -> None:
    assert _filter("(0.18.0; [docs/scpi.md](scpi.md#the-pin-map), x)") == (
        '(0.18.0; <a href="scpi_page.html#the-pin-map">\u201cThe pin map\u201d in the SCPI'
        " command set</a>, x)"
    )
    # A section mark after the link names the section already.
    assert _filter("is [scpi.md](scpi.md#the-pin-map) \u00a72") == (
        'is <a href="scpi_page.html#the-pin-map">the SCPI command set</a> \u00a72'
    )
    assert _filter("of [firmware.dox](firmware.dox)") == (
        'of <a href="firmware_page.html">the firmware\'s front page</a>'
    )


#: What a stranger reading the published site cannot reach.
UNREACHABLE = (
    re.compile(r"\bplan/"),
    re.compile(r"qa/inventory/\.env"),
    re.compile(r"git-?ignored `?\.env"),
    re.compile(r"\bthis Mac\b"),
    re.compile(r"\bc594eec"),
    re.compile(r"hub-ports-\d{4}-\d{2}-\d{2}\.json"),
    re.compile(r"(?:external-power-meter-options|consumption-self-measurement)\.md:\d"),
    re.compile(r"\bwiring-(?:breakout|dut-path|controller)\.md"),
    re.compile(r"(?:ina228-board|esp32s3-n16r8-dsx063)\.jpg"),
    re.compile(r"\b[Tt]eam notes?\b"),
)


def test_no_page_sends_its_reader_where_a_stranger_cannot_go() -> None:
    """Every page, the records under "History" included (owner, 2026-11-27)."""
    found = [
        f"{page}:{number}: {pattern.pattern}"
        for page in mdf.page_files(ROOT)
        for number, line in enumerate(
            (ROOT / page).read_text(encoding="utf-8").splitlines(), start=1
        )
        for pattern in UNREACHABLE
        if pattern.search(line)
    ]
    assert not found, "\n".join(found)
