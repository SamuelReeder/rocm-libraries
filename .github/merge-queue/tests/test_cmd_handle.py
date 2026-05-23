"""Tests for rocm_mq.cmd_handle — /merge and /dequeue command handler
(RFC §4.3 / §4.4 / §4.5).

Coverage:
  - parse_commands edge cases (command grammar)
  - is_self_bootstrap glob hits / misses (RFC §8 self-bootstrap rejection)
  - _check_perm: admin/maintain/write pass; read/none fail; PR-author override
  - _check_at_enqueue_gates: pass / fail-on-no-approval / fail-on-required-check /
    fail-on-maintainer-edits / fail-on-empty-queue-set
  - Idempotency short-circuit (mq:queued / mq:active present → eyes-only)
  - Self-bootstrap rejection (rejection comment posted, no labels)
  - Unmapped-paths rejection (PR touches no opted-in queue path)
  - Successful enqueue happy path (single-queue + multi-queue)
  - main() error paths (missing GITHUB_TOKEN, missing event-path file)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rocm_mq import cmd_handle
from rocm_mq.state import AppIdentity, MergeQueueConfig
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


@pytest.fixture()
def fake_client(fake_state: FakeRepoState) -> FakeGitHub:
    return FakeGitHub(fake_state)


@pytest.fixture()
def hipdnn_config() -> MergeQueueConfig:
    """Minimal config: one queue (hipdnn) keyed off projects/hipdnn/."""
    return MergeQueueConfig(
        all_queues=("hipdnn",),
        path_to_queues=(("projects/hipdnn/", frozenset({"hipdnn"})),),
        app_identity=AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999),
    )


@pytest.fixture()
def multi_queue_config() -> MergeQueueConfig:
    """Two-queue config: hipdnn + integration-tests with one shared path."""
    return MergeQueueConfig(
        all_queues=("hipdnn", "integration-tests"),
        path_to_queues=(
            (
                "projects/hipdnn/",
                frozenset({"hipdnn", "integration-tests"}),
            ),
            (
                "dnn-providers/integration-tests/",
                frozenset({"integration-tests"}),
            ),
        ),
        app_identity=AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999),
    )


def _make_event_payload(
    *,
    comment_body: str = "/merge",
    comment_id: int = 4242,
    commenter_login: str = "alice",
    pr_number: int = 7,
    owner: str = "SamuelReeder",
    repo: str = "rocm-libraries",
    action: str = "created",
    is_pr_comment: bool = True,
) -> dict[str, Any]:
    """Build a GitHub issue_comment webhook payload."""
    payload: dict[str, Any] = {
        "action": action,
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "user": {"login": commenter_login},
        },
        "issue": {
            "number": pr_number,
        },
        "repository": {
            "owner": {"login": owner},
            "name": repo,
        },
    }
    if is_pr_comment:
        payload["issue"]["pull_request"] = {}
    return payload


def _write_event(tmp_path: Path, payload: dict[str, Any]) -> Path:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    return event_path


def _seed_pr(
    state: FakeRepoState,
    *,
    number: int = 7,
    head_sha: str = "head_sha_abc",
    files: list[str] | None = None,
    labels: set[str] | None = None,
    user_login: str = "pr-author",
    maintainer_can_modify: bool = True,
    reviews: list[dict[str, str]] | None = None,
    combined_status: str = "success",
    combined_statuses: list[dict[str, str]] | None = None,
    check_runs: list[dict[str, str | None]] | None = None,
    is_cross_repo: bool = False,
) -> FakePR:
    pr = FakePR(
        number=number,
        head_sha=head_sha,
        files=files if files is not None else ["projects/hipdnn/src/foo.cpp"],
        labels=labels if labels is not None else set(),
        user_login=user_login,
        maintainer_can_modify=maintainer_can_modify,
        reviews=reviews if reviews is not None else [{"state": "APPROVED", "user_login": "rev1"}],
        head_repo_id=2 if is_cross_repo else 1,
        base_repo_id=1,
    )
    state.prs[number] = pr
    state.combined_statuses[head_sha] = {
        "state": combined_status,
        "statuses": combined_statuses or [],
    }
    state.check_runs[head_sha] = check_runs or []
    return pr


# ---------------------------------------------------------------------------
# parse_commands edge cases (command grammar)
# ---------------------------------------------------------------------------


class TestParseCommands:
    def test_empty_comment_yields_no_commands(self) -> None:
        assert cmd_handle.parse_commands("just a comment") == set()

    def test_plain_merge_matches(self) -> None:
        assert cmd_handle.parse_commands("/merge") == {"merge"}

    def test_multi_paragraph_with_command_on_own_line(self) -> None:
        body = "paragraph 1\n\n/merge\n\nparagraph 2"
        assert cmd_handle.parse_commands(body) == {"merge"}

    def test_merge_with_trailing_args_does_not_match(self) -> None:
        # /merge --priority high → trailing non-whitespace blocks match
        assert cmd_handle.parse_commands("/merge --priority high") == set()

    def test_inline_code_does_not_match(self) -> None:
        # `/merge` → backtick at column 1 after strip; ^/ regex fails
        assert cmd_handle.parse_commands("`/merge`") == set()

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert cmd_handle.parse_commands("   /merge   ") == {"merge"}

    def test_both_commands_in_same_body(self) -> None:
        body = "/merge\n/dequeue"
        assert cmd_handle.parse_commands(body) == {"merge", "dequeue"}

    def test_dequeue_only(self) -> None:
        assert cmd_handle.parse_commands("/dequeue") == {"dequeue"}

    def test_unknown_slash_command_ignored(self) -> None:
        assert cmd_handle.parse_commands("/approve") == set()


# ---------------------------------------------------------------------------
# is_self_bootstrap (RFC §8 self-bootstrap rejection)
# ---------------------------------------------------------------------------


class TestIsSelfBootstrap:
    def test_workflow_path_is_hit(self) -> None:
        assert cmd_handle.is_self_bootstrap([".github/workflows/foo.yml"]) == [
            ".github/workflows/foo.yml"
        ]

    def test_merge_queue_source_is_hit(self) -> None:
        result = cmd_handle.is_self_bootstrap([".github/merge-queue/src/rocm_mq/gh.py"])
        assert result == [".github/merge-queue/src/rocm_mq/gh.py"]

    def test_regular_source_path_is_miss(self) -> None:
        assert cmd_handle.is_self_bootstrap(["projects/hipdnn/src/foo.cpp"]) == []

    def test_mixed_returns_only_bootstrap_hits(self) -> None:
        paths = [
            ".github/merge-queue/path_to_queues.yml",
            "projects/hipdnn/x.cpp",
        ]
        result = cmd_handle.is_self_bootstrap(paths)
        assert result == [".github/merge-queue/path_to_queues.yml"]

    def test_empty_list(self) -> None:
        assert cmd_handle.is_self_bootstrap([]) == []


# ---------------------------------------------------------------------------
# _check_perm (RFC §4.4 — live perm check + PR-author override)
# ---------------------------------------------------------------------------


class TestCheckPerm:
    def test_admin_passes(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        fake_state.collaborators["alice"] = "admin"
        eligible, role = cmd_handle._check_perm(
            fake_client, "o", "r", "alice", pr_author_login="bob"
        )
        assert eligible is True
        assert role == "admin"

    def test_write_passes(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        fake_state.collaborators["alice"] = "write"
        eligible, role = cmd_handle._check_perm(
            fake_client, "o", "r", "alice", pr_author_login="bob"
        )
        assert eligible is True
        assert role == "write"

    def test_maintain_passes(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        fake_state.collaborators["alice"] = "maintain"
        eligible, role = cmd_handle._check_perm(
            fake_client, "o", "r", "alice", pr_author_login="bob"
        )
        assert eligible is True
        assert role == "maintain"

    def test_read_fails(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        fake_state.collaborators["alice"] = "read"
        eligible, role = cmd_handle._check_perm(
            fake_client, "o", "r", "alice", pr_author_login="bob"
        )
        assert eligible is False
        assert role == "read"

    def test_not_a_collaborator_404_fails_unless_author(
        self, fake_client: FakeGitHub
    ) -> None:
        # No seeded collaborator → 404 → not eligible (unless author).
        eligible, role = cmd_handle._check_perm(
            fake_client, "o", "r", "carol", pr_author_login="bob"
        )
        assert eligible is False
        assert role == "none"

    def test_pr_author_always_eligible_regardless_of_role(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        # commenter == pr_author_login → eligible even with role=read
        fake_state.collaborators["bob"] = "read"
        eligible, _ = cmd_handle._check_perm(
            fake_client, "o", "r", "bob", pr_author_login="bob"
        )
        assert eligible is True

    def test_pr_author_never_collaborator_still_eligible(
        self, fake_client: FakeGitHub
    ) -> None:
        # commenter == pr_author and not in collaborators (404) → still eligible
        eligible, _ = cmd_handle._check_perm(
            fake_client, "o", "r", "bob", pr_author_login="bob"
        )
        assert eligible is True


# ---------------------------------------------------------------------------
# _check_at_enqueue_gates (RFC §4.3)
# ---------------------------------------------------------------------------


class TestAtEnqueueGates:
    def test_all_gates_pass(
        self,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
    ) -> None:
        _seed_pr(fake_state)
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client,
            "o",
            "r",
            7,
            pr,
            queues=frozenset({"hipdnn"}),
        )
        assert fails == []

    def test_fails_on_no_approval_when_gate_enabled(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        """Default ``require_approval=True`` enforces the RFC §5 gate."""
        _seed_pr(fake_state, reviews=[{"state": "COMMENTED"}])
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "no-approval" in fails

    def test_no_approval_skipped_when_gate_disabled(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        """``require_approval=False`` (dogfood override) bypasses the gate.

        PORT-02 closure: the config flag replaces a code-level comment-out so
        the gate cannot silently regress at upstream port time. This test
        pins the dogfood-disabled behavior against the flag, not against a
        commented-out block.
        """
        _seed_pr(fake_state, reviews=[{"state": "COMMENTED"}])
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client,
            "o",
            "r",
            7,
            pr,
            queues=frozenset({"hipdnn"}),
            require_approval=False,
        )
        assert "no-approval" not in fails

    def test_fails_on_failing_required_check(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        _seed_pr(
            fake_state,
            combined_status="failure",
            combined_statuses=[{"context": "ci", "state": "failure"}],
        )
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "failing-required-check" in fails

    @pytest.mark.parametrize(
        "bad_conclusion",
        [
            "failure",
            "timed_out",
            "cancelled",
            "action_required",
            "startup_failure",
            "stale",
        ],
    )
    def test_fails_on_bad_check_run_conclusions(
        self,
        bad_conclusion: str,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
    ) -> None:
        _seed_pr(
            fake_state,
            combined_statuses=[],
            check_runs=[
                {
                    "name": "TheRock CI Summary",
                    "status": "completed",
                    "conclusion": bad_conclusion,
                }
            ],
        )
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "failing-required-check" in fails

    def test_combined_status_and_check_run_failure_emit_one_gate_failure(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        _seed_pr(
            fake_state,
            combined_statuses=[{"context": "ci", "state": "failure"}],
            check_runs=[
                {
                    "name": "TheRock CI Summary",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
        )
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert fails.count("failing-required-check") == 1

    def test_pending_check_run_is_allowed(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        _seed_pr(
            fake_state,
            check_runs=[
                {
                    "name": "TheRock CI Summary",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        )
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "failing-required-check" not in fails

    def test_fails_on_maintainer_edits_disabled(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        # is_cross_repo=True is required to trigger the maintainer-edits
        # gate: the field is only meaningful when head/base live in
        # different repos. Same-repo PRs return False by default on
        # GitHub but the field has no real meaning there.
        _seed_pr(fake_state, maintainer_can_modify=False, is_cross_repo=True)
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "maintainer-edits-disabled" in fails

    def test_cross_repo_pr_missing_maintainer_edits_field_fails_closed(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        _seed_pr(fake_state, is_cross_repo=True)
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        delattr(pr, "maintainer_can_modify")
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "maintainer-edits-disabled" in fails

    def test_same_repo_pr_with_maintainer_edits_false_is_noop(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        """Same-repo PR with maintainer_can_modify=False must NOT trip the
        gate. GitHub returns False by default for same-repo PRs (the field
        is semantically meaningless there); gating on it would block every
        single-repo PR that legitimately wants to merge through the queue."""
        _seed_pr(fake_state, maintainer_can_modify=False, is_cross_repo=False)
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset({"hipdnn"})
        )
        assert "maintainer-edits-disabled" not in fails

    def test_fails_on_empty_queue_set(
        self, fake_client: FakeGitHub, fake_state: FakeRepoState
    ) -> None:
        _seed_pr(fake_state)
        pr = fake_client.rest.pulls.get("o", "r", 7).parsed_data
        fails = cmd_handle._check_at_enqueue_gates(
            fake_client, "o", "r", 7, pr, queues=frozenset()
        )
        assert "empty-queue-set" in fails


# ---------------------------------------------------------------------------
# Full main() integration scenarios
# ---------------------------------------------------------------------------


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any],
    fake_client: FakeGitHub,
    config: MergeQueueConfig,
) -> int:
    """Invoke cmd_handle.main with the env + monkeypatched client/config."""
    event_path = _write_event(tmp_path, payload)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token")

    # Inject the fake client + canned config so we do not hit the network or
    # try to load_from_develop.
    monkeypatch.setattr(
        cmd_handle, "_build_client", lambda _token: fake_client, raising=True
    )
    monkeypatch.setattr(
        cmd_handle,
        "_load_config",
        lambda _client, _owner, _repo: config,
        raising=True,
    )
    return cmd_handle.main(
        ["--repo", "SamuelReeder/rocm-libraries", "--event-path", str(event_path)]
    )


class TestMainIdempotencyShortCircuit:
    def test_mq_queued_label_triggers_eyes_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, labels={"mq:queued"})
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        # eyes-reaction posted on the trigger comment
        assert (4242, "eyes") in fake_state.reactions_log
        # Labels unchanged — still just mq:queued (no duplicate apply)
        assert fake_state.prs[7].labels == {"mq:queued"}

    def test_mq_active_label_triggers_eyes_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, labels={"mq:active"})
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert (4242, "eyes") in fake_state.reactions_log
        assert fake_state.prs[7].labels == {"mq:active"}

    def test_neither_label_runs_full_enqueue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        # mq:queued + mq:hipdnn labels applied
        assert "mq:queued" in fake_state.prs[7].labels
        assert "mq:hipdnn" in fake_state.prs[7].labels


class TestMainSelfBootstrapRejection:
    def test_workflow_path_change_posts_rejection_no_labels(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, files=[".github/workflows/foo.yml"])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        # No labels applied
        assert "mq:queued" not in fake_state.prs[7].labels
        # Rejection comment posted
        comments = fake_state.comments_store.get(7, {})
        assert any(
            "self-bootstrap" in body.lower() or "rfc §8" in body.lower()
            for body in comments.values()
        )

    def test_mixed_paths_cites_bootstrap_file_in_comment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(
            fake_state,
            files=[
                ".github/merge-queue/path_to_queues.yml",
                "projects/hipdnn/x.cpp",
            ],
        )
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        comments = fake_state.comments_store.get(7, {})
        bodies = list(comments.values())
        assert any("path_to_queues.yml" in body for body in bodies)


class TestMainUnmappedPath:
    def test_pr_touches_only_unmapped_path_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, files=["docs/readme.md"])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert "mq:queued" not in fake_state.prs[7].labels


# ---------------------------------------------------------------------------
# Eyes-reaction posted on EVERY received /merge — including rejections.
# Eyes is posted before any state mutation so re-delivery is idempotent.
# ---------------------------------------------------------------------------


class TestEyesOnEveryMerge:
    """Handler posts eyes reaction on every received /merge, regardless of
    accept/reject outcome. RFC §4.3 wording — eyes is an ack the handler
    saw the comment, not a 'we'll proceed' signal."""

    def test_eyes_posted_on_self_bootstrap_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, files=[".github/workflows/foo.yml"])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        _run_main(tmp_path, monkeypatch, payload=payload,
                  fake_client=fake_client, config=hipdnn_config)
        assert (4242, "eyes") in fake_state.reactions_log

    def test_eyes_posted_on_no_opted_in_path_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, files=["docs/readme.md"])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        _run_main(tmp_path, monkeypatch, payload=payload,
                  fake_client=fake_client, config=hipdnn_config)
        assert (4242, "eyes") in fake_state.reactions_log

    def test_eyes_posted_on_perm_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        # Author is "carol"; commenter alice has no role; no override.
        _seed_pr(fake_state, files=["projects/hipdnn/x.cpp"],
                 user_login="carol")
        fake_state.collaborators["alice"] = "none"
        payload = _make_event_payload()
        _run_main(tmp_path, monkeypatch, payload=payload,
                  fake_client=fake_client, config=hipdnn_config)
        assert (4242, "eyes") in fake_state.reactions_log

    def test_eyes_posted_on_gate_failure_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        # PR with no approval -> at-enqueue gate fails.
        _seed_pr(fake_state, files=["projects/hipdnn/x.cpp"], reviews=[])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        _run_main(tmp_path, monkeypatch, payload=payload,
                  fake_client=fake_client, config=hipdnn_config)
        assert (4242, "eyes") in fake_state.reactions_log

    def test_eyes_posted_exactly_once_on_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        """Don't double-post eyes on successful enqueue (top-of-handler
        post must NOT compound with the existing success-path post)."""
        _seed_pr(fake_state, files=["projects/hipdnn/x.cpp"])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        _run_main(tmp_path, monkeypatch, payload=payload,
                  fake_client=fake_client, config=hipdnn_config)
        eyes = [r for r in fake_state.reactions_log if r == (4242, "eyes")]
        assert len(eyes) == 1, (
            f"Expected exactly 1 eyes reaction; got {len(eyes)}: "
            f"{fake_state.reactions_log}"
        )
        comments = fake_state.comments_store.get(7, {})
        bodies = list(comments.values())
        # rejection cites no opted-in queue
        assert any(
            "opted-in" in b.lower() or "no queue" in b.lower() or "queue" in b.lower()
            for b in bodies
        )


