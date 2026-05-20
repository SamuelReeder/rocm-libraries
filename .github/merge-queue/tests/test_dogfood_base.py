"""Tests for rocm_mq.dogfood._base — Phase 3 plan 03-10 driver scaffolding.

Coverage map (see 03-10-PLAN.md Task 1 + Task 2 behavior blocks):
  - Imports: DogfoodResult, create_dogfood_pr, post_command, poll_pr_state,
    emit_result, download_cycle_summary_artifact
  - DogfoodResult is frozen + uses __slots__
  - DogfoodResult.timeline is tuple-typed (Pitfall 11)
  - emit_result writes JSON with the D-04 schema (exact key set), creates parent dir
  - create_dogfood_pr branch-name pattern (regex) + end-to-end against FakeGitHub
  - post_command body == cmd, returns comment id
  - poll_pr_state: timeout raises TimeoutError; success returns predicate value
  - download_cycle_summary_artifact: graceful empty-string on missing artifact

The dogfood subpackage is I/O layer (CONTEXT.md D-02); these tests use FakeGitHub
extensions for the new endpoints (git.get_ref, git.create_ref,
repos.create_or_update_file_contents, pulls.create, actions.list_workflow_run_artifacts,
actions.download_artifact). No real GitHub calls — drivers (DOG-02..DOG-08) own that.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood._base import (
    DogfoodResult,
    create_dogfood_pr,
    download_cycle_summary_artifact,
    emit_result,
    poll_pr_state,
    post_command,
)
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# D-04 schema — pinned key set; any drift fails test_emitted_json_schema below
# ---------------------------------------------------------------------------

_D04_KEYS: frozenset[str] = frozenset(
    {
        "scenario_id",
        "run_started_at",
        "run_ended_at",
        "pr_number",
        "pr_url",
        "expected_outcome",
        "observed_outcome",
        "timeline",
        "processor_run_urls",
        "step_summary_excerpt",
        "passed",
        "notes",
    }
)


def _make_result(**overrides: Any) -> DogfoodResult:
    """Build a minimal DogfoodResult; overrides win."""
    base: dict[str, Any] = {
        "scenario_id": "dog_02",
        "run_started_at": "2026-05-19T12:00:00+00:00",
        "run_ended_at": "2026-05-19T12:01:00+00:00",
        "pr_number": 42,
        "pr_url": "https://github.test/owner/repo/pull/42",
        "expected_outcome": {"action": "Eject", "reason": "merge conflict with develop"},
        "observed_outcome": {"action": "Eject", "reason": "merge conflict with develop"},
        "timeline": (
            ("2026-05-19T12:00:01+00:00", "pr_opened", {"head_sha": "abc"}),
            ("2026-05-19T12:00:30+00:00", "ejected", {"reason": "merge conflict with develop"}),
        ),
        "processor_run_urls": ("https://github.test/owner/repo/actions/runs/1",),
        "step_summary_excerpt": "cycle started",
        "passed": True,
        "notes": "",
    }
    base.update(overrides)
    return DogfoodResult(**base)


# ---------------------------------------------------------------------------
# DogfoodResult shape (frozen + slots + tuple-typed timeline)
# ---------------------------------------------------------------------------


def test_dogfood_result_frozen() -> None:
    r = _make_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.pr_number = 99  # type: ignore[misc]


def test_dogfood_result_uses_slots() -> None:
    r = _make_result()
    assert not hasattr(r, "__dict__"), "DogfoodResult must use __slots__"


def test_dogfood_result_timeline_accepts_tuple() -> None:
    r = _make_result(timeline=(("ts", "pr_opened", {"k": "v"}),))
    assert isinstance(r.timeline, tuple)


def test_dogfood_result_processor_run_urls_accepts_tuple() -> None:
    r = _make_result(processor_run_urls=("a", "b"))
    assert r.processor_run_urls == ("a", "b")


def test_dogfood_result_is_dataclass_with_d04_fields() -> None:
    assert dataclasses.is_dataclass(DogfoodResult)
    field_names = {f.name for f in dataclasses.fields(DogfoodResult)}
    assert field_names == _D04_KEYS


# ---------------------------------------------------------------------------
# emit_result — D-04 schema + filesystem behavior
# ---------------------------------------------------------------------------


def test_emit_result_writes_json_to_default_dir(tmp_path: Path) -> None:
    r = _make_result()
    out = emit_result(r, output_dir=tmp_path)
    assert out.exists()
    assert out.parent == tmp_path
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scenario_id"] == "dog_02"
    assert payload["pr_number"] == 42


def test_emitted_json_schema_matches_d04_exactly(tmp_path: Path) -> None:
    r = _make_result()
    out = emit_result(r, output_dir=tmp_path)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload.keys()) == _D04_KEYS


def test_emit_result_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    r = _make_result()
    out = emit_result(r, output_dir=nested)
    assert out.exists()
    assert nested.exists()


def test_emit_result_filename_is_filesystem_safe(tmp_path: Path) -> None:
    """ISO timestamps contain ':' which is illegal on Windows + awkward on Unix.

    emit_result must sanitize the filename so a colon in the ISO ts does NOT
    appear in the file's basename.
    """
    r = _make_result(run_started_at="2026-05-19T12:00:00+00:00")
    out = emit_result(r, output_dir=tmp_path)
    assert ":" not in out.name, f"filename contains colon: {out.name}"


# ---------------------------------------------------------------------------
# create_dogfood_pr — branch naming + end-to-end against FakeGitHub
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(r"^dogfood/[a-z0-9_]+-[0-9a-f]{8}$")


class _DogfoodFake(FakeGitHub):
    """FakeGitHub extended with the git/repos/pulls/actions endpoints
    create_dogfood_pr + download_cycle_summary_artifact need.

    Kept LOCAL to this test file (not promoted to gh_fake.py) because the
    dogfood subpackage is the only consumer; promotion can happen in a future
    plan if another module needs the same surface.
    """

    def __init__(
        self,
        state: FakeRepoState,
        *,
        artifacts: list[dict[str, Any]] | None = None,
        archive_bytes: bytes | None = None,
    ) -> None:
        super().__init__(state)
        # Per-PR id seed.
        self._next_pr_number = 1000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._artifacts = artifacts or []
        self._archive_bytes = archive_bytes
        # Mount the new sub-namespaces on rest.
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        self.rest.actions = _ActionsNS(self._artifacts, self._archive_bytes)

    def _create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        message: str,
        content: str,
        branch: str,
        **_: Any,
    ) -> SimpleNamespace:
        record = {
            "owner": owner,
            "repo": repo,
            "path": path,
            "message": message,
            "content": content,
            "branch": branch,
        }
        self._created_files.append(record)
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                commit=SimpleNamespace(sha=f"file_commit_{len(self._created_files)}")
            )
        )

    def _create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        number = self._next_pr_number
        self._next_pr_number += 1
        record = {
            "owner": owner,
            "repo": repo,
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "number": number,
        }
        self._created_pulls.append(record)
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )


class _GitNS:
    def __init__(self, refs: dict[str, str]) -> None:
        self._refs = refs

    def get_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        sha = self._refs.get(ref)
        if sha is None:
            from tests.gh_fake import _make_request_failed

            raise _make_request_failed(404)
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                ref=f"refs/{ref}",
                object=SimpleNamespace(sha=sha, type="commit"),
            )
        )

    def create_ref(self, owner: str, repo: str, *, ref: str, sha: str, **_: Any) -> SimpleNamespace:
        # ref comes in as "refs/heads/<branch>"; store the short form.
        short = ref[len("refs/") :] if ref.startswith("refs/") else ref
        self._refs[short] = sha
        return SimpleNamespace(
            parsed_data=SimpleNamespace(ref=ref, object=SimpleNamespace(sha=sha))
        )


class _ActionsNS:
    def __init__(
        self,
        artifacts: list[dict[str, Any]],
        archive_bytes: bytes | None,
    ) -> None:
        self._artifacts = artifacts
        self._archive_bytes = archive_bytes

    def list_workflow_run_artifacts(
        self, owner: str, repo: str, run_id: int, **_: Any
    ) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                total_count=len(self._artifacts),
                artifacts=[SimpleNamespace(id=a["id"], name=a["name"]) for a in self._artifacts],
            )
        )

    def download_artifact(
        self,
        owner: str,
        repo: str,
        artifact_id: int,
        archive_format: str = "zip",
        **_: Any,
    ) -> SimpleNamespace:
        # Real githubkit returns a Response whose .content is the raw zip bytes.
        # Match the production-side attribute access path of _base.py.
        if self._archive_bytes is None:
            from tests.gh_fake import _make_request_failed

            raise _make_request_failed(404)
        return SimpleNamespace(content=self._archive_bytes)


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


@pytest.fixture()
def fake_client(fake_state: FakeRepoState) -> _DogfoodFake:
    return _DogfoodFake(fake_state)


def test_create_dogfood_pr_branch_naming(fake_client: _DogfoodFake) -> None:
    pr_number, pr_url = create_dogfood_pr(
        fake_client,
        "owner",
        "repo",
        "dog_02",
        "dogfood/test.txt",
        "hello\n",
    )
    assert pr_number == 1000
    assert pr_url.startswith("https://github.test/owner/repo/pull/")
    # Verify a branch named dogfood/<scenario>-<8-hex> was created.
    branches = [b for b in fake_client._refs if b.startswith("heads/dogfood/")]
    assert len(branches) == 1
    branch_short = branches[0][len("heads/") :]
    assert _BRANCH_RE.match(branch_short), f"branch {branch_short!r} fails regex"


def test_create_dogfood_pr_end_to_end(fake_client: _DogfoodFake) -> None:
    pr_number, _pr_url = create_dogfood_pr(
        fake_client,
        "owner",
        "repo",
        "dog_02",
        "dogfood/conflict.txt",
        "content-A\n",
        title_suffix="merge conflict scenario",
    )
    # File was committed on the branch.
    assert len(fake_client._created_files) == 1
    assert fake_client._created_files[0]["path"] == "dogfood/conflict.txt"
    # PR opened against develop with the synthesized title.
    assert len(fake_client._created_pulls) == 1
    pull = fake_client._created_pulls[0]
    assert pull["base"] == "develop"
    assert pull["head"].startswith("dogfood/dog_02-")
    assert "[dogfood dog_02]" in pull["title"]
    assert "merge conflict scenario" in pull["title"]
    assert pr_number == pull["number"]


def test_create_dogfood_pr_title_prefix_override(fake_client: _DogfoodFake) -> None:
    create_dogfood_pr(
        fake_client,
        "owner",
        "repo",
        "dog_02",
        "dogfood/x.txt",
        "x\n",
        title_prefix="[custom]",
        title_suffix="suffix-text",
    )
    assert "[custom]" in fake_client._created_pulls[0]["title"]
    assert "suffix-text" in fake_client._created_pulls[0]["title"]


# ---------------------------------------------------------------------------
# post_command — body is the literal command; comment id returned
# ---------------------------------------------------------------------------


def test_post_command_creates_comment_with_body(fake_client: _DogfoodFake) -> None:
    fake_client.state.prs[42] = FakePR(number=42, head_sha="abcd")
    comment_id = post_command(fake_client, "owner", "repo", 42, cmd="/merge")
    assert isinstance(comment_id, int)
    stored = fake_client.state.comments_store.get(42, {})
    assert comment_id in stored
    assert stored[comment_id] == "/merge"


def test_post_command_default_cmd_is_merge(fake_client: _DogfoodFake) -> None:
    fake_client.state.prs[43] = FakePR(number=43, head_sha="defg")
    cid = post_command(fake_client, "owner", "repo", 43)
    assert fake_client.state.comments_store[43][cid] == "/merge"


# ---------------------------------------------------------------------------
# poll_pr_state — timeout / success / interval
# ---------------------------------------------------------------------------


def test_poll_pr_state_success(fake_client: _DogfoodFake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Predicate truthy on first call → its return value is passed through."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)

    def predicate(client: Any, owner: str, repo: str, pr_number: int) -> dict | None:
        return {"action": "Eject", "reason": "merge conflict with develop"}

    observed = poll_pr_state(
        fake_client,
        "owner",
        "repo",
        42,
        predicate=predicate,
        timeout_s=10,
        interval_s=1,
    )
    assert observed == {"action": "Eject", "reason": "merge conflict with develop"}


def test_poll_pr_state_eventual_success(
    fake_client: _DogfoodFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Predicate falsy then truthy → returns the eventual truthy value."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def predicate(*_args: Any, **_kw: Any) -> dict | None:
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        return {"state": "merged"}

    observed = poll_pr_state(
        fake_client,
        "owner",
        "repo",
        42,
        predicate=predicate,
        timeout_s=100,
        interval_s=1,
    )
    assert observed == {"state": "merged"}
    assert calls["n"] == 3


def test_poll_pr_state_timeout(fake_client: _DogfoodFake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Predicate always falsy → TimeoutError raised after timeout_s elapses.

    Fake the monotonic clock so the test runs in microseconds rather than
    real seconds. Real time.sleep is also stubbed.
    """
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)
    fake_now = {"t": 0.0}

    def fake_monotonic() -> float:
        # Advance 10s per call so timeout_s=30 trips after ~3 iterations.
        fake_now["t"] += 10.0
        return fake_now["t"]

    monkeypatch.setattr("rocm_mq.dogfood._base.time.monotonic", fake_monotonic)

    def predicate(*_args: Any, **_kw: Any) -> None:
        return None

    with pytest.raises(TimeoutError):
        poll_pr_state(
            fake_client,
            "owner",
            "repo",
            42,
            predicate=predicate,
            timeout_s=30,
            interval_s=5,
        )


