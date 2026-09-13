"""Tests for the command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import write
from typer.testing import CliRunner

from ov_sync.cli import app, is_settled
from ov_sync.config import CONFIG_FILENAME
from ov_sync.engine import SkippedFile, SyncResult

BASE = "https://openviking.example/api/v1"
ROOT = "viking://resources/notes"

runner = CliRunner()


def ok(result: Any) -> httpx.Response:
    """Build a successful OpenViking response envelope."""
    return httpx.Response(200, json={"status": "ok", "result": result, "error": None})


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI at a server respx will intercept."""
    monkeypatch.setenv("OV_SYNC_URL", "https://openviking.example")
    monkeypatch.setenv("OV_SYNC_API_KEY", "test-key")


def test_a_pending_deletion_is_not_up_to_date() -> None:
    """Saying so one line after reporting it would contradict itself."""
    result = SyncResult(deleted_detected=[f"{ROOT}/gone.md"])

    assert is_settled(result) is False


def test_a_skipped_file_is_not_up_to_date() -> None:
    """An oversize file is outstanding work, not a finished sync."""
    result = SyncResult(skipped=[SkippedFile(relative_path="huge.bin", reason="too big")])

    assert is_settled(result) is False


@pytest.mark.parametrize(
    "result",
    [
        SyncResult(),
        SyncResult(unchanged=5),
        SyncResult(touched=2, unchanged=3),
    ],
)
def test_nothing_outstanding_is_up_to_date(result: SyncResult) -> None:
    """Unchanged and touched files leave nothing to do."""
    assert is_settled(result) is True


def test_init_writes_a_starter_config(folder: Path) -> None:
    """The generated file is valid TOML the loader accepts."""
    outcome = runner.invoke(app, ["init", str(folder)])

    assert outcome.exit_code == 0
    assert (folder / CONFIG_FILENAME).is_file()


def test_init_refuses_to_overwrite(folder: Path) -> None:
    """Clobbering a config someone tuned would be a bad surprise."""
    (folder / CONFIG_FILENAME).write_text("[sync]\n", encoding="utf-8")

    outcome = runner.invoke(app, ["init", str(folder)])

    assert outcome.exit_code == 1
    assert "already exists" in outcome.output


def test_run_without_a_target_says_how_to_set_one(folder: Path, configured: None) -> None:
    """The first run needs a root, and the error names both ways to give it."""
    outcome = runner.invoke(app, ["run", str(folder)])

    assert outcome.exit_code == 1
    assert "--root-uri" in outcome.output


@respx.mock
def test_run_stops_when_the_target_does_not_exist(folder: Path, configured: None) -> None:
    """The error carries the exact `ov mkdir` that fixes it."""
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            404,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "NOT_FOUND", "message": "gone", "details": None},
            },
        )
    )

    outcome = runner.invoke(app, ["run", str(folder), "--root-uri", ROOT])

    assert outcome.exit_code == 1
    assert f"ov mkdir {ROOT}" in outcome.output


@respx.mock
def test_dry_run_lists_the_work_without_doing_it(folder: Path, configured: None) -> None:
    """Only the root check reaches the server."""
    write(folder / "note.md", "hello")
    stat = respx.get(f"{BASE}/fs/stat").mock(
        return_value=ok({"name": "notes", "size": 0, "isDir": True})
    )
    batch = respx.post(f"{BASE}/content/batch-write")

    outcome = runner.invoke(app, ["run", str(folder), "--root-uri", ROOT, "--dry-run"])

    assert outcome.exit_code == 0
    assert "note.md" in outcome.output
    assert stat.called
    assert not batch.called


def test_a_bracketed_name_survives_the_report(folder: Path) -> None:
    """Rich reads brackets as style tags, so an unescaped name loses them.

    `[#TDAI-709] plan.pdf` printed as ` plan.pdf`, which is how the file that
    started all this looked in the error that reported it.
    """
    write(folder / "[#TDAI-709] plan.md", "hello")

    outcome = runner.invoke(app, ["status", str(folder), "--root-uri", ROOT])

    assert "[#TDAI-709] plan.md" in outcome.output
    assert "[_TDAI-709] plan.md" in outcome.output


