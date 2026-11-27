# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A change to what a firmware-family job reads runs that job.

``📋 What changed`` sets ``firmware`` false when every changed path matches
``not_firmware`` and none matches ``build_guards``, and the
``static-analysis`` and ``firmware`` jobs then skip; ``docs`` is ``firmware``
or true when a changed path matches ``docs_inputs`` (the documentation site's
pages, images, settings and scripts), and the ``docs`` job runs when either
is; ``panel`` is false when every
changed path matches ``not_panel`` and none matches ``panel_runners`` or
``panel_inputs``, and the Playwright job skips. These tests run the
filter's own expressions on single paths, as the workflow runs them with
``grep -E``, and hold every path those jobs read to the family that runs
them: each script and test a job's steps run, and each input of the
documentation site (``Doxyfile``'s ``INPUT``), which the ``docs`` job builds.

Host-only: the workflow read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
DOXYFILE = ROOT / "Doxyfile"


def pattern(name: str) -> re.Pattern[str]:
    """The filter's expression *name*, as the workflow assigns it."""
    match = re.search(rf"^\s*{name}='(.*)'$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"check.yml assigns no {name}"
    return re.compile(match.group(1))


def firmware_runs(*changed: str) -> bool:
    """Whether a change of exactly *changed* sets ``firmware=true``."""
    not_firmware, guards = pattern("not_firmware"), pattern("build_guards")
    return any(not not_firmware.search(p) for p in changed) or any(
        guards.search(p) for p in changed
    )


def docs_runs(*changed: str) -> bool:
    """Whether a change of exactly *changed* runs the ``docs`` job."""
    inputs = pattern("docs_inputs")
    return firmware_runs(*changed) or any(inputs.search(p) for p in changed)


def firmware_jobs() -> dict[str, str]:
    """Each job gated on the firmware family, by id, its text without comments."""
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = text[text.index("\njobs:\n") :]
    found = {}
    header = r"^  ([a-z][a-z0-9_-]*):\n"
    for match in re.finditer(header + r"(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)", jobs, re.M | re.S):
        body = match.group(2)
        if "needs.changes.outputs.firmware == 'true'" in body:
            found[match.group(1)] = "\n".join(
                line for line in body.splitlines() if not line.lstrip().startswith("#")
            )
    return found


def read_by_firmware_jobs(docs: bool = False) -> set[str]:
    """Every script and test file the firmware family's jobs run; with *docs*, the docs job's."""
    paths: set[str] = set()
    for job, body in firmware_jobs().items():
        if (job == "docs") == docs:
            paths.update(re.findall(r"(?<![\w/.-])((?:scripts|tests)/[\w/.-]+\.(?:py|sh))", body))
    return paths


def doxygen_inputs() -> list[str]:
    """The files and folders ``Doxyfile``'s ``INPUT`` names."""
    text = DOXYFILE.read_text(encoding="utf-8")
    match = re.search(r"^INPUT\s*=\s*((?:.*\\\n)*.*)$", text, re.MULTILINE)
    assert match, "Doxyfile has no INPUT"
    return match.group(1).replace("\\\n", " ").split()


def test_the_family_jobs_are_found() -> None:
    assert {"docs", "static-analysis", "firmware"} <= set(firmware_jobs())
    assert "scripts/check_heap_reachability.py" in read_by_firmware_jobs()


@pytest.mark.parametrize("path", sorted(read_by_firmware_jobs()))
def test_a_change_to_a_script_or_test_a_family_job_runs_runs_it(path: str) -> None:
    assert firmware_runs(path), f"a change to {path} alone skips the job that runs it"


@pytest.mark.parametrize("path", sorted(read_by_firmware_jobs(docs=True)))
def test_a_change_to_a_script_or_test_the_docs_job_runs_runs_it(path: str) -> None:
    assert docs_runs(path), f"a change to {path} alone skips the docs job"


def _inner(path: str) -> str:
    """A file the folder *path* of ``INPUT`` gives the site, or *path* itself."""
    folder = ROOT / path
    if not folder.is_dir():
        return path
    if path == "firmware/src":
        return f"{path}/main.cpp"
    return next(f"{path}/{p.name}" for p in sorted(folder.glob("*.md")))


@pytest.mark.parametrize("path", doxygen_inputs())
def test_a_change_to_an_input_of_the_site_runs_the_docs_job(path: str) -> None:
    inner = _inner(path)
    assert docs_runs(inner), f"a change to {inner} alone skips the docs job"
    assert docs_runs("Doxyfile") and docs_runs("Doxyfile.public")


@pytest.mark.parametrize(
    "path",
    [
        "docs/published-reference/header.html",
        "scripts/published_reference/api_reference_pages.py",
        "docs/images/panel/live-dark-1280.png",
        "hardware/rig.svg",
        ".gitleaks.toml",
    ],
)
def test_a_change_to_what_the_published_site_reads_runs_the_docs_job(path: str) -> None:
    assert docs_runs(path), f"a change to {path} alone skips the docs job"


def test_a_change_to_the_host_alone_skips_the_docs_job() -> None:
    assert not docs_runs("host/kleidos_meter/cli.py", "tests/firmware/test_readme.py")


def panel_runs(*changed: str) -> bool:
    """Whether a change of exactly *changed* sets ``panel=true``."""
    excluded = pattern("not_panel")
    added = (pattern("panel_runners"), pattern("panel_inputs"))
    return any(not excluded.search(p) or any(a.search(p) for a in added) for p in changed)


@pytest.mark.parametrize(
    "path",
    ["scripts/panel_images.py", "docs/images/panel/manifest.json", "host/tests/panel_shots.py"],
)
def test_a_change_to_what_the_images_browser_half_reads_runs_the_panel_job(path: str) -> None:
    """``test_panel_browser.py`` signs every image's view against the manifest by
    ``scripts/panel_images.py``: either changed alone runs it; the firmware's jobs read neither."""
    assert panel_runs(path), f"a change to {path} alone skips the panel job"
    assert not firmware_runs(path)


def test_the_images_alone_run_neither_family() -> None:
    """A PNG retaken, the script that takes them and the guard over their files are no input
    of the page's tests or of the image: the tests job runs the guard."""
    changed = ("docs/images/panel/live-dark-1280.png", "scripts/take_panel_shots.py")
    assert not panel_runs(*changed)
    assert not firmware_runs(*changed, "scripts/media_meta.py")


def test_a_change_to_prose_alone_still_skips_the_family() -> None:
    """The exclusions still do their work: docs and host code are not the image's."""
    assert not firmware_runs("docs/usage.md", "host/kleidos_meter/cli.py", "README.md")
    assert not firmware_runs("scripts/check_spdx.py", "tests/firmware/test_readme.py")
