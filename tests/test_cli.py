"""Tests for gtfs-diff.cli."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gtfs_diff import engine_duckdb
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
        assert "gtfs-diff-engine" in result.output


class TestDiffToStdout:
    def test_diff_to_stdout(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
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
        data = json.loads(result.stdout)
        # cap=0 suppresses all row-level detail; row_changes is omitted from JSON
        # (exclude_none=True). True counts are still visible in summary.files.
        for fd in data["file_diffs"]:
            assert "row_changes" not in fd

    def test_cap_stored_in_metadata(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--cap", "5"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["metadata"]["row_changes_cap_per_file"] == 5


class TestMissingPrimaryKeyNotCompared:
    @staticmethod
    def _stops_file_diff(result):
        data = json.loads(result.stdout)
        return next(fd for fd in data["file_diffs"] if fd["file_name"] == "stops.txt")

    def test_succeeds_on_missing_pk_column_in_base(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\n"
                "Stop One,1.0,2.0\n",  # stop_id absent
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code == 0, result.output
        fd = self._stops_file_diff(result)
        assert fd["file_action"] == "not_compared"
        assert fd["not_compared_reason"]["code"] == "missing_primary_key"

    def test_succeeds_on_missing_pk_column_in_new(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\n"
                "Stop One,1.0,2.0\n",  # stop_id absent
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code == 0, result.output
        fd = self._stops_file_diff(result)
        assert fd["file_action"] == "not_compared"
        assert fd["not_compared_reason"]["code"] == "missing_primary_key"

    def test_not_compared_reason_names_missing_column(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\n"
                "Stop One,1.0,2.0\n",  # stop_id absent
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new)])
        assert result.exit_code == 0, result.output
        fd = self._stops_file_diff(result)
        assert fd["file_action"] == "not_compared"
        assert fd["not_compared_reason"]["code"] == "missing_primary_key"
        assert "stop_id" in fd["not_compared_reason"]["message"]


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
        assert "\n" in result.stdout
        assert "  " in result.stdout  # indentation present

    def test_no_pretty_output_is_compact(self, tmp_path: Path):
        base, new = _make_feeds(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, [str(base), str(new), "--no-pretty"])
        assert result.exit_code == 0, result.output
        # Should be valid JSON even without pretty-printing
        data = json.loads(result.stdout)
        assert "metadata" in data


def _make_feed_dirs(tmp_path: Path):
    base = tmp_path / "base_dir"
    new = tmp_path / "new_dir"
    base.mkdir()
    new.mkdir()
    (base / "stops.txt").write_text(TINY_BASE_FILES["stops.txt"], encoding="utf-8")
    (new / "stops.txt").write_text(TINY_NEW_FILES["stops.txt"], encoding="utf-8")
    return base, new


def _json_without_metadata(output: str) -> dict:
    data = json.loads(output)
    data.pop("metadata", None)
    return data


class TestDuckDBOptions:
    def test_no_duckdb_is_accepted_and_disables_backend(
        self, tmp_path: Path, monkeypatch
    ):
        base, new = _make_feed_dirs(tmp_path)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("DuckDB should not be called")

        monkeypatch.setattr(engine_duckdb, "diff_modified_duckdb", fail_if_called)
        result = CliRunner().invoke(main, [str(base), str(new), "--no-duckdb"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["total_changes"] == 1
        assert data["file_diffs"][0]["stats"]["rows_added_count"] == 1

    def test_large_file_threshold_zero_is_accepted_and_uses_duckdb(
        self, tmp_path: Path, monkeypatch
    ):
        base, new = _make_feed_dirs(tmp_path)
        original = engine_duckdb.diff_modified_duckdb
        calls = []

        def record_call(*args, **kwargs):
            calls.append(kwargs["file_name"])
            return original(*args, **kwargs)

        monkeypatch.setattr(engine_duckdb, "diff_modified_duckdb", record_call)
        duck = CliRunner().invoke(
            main, [str(base), str(new), "--large-file-threshold-mb", "0"]
        )
        assert duck.exit_code == 0, duck.output
        assert calls == ["stops.txt"]

        normal = CliRunner().invoke(main, [str(base), str(new), "--no-duckdb"])
        assert normal.exit_code == 0, normal.output
        assert _json_without_metadata(duck.stdout) == _json_without_metadata(
            normal.stdout
        )
