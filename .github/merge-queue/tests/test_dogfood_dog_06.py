"""Tests for rocm_mq.dogfood.dog_06 — marquee 5-PR RFC §4.2 worked example driver.

Scenario contract (RFC §4.2 / RFC §6 DOG-06, plan 03-14): the driver orchestrates
the canonical 5-PR worked example end-to-end:

    A enqueues + merges → B/C enqueue + activate + merge in parallel
    → D enqueues + merges → E enqueues + merges

Per-PR path choices match the RFC §4.2 narrative queue-membership story:
  * PR_A: ``dnn-providers/integration-tests/...`` — touches integration-tests +
    every provider queue (hipdnn queue is not engaged because the path is not
    under projects/hipdnn/; per path_to_queues.yml the integration-tests path
    routes to {miopen, hipblaslt, hip-kernel, fusilli, integration-tests}).
  * PR_B: ``dnn-providers/miopen-provider/...`` — single provider queue.
  * PR_C: ``dnn-providers/hipblaslt-provider/...`` — disjoint provider queue
    from PR_B; B and C share no queue → can parallelize.
  * PR_D: ``dnn-providers/integration-tests/...`` — engages all four provider
    queues + integration-tests.
  * PR_E: ``dnn-providers/miopen-provider/...`` — same provider as PR_B (the
    "E touches provider in the same queue as something earlier" beat).

Single JSON output reads top-to-bottom like the RFC §4.2 narrative; every
``squash_merge_completed`` event carries ``tree_diff_status='ahead'`` per
Phase 2 SC#3 / CONTEXT.md D-05 closure. Driver asserts the RFC §4.3 ordering
invariant — any ``squash_merge_completed`` MUST be preceded by
``merge_queue_active_status_posted`` for the SAME PR.

Two-mode driver per CONTEXT.md D-04. Unit-test mode (this file) drives a
FakeGitHub extension that simulates:
  1. Per-PR ``/merge`` → ``mq:queued`` label apply (handler simulation).
  2. Sequenced activation per the worked-example ordering: A activates first;
     B and C activate as a parallel pair after A's squash; D activates after
     B+C squash; E activates after D's squash.
  3. Status comment lifecycle: queued comment on /merge, active comment on
     activation, "Squashed" comment on squash with the processor cycle URL +
     tree_diff_status='ahead' embedded for the driver to extract.
  4. Cycle-summary artifact downloadable per processor_run_url — contains the
     per-PR tree_diff_status line the driver scrapes.

Live-fork mode (``python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder --repo
rocm-libraries``) is operator-initiated AFTER all Phase 3 workflows are
deployed; NOT exercised in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_06
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


_STATUS_MARKER = "<!-- rocm-mq-status -->"


def test_scenario_id_is_dog_06() -> None:
    assert dog_06.SCENARIO_ID == "dog_06"


def test_timeout_budget_60_minutes() -> None:
    # Plan 03-14 must_haves: TIMEOUT_S == 60 * 60.
    assert dog_06.TIMEOUT_S == 60 * 60


def test_main_is_callable() -> None:
    assert callable(dog_06.main)


def test_run_scenario_is_callable() -> None:
    assert callable(dog_06.run_scenario)


def test_pr_descriptors_match_rfc_section_4_2() -> None:
    """Exactly 5 per-PR descriptors A..E with deterministic file paths.

    Per plan 03-14 <action>: "Define a per-PR descriptor list mapping to RFC
    §4.2: PR_A_PATHS, PR_B_PATHS, ... each a list of file paths derived from
    path_to_queues.yml so queue-membership matches the narrative".
    """
    descriptors = dog_06.PR_DESCRIPTORS
    assert len(descriptors) == 5
    letters = [d.letter for d in descriptors]
    assert letters == ["A", "B", "C", "D", "E"]
    # Each descriptor names at least one file path; all paths must be opted-in
    # per the path_to_queues.yml shape (verified in a separate test below).
    for d in descriptors:
        assert d.paths
        for p in d.paths:
            assert "/" in p, f"path {p!r} must be repo-relative (contain /)"


def test_pr_descriptor_paths_route_to_expected_queue_membership() -> None:
    """PR descriptor paths must materialize the RFC §4.2 queue-overlap shape.

    PR_A: integration-tests path → engages every provider + integration-tests
    PR_B: miopen-provider → single queue
    PR_C: hipblaslt-provider → single queue, disjoint from PR_B
    PR_D: integration-tests path → engages every provider + integration-tests
    PR_E: miopen-provider path → same queue as PR_B (narrative beat)
    """
    descriptors = {d.letter: d for d in dog_06.PR_DESCRIPTORS}
    # PR_A goes through integration-tests path (engages all providers).
    assert any(
        "integration-tests" in p for p in descriptors["A"].paths
    ), "PR_A path must engage integration-tests"
    # PR_B and PR_C live under different provider sub-paths (disjoint queues).
    b_paths = " ".join(descriptors["B"].paths)
    c_paths = " ".join(descriptors["C"].paths)
    assert "miopen-provider" in b_paths
    assert "hipblaslt-provider" in c_paths
    # PR_D engages integration-tests.
    assert any("integration-tests" in p for p in descriptors["D"].paths)
    # PR_E re-uses the miopen provider queue (same as B).
    assert any("miopen-provider" in p for p in descriptors["E"].paths)


# ---------------------------------------------------------------------------
# _DogfoodFake — simulates the 5-PR RFC §4.2 lifecycle deterministically
# ---------------------------------------------------------------------------


_TREE_DIFF_AHEAD_MARKER = "tree_diff_status=ahead"


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the extensions dog_06 orchestration needs.

    Wires:
      * ``rest.git.get_ref`` / ``create_ref`` — branch creation off develop.
      * ``rest.repos.create_or_update_file_contents`` — multi-file seed commit
        per PR descriptor (each PR's paths are written together in one commit).
      * ``rest.pulls.create`` — PR creation; seeds FakePR with the
        ``mq:queued`` label simulation deferred to /merge time.
      * ``rest.issues.create_comment`` wrapper — on ``/merge`` body, marks the
        PR as enqueued (state.prs[N].labels gets ``mq:queued``). Activation
        and squash transitions are driven externally via ``advance(...)``.
      * ``rest.actions.list_workflow_run_artifacts`` /
        ``download_artifact`` — returns a stub cycle-summary archive whose
        body carries ``tree_diff_status=ahead`` for each PR's squash run.
      * ``advance(letters)`` — test-side helper that flips the per-PR labels
        from queued → active → merged and posts the squash status comment
        with the processor run URL embedded. Called from the polling seam
        between driver polls.
    """

    def __init__(self, state: FakeRepoState) -> None:
        super().__init__(state)
        self._next_pr_number = 6000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        # Map (pr_letter -> pr_number) so advance(...) can look up the PR.
        self._letter_to_pr: dict[str, int] = {}
        # Activation/squash run URLs assigned per PR (deterministic in tests).
        self._run_url_per_pr: dict[int, str] = {}
        # Synthetic run_id used by the artifact-download stub.
        self._run_id_per_pr: dict[int, int] = {}
        # Sequence counter for naming squash status comments uniquely.
        self._squash_seq = 0
        # Set of PR numbers that have already had squash comment posted.
        self._squashed: set[int] = set()
        # Wire sub-namespaces.
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        self._base_create_comment = self.rest.issues.create_comment
        self.rest.issues.create_comment = self._wrapped_create_comment  # type: ignore[assignment]
        # Wire actions API.
        self.rest.actions = _ActionsNS(self)  # type: ignore[attr-defined]

    # -- repo file writes ----------------------------------------------------

    def _create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        message: str,
        content: str,
        branch: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        self._created_files.append({"path": path, "branch": branch, "message": message})
        commit_sha = f"commit_{len(self._created_files)}"
        return SimpleNamespace(
            parsed_data=SimpleNamespace(commit=SimpleNamespace(sha=commit_sha))
        )

    # -- PR creation ---------------------------------------------------------

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
        # Derive the per-PR letter from the title's "[dogfood dog_06 PR_X]" prefix.
        letter = ""
        if "PR_" in title:
            try:
                letter = title.split("PR_", 1)[1][0]
            except IndexError:
                letter = ""
        if letter:
            self._letter_to_pr[letter] = number
        self._created_pulls.append({"number": number, "head": head, "title": title})
        self.state.prs[number] = FakePR(number=number, head_sha=f"head_{number}_v1")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )

    # -- /merge wrapper — flip to mq:queued ----------------------------------

    def _wrapped_create_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str = "",
        **kwargs: Any,
    ) -> SimpleNamespace:
        resp = self._base_create_comment(owner, repo, issue_number, body=body, **kwargs)
        if body.strip() == "/merge":
            pr = self.state.prs.get(issue_number)
            if pr is not None:
                pr.labels.add("mq:queued")
        return resp

    # -- advance(letters) — test-side activation + squash driver -------------

    def advance(self, letters: list[str]) -> None:
        """Simulate one processor cycle that activates AND squashes ``letters``.

        For each letter:
          (a) Apply ``mq:active`` label.
          (b) Post a "merge_queue/active" status comment carrying the
              activation-time head SHA.
          (c) Apply ``mq:merged`` label.
          (d) Post a squash status comment carrying the processor run URL
              AND the documented ``tree_diff_status=ahead`` substring.

        The driver's polling loop interleaves with these advances; in the
        happy-path test the test fixture calls advance() between polls to
        drive the scenario forward deterministically.
        """
        for letter in letters:
            pr_number = self._letter_to_pr.get(letter)
            if pr_number is None:
                continue
            pr = self.state.prs.get(pr_number)
            if pr is None:
                continue
            if pr_number in self._squashed:
                continue
            # Activation.
            pr.labels.add("mq:active")
            run_id = 9000 + pr_number
            run_url = (
                f"https://github.test/owner/repo/actions/runs/{run_id}"
            )
            self._run_url_per_pr[pr_number] = run_url
            self._run_id_per_pr[pr_number] = run_id
            active_body = (
                f"{_STATUS_MARKER}\n"
                f"## Active\n"
                f"PR queued for merge; activation status posted on head SHA.\n"
                f"Processor cycle: {run_url}\n"
            )
            self._base_create_comment(
                "owner", "repo", pr_number, body=active_body
            )
            # Squash.
            self._squash_seq += 1
            pr.labels.discard("mq:active")
            pr.labels.add("mq:merged")
            squash_body = (
                f"{_STATUS_MARKER}\n"
                f"## Squashed\n"
                f"PR merged into develop.\n"
                f"Processor cycle: {run_url}\n"
                f"{_TREE_DIFF_AHEAD_MARKER}\n"
            )
            self._base_create_comment(
                "owner", "repo", pr_number, body=squash_body
            )
            self._squashed.add(pr_number)