class TestMainSuccessfulEnqueue:
    def test_single_queue_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert fake_state.prs[7].labels == {"mq:queued", "mq:hipdnn"}
        # Status comment created with the load-bearing marker
        comments = fake_state.comments_store.get(7, {})
        assert any("<!-- rocm-mq-status -->" in b for b in comments.values())
        # eyes-reaction posted
        assert (4242, "eyes") in fake_state.reactions_log

    def test_multi_queue_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        multi_queue_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=multi_queue_config,
        )
        assert rc == 0
        assert "mq:queued" in fake_state.prs[7].labels
        assert "mq:hipdnn" in fake_state.prs[7].labels
        assert "mq:integration-tests" in fake_state.prs[7].labels


class TestMainPermRejection:
    def test_read_only_commenter_who_is_not_author_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, user_login="bob")
        fake_state.collaborators["alice"] = "read"
        payload = _make_event_payload(commenter_login="alice")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        # No labels applied
        assert "mq:queued" not in fake_state.prs[7].labels
        # Rejection comment posted citing the live role
        comments = fake_state.comments_store.get(7, {})
        bodies = list(comments.values())
        assert any("read" in b for b in bodies)


class TestMainGateFailures:
    def test_no_approval_emits_single_rejection_comment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, reviews=[{"state": "COMMENTED"}])
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload()
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert "mq:queued" not in fake_state.prs[7].labels
        comments = fake_state.comments_store.get(7, {})
        bodies = list(comments.values())
        assert any("no-approval" in b or "approval" in b.lower() for b in bodies)


