"""The startup gate holds the installed packages to the tolerance, and only shows the project's
own code. `docs/deployment.md`, "A cold start"."""

import json
from pathlib import Path

import pytest

from devtools.startup import check

BASELINE = {"app": 50, "fastapi": 100, "pydantic": 100}


def recorded(tmp_path: Path) -> Path:
    baseline = tmp_path / "startup.json"
    baseline.write_text(json.dumps({"files": sum(BASELINE.values()), "packages": BASELINE}))
    return baseline


def test_the_projects_own_code_grows_without_a_limit(tmp_path: Path) -> None:
    assert check({**BASELINE, "app": 400}, recorded(tmp_path), tighten=False) == 0


def test_the_projects_own_code_shrinks_without_a_limit(tmp_path: Path) -> None:
    assert check({**BASELINE, "app": 1}, recorded(tmp_path), tighten=False) == 0


def test_an_installed_package_that_grows_past_the_tolerance_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = {**BASELINE, "app": 400, "sqlalchemy": 20}

    assert check(now, recorded(tmp_path), tighten=False) == 1
    assert "  grew: sqlalchemy +20\n" in capsys.readouterr().err


def test_a_rise_inside_the_tolerance_passes(tmp_path: Path) -> None:
    assert check({**BASELINE, "fastapi": 109}, recorded(tmp_path), tighten=False) == 0