def test_a_name_cannot_smuggle_markup_into_the_report(folder: Path) -> None:
    """A file called `[bold]...` must not restyle the rest of the output."""
    write(folder / "[bold]loud.md", "hello")

    outcome = runner.invoke(app, ["status", str(folder), "--root-uri", ROOT])

    assert "[bold]loud.md" in outcome.output


def test_init_keeps_the_toml_section_it_points_at(folder: Path) -> None:
    """`[sync]` is the section to edit, and rich used to swallow it whole."""
    outcome = runner.invoke(app, ["init", str(folder)])

    assert "[sync]" in outcome.output


def test_status_never_contacts_the_server(folder: Path) -> None:
    """Status reads the local state only, so it works offline.

    respx is not installed here on purpose: any HTTP call would fail the test
    by raising, rather than quietly succeeding against a mock.
    """
    write(folder / "note.md", "hello")

    outcome = runner.invoke(app, ["status", str(folder), "--root-uri", ROOT])

    assert outcome.exit_code == 0
    assert "never" in outcome.output


def test_status_needs_no_credentials(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder can be inspected before the server is configured at all."""
    monkeypatch.delenv("OV_SYNC_URL", raising=False)
    monkeypatch.delenv("OV_SYNC_API_KEY", raising=False)

    outcome = runner.invoke(app, ["status", str(folder), "--root-uri", ROOT])

    assert outcome.exit_code == 0


@respx.mock
def test_create_root_makes_a_missing_target(folder: Path, configured: None) -> None:
    """The first run should not have to leave the tool to get started."""
    write(folder / "note.md", "hello")
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            404,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "NOT_FOUND", "message": "gone", "details": None},
            },
        )
    )
    mkdir = respx.post(f"{BASE}/fs/mkdir").mock(return_value=ok({"uri": ROOT}))
    respx.post(f"{BASE}/content/batch-write").mock(
        return_value=ok(
            {
                "root_uri": ROOT,
                "created": [f"{ROOT}/note.md"],
                "updated": [],
                "unchanged": [],
            }
        )
    )

    outcome = runner.invoke(
        app, ["run", str(folder), "--root-uri", ROOT, "--create-root"]
    )

    assert outcome.exit_code == 0, outcome.output
    assert json.loads(mkdir.calls[0].request.content)["uri"] == ROOT


@respx.mock
def test_a_missing_target_is_not_created_without_the_flag(
    folder: Path, configured: None
) -> None:
    """A typo'd URI is easy to make; creating it silently leaves a stray tree."""
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            404,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "NOT_FOUND", "message": "gone", "details": None},
            },
        )
    )
    mkdir = respx.post(f"{BASE}/fs/mkdir")

    outcome = runner.invoke(app, ["run", str(folder), "--root-uri", ROOT])

    assert outcome.exit_code == 1
    assert not mkdir.called
    assert "--create-root" in outcome.output


@respx.mock
def test_a_refused_root_is_not_reported_as_a_network_problem(
    folder: Path, configured: None
) -> None:
    """The server refuses some roots outright, and says so clearly.

    Reporting that as "cannot reach OpenViking" sends the user to look at
    their network for an answer the server gave them.
    """
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            404,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "NOT_FOUND", "message": "gone", "details": None},
            },
        )
    )
    respx.post(f"{BASE}/fs/mkdir").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "batch-write root must be inside a resource directory",
                    "details": None,
                },
            },
        )
    )

    outcome = runner.invoke(
        app, ["run", str(folder), "--root-uri", "viking://resources", "--create-root"]
    )

    assert outcome.exit_code == 1
    # Rich wraps console output, so compare on collapsed whitespace.
    printed = " ".join(outcome.output.split())
    assert "Cannot reach" not in printed
    assert "must be inside a resource directory" in printed


@respx.mock
def test_a_dry_run_does_not_create_the_root(folder: Path, configured: None) -> None:
    """--dry-run --create-root reports what it would make; it does not make it."""
    write(folder / "note.md", "hello")
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            404,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "NOT_FOUND", "message": "gone", "details": None},
            },
        )
    )
    mkdir = respx.post(f"{BASE}/fs/mkdir")

    outcome = runner.invoke(
        app, ["run", str(folder), "--root-uri", ROOT, "--create-root", "--dry-run"]
    )

    assert not mkdir.called
    assert outcome.exit_code == 1