# ---------------------------------------------------------------------------
# download_cycle_summary_artifact — graceful empty-string on missing artifact
# ---------------------------------------------------------------------------


def test_download_cycle_summary_graceful_on_missing(fake_state: FakeRepoState) -> None:
    """No artifacts at all → returns empty string (not raise)."""
    client = _DogfoodFake(fake_state, artifacts=[])
    result = download_cycle_summary_artifact(client, "owner", "repo", run_id=123)
    assert result == ""


def test_download_cycle_summary_graceful_on_no_matching_artifact(
    fake_state: FakeRepoState,
) -> None:
    """Artifacts exist but none start with 'cycle-summary-' → empty string."""
    client = _DogfoodFake(fake_state, artifacts=[{"id": 1, "name": "other-artifact-7"}])
    result = download_cycle_summary_artifact(client, "owner", "repo", run_id=123)
    assert result == ""


def test_download_cycle_summary_returns_content_on_success(
    fake_state: FakeRepoState,
) -> None:
    """Happy path: cycle-summary-* artifact exists; zip contains a single file
    whose UTF-8 content is returned verbatim.
    """
    payload = "## Cycle summary\n\nactions: 0\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cycle-summary.md", payload)
    client = _DogfoodFake(
        fake_state,
        artifacts=[{"id": 7, "name": "cycle-summary-9999"}],
        archive_bytes=buf.getvalue(),
    )
    result = download_cycle_summary_artifact(client, "owner", "repo", run_id=9999)
    assert result == payload


def test_download_cycle_summary_graceful_on_api_failure(
    fake_state: FakeRepoState,
) -> None:
    """Artifact lookup succeeds, but the archive fetch fails → empty string."""
    client = _DogfoodFake(
        fake_state,
        artifacts=[{"id": 7, "name": "cycle-summary-9999"}],
        archive_bytes=None,  # download_artifact will raise 404
    )
    result = download_cycle_summary_artifact(client, "owner", "repo", run_id=9999)
    assert result == ""
