"""tests/test_preflight.py — Unit tests for rocm_mq.preflight.

Covers the comprehensive 2-check preflight (WF-10 + path_to_queues loadable)
implemented per plan 03-04. The third RESEARCH.md Area #23 check (App identity
slug-match) is DEFERRED to processor startup because apps.get_authenticated
requires an App-token but preflight runs BEFORE the App-token mint and only
holds the workflow's GITHUB_TOKEN.

Test surface:

- main() returns 0 when default_branch == "develop" AND path_to_queues.yml
  is loadable from the develop ref via the Contents API.
- main() returns 1 with a structured stderr line on each individual failure
  mode (wrong default branch; path_to_queues.yml not loadable).
- main() returns 2 on usage errors (--repo missing or malformed; GITHUB_TOKEN
  env var unset) — mirrors the cmd_process.py exit-code-2 convention so the
  workflow's run-log structure stays uniform across CLI entrypoints.
- $GITHUB_STEP_SUMMARY append uses "a" (append) mode and never truncates a
  pre-existing summary written by an earlier workflow step — verified by a
  sentinel-survival pattern.
"""

from __future__ import annotations

import base64
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gh_fake import FakeGitHub, FakeRepoState

# ---------------------------------------------------------------------------
# Shared FakeGitHub patching helpers
# ---------------------------------------------------------------------------


def _patch_repos_get_default_branch(
    fake: FakeGitHub, *, default_branch: str
) -> None:
    """Add a ``repos.get(owner, repo)`` method to the fake returning default_branch.

    The base FakeGitHub does not model the repos.get metadata call; this
    helper is the per-test extension pattern used elsewhere in the suite
    (e.g., tests/test_config.py::_seed_contents).
    """

    def get(owner: str, repo: str, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(default_branch=default_branch)
        )

    fake.rest.repos.get = get  # type: ignore[attr-defined]


def _patch_repos_get_content_ok(
    fake: FakeGitHub, *, path: str = ".github/merge-queue/path_to_queues.yml"
) -> list[dict[str, object]]:
    """Add a working ``repos.get_content`` returning a valid yaml payload.

    Returns a call-log list so tests can inspect the call args (e.g., to
    assert the loader passed ref='develop').
    """
    call_log: list[dict[str, object]] = []
    payload = "queues:\n  - hipdnn\n"
    encoded = base64.b64encode(payload.encode()).decode()

    def get_content(
        owner: str,
        repo: str,
        content_path: str,
        *,
        ref: str = "",
        **_: object,
    ) -> SimpleNamespace:
        call_log.append(
            {"owner": owner, "repo": repo, "path": content_path, "ref": ref}
        )
        if content_path != path:
            raise AssertionError(
                f"unexpected path {content_path!r}; seeded {path!r}"
            )
        return SimpleNamespace(parsed_data=SimpleNamespace(content=encoded))

    fake.rest.repos.get_content = get_content  # type: ignore[attr-defined]
    return call_log


def _patch_repos_get_content_raises(
    fake: FakeGitHub, *, exc: Exception
) -> None:
    """Add a ``repos.get_content`` that always raises ``exc`` to simulate API errors."""

    def get_content(*args: object, **kwargs: object) -> SimpleNamespace:
        raise exc

    fake.rest.repos.get_content = get_content  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Module importability + parser surface
# ---------------------------------------------------------------------------


def test_preflight_module_importable() -> None:
    """Smoke: the module exists and exposes ``main``."""
    from rocm_mq import preflight

    assert callable(preflight.main)


# ---------------------------------------------------------------------------
# Happy path — both checks pass → exit 0
# ---------------------------------------------------------------------------


def test_main_happy_path_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """default_branch=develop AND path_to_queues.yml loadable → exit 0."""
    from rocm_mq import preflight

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="develop")
    call_log = _patch_repos_get_content_ok(fake)

    # Inject the fake by patching GitHubClient at the preflight module's
    # import site so main() uses it instead of constructing a real client.
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 0

    err = capsys.readouterr().err
    assert "preflight passed" in err

    # Check 3 hits the develop ref at the canonical path.
    assert len(call_log) == 1
    assert call_log[0]["ref"] == "develop"
    assert call_log[0]["path"] == ".github/merge-queue/path_to_queues.yml"


