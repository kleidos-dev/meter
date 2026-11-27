# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""gitleaks catches the private bench data, and not the documents' placeholders.

``.gitleaks.toml`` adds three rules to gitleaks' own: a serial port name that
carries a USB serial number, that serial as macOS prints it, and a MAC
address. Each rule is compiled here and held to samples it must catch and to
the placeholders the documents write instead, which it must pass; with the
gitleaks binary on PATH (CI's lint job has it, the pre-commit hook brings its
own) the same samples are scanned for real, so a rule gitleaks cannot load
fails too. ``.gitleaksignore`` may only name findings of the history, never
one the working tree still holds.

The samples are built at run time, so this file itself holds none and the
scan of the tree stays clean.

Host-only: the configuration read with tomllib; gitleaks when installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"
IGNORE = ROOT / ".gitleaksignore"

#: Made-up values: hex where a real serial or MAC is hex, never a bench's.
SERIAL = "".join(["1A2B", "3C4D", "5E"])
MAC_BYTES = ["02", "1a", "2b", "3c", "4d", "5e"]

#: Rule id -> text it must catch.
CAUGHT = {
    "kleidos-usb-serial-port": [
        "/dev/" + "cu." + "usbmodem" + SERIAL + "1",
        "/dev/" + "tty." + "usbserial-" + SERIAL,
        "/dev/" + "cu." + "wchusbserial" + SERIAL,
        "/dev/serial/by-id/" + "usb-1a86" + "_USB_Single_Serial_" + SERIAL + "-if00",
    ],
    "kleidos-usb-serial-number": [
        '"USB Serial' + ' Number" = "' + SERIAL + '"',
        "Serial" + " Number: " + SERIAL,
    ],
    "kleidos-mac-address": [":".join(MAC_BYTES), "-".join(MAC_BYTES).upper()],
}
#: Text every rule must pass: what the documents write instead.
PASSED = [
    "export KLEIDOS_METER_PORT=/dev/cu.usbmodem<serial>1",
    "kleidos-meter --port /dev/cu.usbmodemXXXX1 '*IDN?'",
    "a `cu.usbmodem<serial>1` appearing is the CH343 port",
    "`ioreg` shows a `USB Serial Number` (private bench data, not recorded here)",
    "instrument `KM-0000000000`, 15:37:35 to 20:40:15",
    "the elf=5e1f0ba47c2291d3 key",
]


def rules() -> dict[str, re.Pattern[str]]:
    """The repository's own rules, compiled."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert config["extend"]["useDefault"] is True, "gitleaks' own rules are kept"
    return {rule["id"]: re.compile(rule["regex"]) for rule in config["rules"]}


def test_each_rule_catches_its_samples() -> None:
    compiled = rules()
    assert set(compiled) == set(CAUGHT)
    for rule, samples in CAUGHT.items():
        for sample in samples:
            assert compiled[rule].search(sample), f"{rule} misses {sample!r}"


def test_no_rule_fires_on_the_documents_placeholders() -> None:
    for rule, pattern in rules().items():
        for text in PASSED:
            assert not pattern.search(text), f"{rule} fires on {text!r}"


def test_the_ignore_file_names_only_history() -> None:
    entries = [
        line
        for line in IGNORE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert entries
    for entry in entries:
        commit, path, rule, line = entry.split(":")
        assert re.fullmatch(r"[0-9a-f]{40}", commit), f"{entry}: a commit, so history only"
        assert rule in CAUGHT or rule.startswith("generic"), entry
        current = (ROOT / path).read_text(encoding="utf-8") if (ROOT / path).exists() else ""
        assert not rules()[rule].search(current), f"{path} still holds a {rule} finding: fix it"
        assert int(line) > 0


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not on PATH")
def test_gitleaks_itself_reads_the_rules(tmp_path: Path) -> None:
    caught = tmp_path / "caught"
    caught.mkdir()
    for rule, samples in CAUGHT.items():
        (caught / f"{rule}.md").write_text("\n".join(samples) + "\n", encoding="utf-8")
    passed = tmp_path / "passed"
    passed.mkdir()
    (passed / "placeholders.md").write_text("\n".join(PASSED) + "\n", encoding="utf-8")
    command = ["gitleaks", "dir", "--config", str(CONFIG), "--no-banner", "--redact"]
    found = subprocess.run([*command, str(caught)], capture_output=True, text=True, check=False)
    assert found.returncode == 1, found.stderr
    count = sum(len(samples) for samples in CAUGHT.values())
    assert f"leaks found: {count}" in found.stderr, found.stderr
    clean = subprocess.run([*command, str(passed)], capture_output=True, text=True, check=False)
    assert clean.returncode == 0, clean.stderr