class _GitNS:
    def __init__(self, refs: dict[str, str]) -> None:
        self._refs = refs

    def get_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        sha = self._refs.get(ref, "develop_initial_tip")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                ref=f"refs/{ref}", object=SimpleNamespace(sha=sha)
            )
        )

    def create_ref(
        self, owner: str, repo: str, *, ref: str, sha: str, **_: Any
    ) -> SimpleNamespace:
        short = ref[len("refs/"):] if ref.startswith("refs/") else ref
        self._refs[short] = sha
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                ref=ref, object=SimpleNamespace(sha=sha)
            )
        )


class _ActionsNS:
    """Stub for ``client.rest.actions.list_workflow_run_artifacts`` +
    ``download_artifact``.

    Returns a synthesized cycle-summary archive whose body carries the
    ``tree_diff_status=ahead`` line for the requested run_id's PR. The driver
    extracts the tree_diff_status field from this archive via
    ``download_cycle_summary_artifact`` (base scaffolding).
    """

    def __init__(self, fake: _DogfoodFake) -> None:
        self._fake = fake

    def list_workflow_run_artifacts(
        self, owner: str, repo: str, run_id: int
    ) -> SimpleNamespace:
        # Return one matching cycle-summary artifact for the requested run_id.
        artifact = SimpleNamespace(
            id=run_id * 10,
            name=f"cycle-summary-{run_id}",
        )
        return SimpleNamespace(
            parsed_data=SimpleNamespace(artifacts=[artifact])
        )

    def download_artifact(
        self, owner: str, repo: str, artifact_id: int, *, archive_format: str = "zip"
    ) -> SimpleNamespace:
        # Build an in-memory zip containing cycle-summary.md with the
        # tree_diff_status marker. The driver reads the first file in the
        # archive per the base scaffolding contract.
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "cycle-summary.md",
                "## Cycle outcomes\n"
                "- ✅ Squash #N\n"
                f"{_TREE_DIFF_AHEAD_MARKER}\n",
            )
        return SimpleNamespace(content=buf.getvalue())