# ---------------------------------------------------------------------------
# Failure modes — Check 1 (wrong default branch)
# ---------------------------------------------------------------------------


def test_main_wrong_default_branch_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """default_branch != 'develop' → exit 1 with structured stderr."""
    from rocm_mq import preflight

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="main")
    # Do NOT patch get_content — we MUST short-circuit before Check 3.
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 1

    err = capsys.readouterr().err
    assert "preflight FAILED" in err
    assert "default_branch=" in err
    assert "'main'" in err
    assert "expected 'develop'" in err


# ---------------------------------------------------------------------------
# Failure modes — Check 3 (path_to_queues.yml not loadable)
# ---------------------------------------------------------------------------


def test_main_path_to_queues_unloadable_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Contents API error on path_to_queues.yml → exit 1 with structured stderr."""
    from rocm_mq import preflight

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="develop")
    _patch_repos_get_content_raises(fake, exc=RuntimeError("404 Not Found"))
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 1

    err = capsys.readouterr().err
    assert "preflight FAILED" in err
    assert "PATH_TO_QUEUES not loadable" in err
    # The exception repr should be embedded for debug visibility.
    assert "404 Not Found" in err


# ---------------------------------------------------------------------------
# Usage errors — exit 2 (--repo missing / malformed / no GITHUB_TOKEN)
# ---------------------------------------------------------------------------


def test_main_no_repo_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --repo and no $GITHUB_REPOSITORY → exit 2 with canonical stderr."""
    from rocm_mq import preflight

    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    rc = preflight.main([])
    assert rc == 2

    err = capsys.readouterr().err
    assert "--repo must be in OWNER/REPO form" in err


def test_main_malformed_repo_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--repo without a slash → exit 2 with canonical stderr."""
    from rocm_mq import preflight

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    rc = preflight.main(["--repo", "noslash"])
    assert rc == 2

    err = capsys.readouterr().err
    assert "--repo must be in OWNER/REPO form" in err


def test_main_missing_github_token_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No GITHUB_TOKEN env var → exit 2; mirrors cmd_process.py pattern."""
    from rocm_mq import preflight

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 2

    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err


# ---------------------------------------------------------------------------
# $GITHUB_STEP_SUMMARY append — sentinel survival (no truncate)
# ---------------------------------------------------------------------------


def test_failure_appends_to_step_summary_in_append_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A failure path appends ``## Pre-flight FAILED`` and preserves prior content.

    GHA convention: $GITHUB_STEP_SUMMARY is shared across steps; preflight
    MUST open it in "a" (append) mode so an earlier step's content survives.
    """
    from rocm_mq import preflight

    summary_file = tmp_path / "step_summary.md"
    sentinel = "## Sentinel from earlier step\n\nThis text MUST survive.\n"
    summary_file.write_text(sentinel)

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="main")
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 1

    content = summary_file.read_text()
    # Sentinel from prior step preserved.
    assert sentinel in content, (
        f"preflight must not truncate $GITHUB_STEP_SUMMARY; got:\n{content!r}"
    )
    # Append section visible.
    assert "## Pre-flight FAILED" in content
    assert "default_branch=" in content


def test_step_summary_not_required_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When $GITHUB_STEP_SUMMARY is unset (local dev), preflight still runs cleanly."""
    from rocm_mq import preflight

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="main")
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main(["--repo", "owner/repo"])
    assert rc == 1
    # No exception raised even though the summary file path is unset.


# ---------------------------------------------------------------------------
# --repo defaults to $GITHUB_REPOSITORY (cmd_process.py parity)
# ---------------------------------------------------------------------------


def test_main_repo_defaults_to_github_repository_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--repo absent → falls back to $GITHUB_REPOSITORY (GHA convention)."""
    from rocm_mq import preflight

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    fake = FakeGitHub(FakeRepoState())
    _patch_repos_get_default_branch(fake, default_branch="develop")
    _patch_repos_get_content_ok(fake)
    monkeypatch.setattr(preflight, "GitHubClient", lambda token: fake)

    rc = preflight.main([])
    assert rc == 0


# Keep `sys` reference so static linters don't drop the import.
_ = sys
