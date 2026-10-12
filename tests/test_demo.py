from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["basic", "package", "rename"])
def test_generated_demo_matches_reviewable_report(tmp_path, mode):
    project = Path(__file__).resolve().parents[1]
    destination = tmp_path / "demo"
    command = [sys.executable, str(project / "examples/demo.py"), "--destination", str(destination)]
    if mode != "basic":
        command.append("--" + mode)
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ripple",
            "analyze",
            "--repo",
            str(destination),
            "--format",
            "json",
            "--strict",
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (4 if mode == "rename" else 0), result.stderr
    actual = json.loads(result.stdout)
    filename = "demo.json" if mode == "basic" else mode + "-demo.json"
    expected = json.loads((project / "examples/reports" / filename).read_text(encoding="utf-8"))

    def portable_evidence(report):
        return {
            "summary": report["summary"],
            "completeness": report["completeness"],
            "changes": [
                (c["display_name"], c["kind"], c["before"], c["after"]) for c in report["changes"]
            ],
            "file_changes": [
                (f["status"], f["before_path"], f["after_path"], f["similarity"])
                for f in report["file_changes"]
            ],
            "impacts": [
                (
                    i["display_name"],
                    i["depth"],
                    i["provenance"],
                    [(e["snapshot"], e["steps"]) for e in i["evidence_paths"]],
                )
                for i in report["impacts"]
            ],
        }

    assert portable_evidence(actual) == portable_evidence(expected)
    # Running the helper a second time must refuse to overwrite existing content.
    source = (
        destination
        / {"basic": "service.py", "package": "src/shop/pricing.py", "rename": "totals.py"}[mode]
    )
    before = source.read_bytes()
    assert subprocess.run(command, capture_output=True, timeout=30).returncode != 0
    assert source.read_bytes() == before