# ---------------------------------------------------------------------------
# run_scenario — happy path + invariant assertions
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op sleep so poll_pr_state does not block."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def _make_happy_path_advance_hook(client: _DogfoodFake):
    """Return a per-poll callback that advances the scenario one beat at a time.

    The driver polls in a loop; the callback is invoked before each poll
    iteration. We sequence the per-letter advances so:
      poll 1: advance A
      poll 2: advance B, C  (parallel)
      poll 3: advance D
      poll 4: advance E
    After all five PRs are squashed, the predicate fires and the driver returns.
    """
    sequence = [["A"], ["B", "C"], ["D"], ["E"]]
    state = {"step": 0}

    def hook() -> None:
        if state["step"] < len(sequence):
            client.advance(sequence[state["step"]])
            state["step"] += 1

    return hook


def test_run_scenario_happy_path_passes(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """5 PRs created, all squash-merged, all invariants hold → passed=True."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    assert result.passed is True, (
        f"happy-path scenario must pass; observed_outcome={result.observed_outcome!r}"
    )
    assert result.scenario_id == "dog_06"
    # All 5 PRs created.
    assert len(client._created_pulls) == 5
    # All 5 PRs squashed.
    assert len(client._squashed) == 5
    # JSON emitted.
    written = list(tmp_path.glob("*-dog_06.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["scenario_id"] == "dog_06"


def test_run_scenario_emits_d04_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Per-run JSON includes every D-04 schema field."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    written = list(tmp_path.glob("*-dog_06.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    for field in (
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
    ):
        assert field in payload, f"missing D-04 field: {field}"


def test_timeline_contains_squash_event_for_every_pr(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Timeline carries one squash_merge_completed event per PR (5 total)."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    squash_events = [
        (ts, evt, state)
        for (ts, evt, state) in result.timeline
        if evt == "squash_merge_completed"
    ]
    assert len(squash_events) == 5


def test_timeline_each_squash_carries_tree_diff_status_ahead(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Every squash_merge_completed.observed_state.tree_diff_status == 'ahead'.

    Plan 03-14 must_haves: "Every squash_merge_completed event carries
    tree_diff_status='ahead' captured from processor $GITHUB_STEP_SUMMARY
    (defends Pitfall 8 — Phase 2 SC#3 closure)".
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    squash_events = [
        state
        for (_ts, evt, state) in result.timeline
        if evt == "squash_merge_completed"
    ]
    assert len(squash_events) == 5
    for state in squash_events:
        assert state.get("tree_diff_status") == "ahead", (
            f"squash event missing tree_diff_status=ahead: {state!r}"
        )


def test_timeline_ordering_invariant_active_before_squash_per_pr(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """For each PR, merge_queue_active_status_posted MUST precede squash_merge_completed.

    Plan 03-14 must_haves: "Driver asserts the event ordering invariant: any
    squash_merge_completed MUST be preceded by merge_queue_active_status_posted
    (RFC §4.3 binding)".
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    # Walk the timeline once, tracking per-PR first index of each event_type.
    active_at: dict[int, int] = {}
    squash_at: dict[int, int] = {}
    for idx, (_ts, evt, state) in enumerate(result.timeline):
        pr_n = state.get("pr_number") if isinstance(state, dict) else None
        if pr_n is None:
            continue
        if evt == "merge_queue_active_status_posted" and pr_n not in active_at:
            active_at[pr_n] = idx
        if evt == "squash_merge_completed" and pr_n not in squash_at:
            squash_at[pr_n] = idx
    # Every squash must have a prior active event for the SAME PR.
    for pr_n, squash_idx in squash_at.items():
        assert pr_n in active_at, (
            f"PR #{pr_n} squash event present but no merge_queue_active_status_posted"
        )
        assert active_at[pr_n] < squash_idx, (
            f"PR #{pr_n}: active event index {active_at[pr_n]} must precede "
            f"squash event index {squash_idx}"
        )


def test_processor_run_urls_deduplicated_tuple(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """processor_run_urls is a deduplicated tuple of contributing run URLs."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    # Tuple type (Pitfall 11 / D-04 schema hashability).
    assert isinstance(result.processor_run_urls, tuple)
    # Deduplicated.
    assert len(result.processor_run_urls) == len(set(result.processor_run_urls))
    # Non-empty (every PR contributed at least one cycle URL).
    assert len(result.processor_run_urls) >= 1


def test_failure_mode_tree_diff_status_missing_flips_passed_false(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If a squash comment lacks tree_diff_status=ahead → driver detects corruption.

    Per plan 03-14 must_haves: "main() asserts every squash_merge_completed.
    observed_state.tree_diff_status equals 'ahead' (Phase 2 SC#3 invariant)".
    A missing/wrong status MUST flip passed=False — this is the
    silent-corruption detection (Apr-2026 regression class, CONTEXT.md D-05).
    """
    _patch_timing(monkeypatch)

    # Subclass to corrupt PR_A's squash comment + artifact.
    class _CorruptedFake(_DogfoodFake):
        def advance(self, letters: list[str]) -> None:
            for letter in letters:
                pr_number = self._letter_to_pr.get(letter)
                if pr_number is None:
                    continue
                pr = self.state.prs.get(pr_number)
                if pr is None or pr_number in self._squashed:
                    continue
                # Standard activation.
                pr.labels.add("mq:active")
                run_id = 9000 + pr_number
                run_url = f"https://github.test/owner/repo/actions/runs/{run_id}"
                self._run_url_per_pr[pr_number] = run_url
                self._run_id_per_pr[pr_number] = run_id
                self._base_create_comment(
                    "owner", "repo", pr_number,
                    body=(
                        f"{_STATUS_MARKER}\n## Active\n"
                        f"Processor cycle: {run_url}\n"
                    ),
                )
                # CORRUPT squash for letter A — drop tree_diff_status line.
                pr.labels.discard("mq:active")
                pr.labels.add("mq:merged")
                if letter == "A":
                    squash_body = (
                        f"{_STATUS_MARKER}\n## Squashed\n"
                        f"Processor cycle: {run_url}\n"
                        # NO tree_diff_status line — silent-corruption shape.
                    )
                else:
                    squash_body = (
                        f"{_STATUS_MARKER}\n## Squashed\n"
                        f"Processor cycle: {run_url}\n"
                        f"{_TREE_DIFF_AHEAD_MARKER}\n"
                    )
                self._base_create_comment(
                    "owner", "repo", pr_number, body=squash_body
                )
                self._squashed.add(pr_number)

        # Also corrupt the artifact for PR_A so the artifact-download fallback
        # cannot recover the tree_diff_status from the cycle summary.
        # Override on the ActionsNS by replacing it.

    corrupted_client = _CorruptedFake(fake_state)
    # Wire the artifact stub to omit tree_diff_status for PR_A's run_id.

    class _CorruptActionsNS(_ActionsNS):
        def download_artifact(
            self, owner: str, repo: str, artifact_id: int, *,
            archive_format: str = "zip",
        ) -> SimpleNamespace:
            import io
            import zipfile

            # PR_A's PR_number is 6000 → run_id 15000 → artifact_id 150000.
            # All other PRs have artifact ids 150010+ etc. Strip
            # tree_diff_status from the FIRST PR's artifact only.
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                if artifact_id == (9000 + 6000) * 10:  # PR_A
                    zf.writestr(
                        "cycle-summary.md",
                        "## Cycle outcomes\n- ✅ Squash #6000\n(no tree-diff line)\n",
                    )
                else:
                    zf.writestr(
                        "cycle-summary.md",
                        f"## Cycle outcomes\n- ✅ Squash\n{_TREE_DIFF_AHEAD_MARKER}\n",
                    )
            return SimpleNamespace(content=buf.getvalue())

    corrupted_client.rest.actions = _CorruptActionsNS(corrupted_client)  # type: ignore[attr-defined]
    advance_hook = _make_happy_path_advance_hook(corrupted_client)

    result = dog_06.run_scenario(
        corrupted_client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    # The driver MUST detect the missing tree_diff_status on PR_A and flip
    # passed=False — this is the silent-corruption detection invariant.
    assert result.passed is False, (
        "missing tree_diff_status on PR_A squash MUST flip passed=False"
    )


def test_failure_mode_when_pr_never_activates_times_out(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If a PR never reaches mq:merged, the driver times out at the global poll."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)

    # Advance hook that only ever advances A — B/C/D/E never merge.
    def hook() -> None:
        client.advance(["A"])

    with pytest.raises(TimeoutError):
        dog_06.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            on_poll=hook,
        )


def test_b_and_c_can_activate_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """B and C both reach merged after a single combined advance step.

    Per the RFC §4.2 narrative beat: after A merges, B and C activate AND
    squash in the same processor-cycle window (they share no queue). The
    driver's timeline ordering should reflect this — both B's
    merge_queue_active_status_posted and C's appear before any subsequent
    PR's events.
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)
    advance_hook = _make_happy_path_advance_hook(client)

    result = dog_06.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        on_poll=advance_hook,
    )

    # All 5 PRs must have squash events.
    pr_to_letter = {pr_num: letter for letter, pr_num in client._letter_to_pr.items()}
    squashed_letters = {
        pr_to_letter[state["pr_number"]]
        for (_ts, evt, state) in result.timeline
        if evt == "squash_merge_completed" and "pr_number" in state
    }
    assert squashed_letters == {"A", "B", "C", "D", "E"}
