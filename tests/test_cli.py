"""Tests for gtfs_diff.cli."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gtfs_diff.cli import main

from tests.helpers import write_zip

STOPS_HEADER = "stop_id,stop_name,stop_lat,stop_lon\n"

TINY_BASE_FILES = {"stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n"}
TINY_NEW_FILES = {
    "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n"
}


def _make_feeds(tmp_path: Path):
    base = write_zip(tmp_path / "base.zip", TINY_BASE_FILES)
    new = write_zip(tmp_path / "new.zip", TINY_NEW_FILES)
    return base, new


class TestHelp:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output or "usage" in result.output.lower()


class TestVersion:
    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestDiffToStdout:
    def test_diff_to_stdout(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "metadata" in data
        assert "summary" in data
        assert "file_diffs" in data


class TestDiffToFile:
    def test_diff_to_file(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        out_file = tmp_path / "result.json"
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert "metadata" in data


class TestCapOption:
    def test_cap_zero_produces_null_row_changes(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--cap", "0"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        for fd in data["file_diffs"]:
            assert fd["row_changes"] is None

    def test_cap_stored_in_metadata(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--cap", "5"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"]["row_changes_cap_per_file"] == 5


class TestInvalidPath:
    def test_invalid_path_exits_nonzero(self, tmp_path: Path):
        base = tmp_path / "nonexistent_base.zip"
        new = tmp_path / "nonexistent_new.zip"
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code != 0


class TestPrettyJson:
    def test_pretty_output_is_indented(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--pretty"])
        assert result.exit_code == 0, result.output
        # Indented JSON contains newlines beyond just the top level
        assert "\n" in result.output
        assert "  " in result.output  # indentation present

    def test_no_pretty_output_is_compact(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--no-pretty"])
        assert result.exit_code == 0, result.output
        # Should be valid JSON even without pretty-printing
        data = json.loads(result.output)
        assert "metadata" in data
