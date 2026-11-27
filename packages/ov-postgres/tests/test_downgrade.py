"""Downgrading: the previous release still works on a table this one reconciled.

Runs the real previous stack -- ov-postgres 0.6.0, built from its tag, on
OpenViking 0.4.17.1 from PyPI -- in a virtual environment of its own, against a
table this version has reconciled: new columns, the extra-guard trigger, new
indexes and registry entries. Then this version reads back what the old one
wrote.

The environment is built once with ``uv`` and cached under the user cache
directory, so the first run needs the network and later runs do not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from openviking.storage.acl import AclAction, acl_grant_tokens

from .test_integration import DIM, make_adapter, vec
from .test_schema_reconcile import (
    NEW,
    OLD,
    create,
    resource,
    visible_to_bob,
)

pytestmark = pytest.mark.integration

OLD_OPENVIKING = "0.4.17.1"
OLD_TAG = "ov-postgres-v0.6.0"
REPO = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).with_name("downgrade_probe.py")


@pytest.fixture(scope="session")
def old_python() -> Path:
    """Return the interpreter of a venv holding the previous release."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    root = cache / "ov-postgres-tests" / f"{OLD_TAG}-openviking-{OLD_OPENVIKING}"
    python = root / "bin" / "python"
    ready = root / ".ready"
    if not ready.exists():
        uv = shutil.which("uv")
        assert uv is not None, "building the old environment needs uv on PATH"
        shutil.rmtree(root, ignore_errors=True)
        subprocess.run(
            [
                uv,
                "venv",
                "--quiet",
                "--python",
                f"{sys.version_info[0]}.{sys.version_info[1]}",
                str(root),
            ],
            check=True,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--quiet",
                "--python",
                str(python),
                f"openviking=={OLD_OPENVIKING}",
                f"ov-postgres @ git+file://{REPO}@{OLD_TAG}#subdirectory=packages/ov-postgres",
            ],
            check=True,
        )
        ready.write_text("")
    return python


def run_old(python: Path, dsn: str, schema: str) -> dict[str, Any]:
    """Run the probe under the old stack and return its report."""
    result = subprocess.run(
        [
            str(python),
            "-I",
            str(PROBE),
            json.dumps({"dsn": dsn, "schema": schema, "dim": DIM}),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return report


def test_the_previous_release_runs_on_a_reconciled_table(
    dsn: str, test_schema: str, old_python: Path
) -> None:
    """Reconcile here, run 0.6.0 there, then read its writes back here."""
    old_era = make_adapter(dsn, test_schema)
    create(old_era, OLD)
    old_era.upsert([resource("legacy"), resource("to-delete")])
    old_era.close()

    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, NEW)
        grants = acl_grant_tokens(["user:alice"], AclAction.READ)
        adapter.upsert(
            [
                {**resource("new-open", md5="n1"), "vector": vec(1)},
                resource(
                    "new-secret",
                    md5="s1",
                    acl_mode="restricted",
                    acl_direct_grants=grants[:1],
                ),
            ]
        )
    finally:
        adapter.close()

    report = run_old(old_python, dsn, test_schema)

    assert report["created"] is False
    seen = report["seen"]
    assert set(seen) == {"legacy", "to-delete", "new-open", "new-secret"}
    assert seen["new-secret"]["acl_mode"] == "restricted"
    assert seen["new-open"]["md5"] == "n1"
    assert seen["legacy"]["acl_mode"] == "none"
    assert report["level_2"] == sorted(seen)
    assert report["nearest"] == "new-open"
    assert report["count"] == 5
    assert report["deleted"] == 1

    after = make_adapter(dsn, test_schema)
    try:
        assert create(after, NEW) is False
        rows = {
            row["id"]: row for row in after.get(["old-write", "new-open", "new-secret"])
        }
        assert rows["old-write"]["acl_mode"] == "none"
        assert rows["old-write"]["md5"] == ""
        assert rows["new-open"]["active_count"] == 5
        assert rows["new-open"]["md5"] == "n1"
        assert rows["new-secret"]["acl_mode"] == "restricted", (
            "the old stack's rewrite must not erase the restriction"
        )
        assert rows["new-secret"]["md5"] == "s1"
        assert rows["new-secret"]["abstract"] == "rewritten by the old stack"
        assert visible_to_bob(after) == {"legacy", "new-open", "old-write"}
    finally:
        after.close()