class TestMainDequeue:
    def test_dequeue_removes_labels_and_upserts_comment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, labels={"mq:queued", "mq:hipdnn"})
        fake_state.collaborators["alice"] = "write"
        payload = _make_event_payload(comment_body="/dequeue")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert "mq:queued" not in fake_state.prs[7].labels
        assert "mq:hipdnn" not in fake_state.prs[7].labels
        # eyes-reaction still posted
        assert (4242, "eyes") in fake_state.reactions_log

    def test_dequeue_allows_pr_author_without_repo_role(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, labels={"mq:queued", "mq:hipdnn"}, user_login="alice")
        payload = _make_event_payload(comment_body="/dequeue", commenter_login="alice")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert fake_state.prs[7].labels == set()
        assert (4242, "eyes") in fake_state.reactions_log

    def test_dequeue_rejects_non_author_without_write_role(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state, labels={"mq:queued", "mq:hipdnn"}, user_login="bob")
        fake_state.collaborators["alice"] = "read"
        fake_state.comments_store[7] = {
            123: "existing queued status\n<!-- rocm-mq-status -->",
        }
        payload = _make_event_payload(comment_body="/dequeue", commenter_login="alice")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert fake_state.prs[7].labels == {"mq:queued", "mq:hipdnn"}
        assert (4242, "eyes") not in fake_state.reactions_log
        comments = fake_state.comments_store.get(7, {})
        assert comments[123] == "existing queued status\n<!-- rocm-mq-status -->"
        bodies = list(comments.values())
        assert any("`/dequeue` rejected" in b and "read" in b for b in bodies)
        assert not any("Ejected from merge queue" in b for b in bodies)


# ---------------------------------------------------------------------------
# main() non-PR / non-created event skip path
# ---------------------------------------------------------------------------


class TestMainEventSkip:
    def test_non_created_action_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        payload = _make_event_payload(action="edited")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        # No labels touched
        assert fake_state.prs[7].labels == set()

    def test_non_pr_issue_comment_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        payload = _make_event_payload(is_pr_comment=False)
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert fake_state.prs[7].labels == set()

    def test_no_recognized_command_in_body_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_client: FakeGitHub,
        fake_state: FakeRepoState,
        hipdnn_config: MergeQueueConfig,
    ) -> None:
        _seed_pr(fake_state)
        payload = _make_event_payload(comment_body="thanks for the review")
        rc = _run_main(
            tmp_path,
            monkeypatch,
            payload=payload,
            fake_client=fake_client,
            config=hipdnn_config,
        )
        assert rc == 0
        assert fake_state.prs[7].labels == set()


# ---------------------------------------------------------------------------
# main() error / usage paths
# ---------------------------------------------------------------------------


class TestMainErrors:
    def test_missing_github_token_returns_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        event_path = _write_event(tmp_path, _make_event_payload())
        rc = cmd_handle.main(
            ["--repo", "x/y", "--event-path", str(event_path)]
        )
        assert rc == 2

    def test_missing_event_path_returns_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
        rc = cmd_handle.main(["--repo", "x/y", "--event-path", "/nonexistent/event.json"])
        assert rc == 1
        err = capsys.readouterr().err
        # full traceback printed via the shared try/except wrapper
        assert "error" in err.lower() or "traceback" in err.lower()

    def test_malformed_repo_returns_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
        event_path = _write_event(tmp_path, _make_event_payload())
        rc = cmd_handle.main(["--repo", "no-slash", "--event-path", str(event_path)])
        assert rc == 2


# ---------------------------------------------------------------------------
# Module-level import smoke
# ---------------------------------------------------------------------------


def test_cmd_handle_module_importable() -> None:
    """Public surface exists: main, parse_commands, is_self_bootstrap."""
    assert callable(cmd_handle.main)
    assert callable(cmd_handle.parse_commands)
    assert callable(cmd_handle.is_self_bootstrap)
