"""Tests for rocm_mq.dogfood.dog_03 — CI-failure-during-evaluation eject driver.

Scenario contract (RFC §6 DOG-03, plan 03-12): a PR opted into a queue is
``/merge``d, transitions through ``mq:queued`` → ``mq:active`` (activation
cycle), and at required-check evaluation the processor observes a FAILING
required check (supplied by the dogfood canary, plan 03-09, triggered by
the literal title substring ``[dogfood-ci-fail]``) → processor ejects with
a status-comment reason naming the failed check (literal substring of the
canary's registered check-run name, e.g. ``mq-dogfood-canary``, read at
runtime from ``path_to_queues.yml required_checks.dogfood-canary`` so this
driver does not bake the check name into source).

Two-mode driver per CONTEXT.md D-04. Unit-test mode (this file) exercises
the driver's orchestration against a FakeGitHub extension that simulates
the canary-failed check + the processor's eject status comment. Live-fork
mode (``python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder --repo
rocm-libraries``) is operator-initiated AFTER mq-handler.yml + mq-processor.yml
are deployed; NOT exercised in CI per the plan's Task 2 deferral pattern
(mirrors plan 03-09's deferral of the analogous Task 2 checkpoint).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_03
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants — locked by plan 03-12 Task 1 behavior block.
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_03() -> None:
    assert dog_03.SCENARIO_ID == "dog_03"


def test_timeout_budget_20_minutes() -> None:
    # RESEARCH.md Area #10: canary fast but cycle + eval + eject.
    assert dog_03.TIMEOUT_S == 20 * 60


def test_main_is_callable() -> None:
    assert callable(dog_03.main)


def test_run_scenario_is_callable() -> None:
    assert callable(dog_03.run_scenario)


def test_title_prefix_contains_dogfood_ci_fail_marker() -> None:
    """The driver must inject the canary's title-substring trigger into the PR
    title; without this marker the canary exits 0 and the scenario cannot
    materialize (RFC §6 DOG-03 requires the canary FAIL signal).
    """
    assert "[dogfood-ci-fail]" in dog_03.TITLE_PREFIX


# ---------------------------------------------------------------------------
# _DogfoodFake — FakeGitHub extension simulating
#   1. The path_to_queues.yml contents (so config.load_from_develop returns
#      a parsed dict carrying the canary required-check name).
#   2. The processor's eject status-comment (with the canary check name in
#      the body) landing on the PR.
# Mirrors the dog_02 _DogfoodFake pattern.
# ---------------------------------------------------------------------------


# The placeholder pinned in .github/merge-queue/path_to_queues.yml today
# (plan 03-03 Task 1). The driver reads this string from the YAML at
# runtime — tests pin the same value here so the assertion shape is
# stable even if the YAML pin shifts (the predicate uses whatever the
# loader returned, not this constant).
_CANARY_CHECK_NAME = "mq-dogfood-canary / canary"
_CANARY_SUBSTRING = "mq-dogfood-canary"

_PATH_TO_QUEUES_YAML = (
    "queues: [dogfood-canary]\n"
    "paths:\n"
    "  - path: dogfood/\n"
    "    queues: [dogfood-canary]\n"
    "required_checks:\n"
    f"  dogfood-canary: ['{_CANARY_CHECK_NAME}']\n"
)


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the git/repos/pulls extensions dog_03 needs.

    Wires:
      * ``rest.git.get_ref`` / ``create_ref`` — branch creation off develop.
      * ``rest.repos.create_or_update_file_contents`` — PR-branch seed file.
      * ``rest.repos.get_content`` — returns the path_to_queues.yml payload
        the driver loads via ``config.load_from_develop``.
      * ``rest.pulls.create`` — PR creation; the produced PR carries the
        ``[dogfood-ci-fail]`` title prefix the driver passes in.
      * ``inject_eject_comment`` — test-only seam invoked from the
        polling predicate; simulates the processor posting an eject
        status comment containing the canary check name.
    """

    def __init__(
        self,
        state: FakeRepoState,
        *,
        eject_comment_body: str | None = None,
        yaml_payload: str = _PATH_TO_QUEUES_YAML,
    ) -> None:
        super().__init__(state)
        self._next_pr_number = 4000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._eject_comment_body = eject_comment_body
        self._comment_posted = False
        self._yaml_payload = yaml_payload
        # Wire sub-namespaces — kept LOCAL per the 03-10 pattern.
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.repos.get_content = self._get_content  # type: ignore[attr-defined]
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]

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
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                commit=SimpleNamespace(sha=f"commit_{len(self._created_files)}")
            )
        )

    def _get_content(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        """Return the path_to_queues.yml payload, base64-encoded as the API
        delivers it (config.load_from_develop base64-decodes before parsing).
        """
        encoded = base64.b64encode(self._yaml_payload.encode("utf-8")).decode("ascii")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(content=encoded, encoding="base64")
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
        self._created_pulls.append({"number": number, "head": head, "title": title})
        self.state.prs[number] = FakePR(number=number, head_sha=f"head_{number}")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )

    def inject_eject_comment(self, pr_number: int) -> None:
        """Simulate the processor posting the eject status-comment."""
        if self._eject_comment_body is None:
            return
        if self._comment_posted:
            return
        self.rest.issues.create_comment(
            "owner", "repo", pr_number, body=self._eject_comment_body
        )
        self._comment_posted = True


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
        short = ref[len("refs/") :] if ref.startswith("refs/") else ref
        self._refs[short] = sha
        return SimpleNamespace(
            parsed_data=SimpleNamespace(ref=ref, object=SimpleNamespace(sha=sha))
        )


# ---------------------------------------------------------------------------
# run_scenario tests — happy path + failure modes
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op sleep so poll_pr_state does not block."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def test_run_scenario_happy_path_returns_passed_result(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Eject comment with the canary check-name substring → passed=True."""
    _patch_timing(monkeypatch)
    eject_body = (
        "<!-- rocm-mq-status -->\n"
        f"## Ejected: required check `{_CANARY_CHECK_NAME}` failed on head SHA\n"
        "Processor cycle: https://github.test/x/y/actions/runs/888\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_03.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_comment,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_03"
    assert result.observed_outcome["action"] == "Eject"
    # Reason substring captured at runtime from path_to_queues.yml.
    assert _CANARY_SUBSTRING in result.observed_outcome["reason"]
    # Expected outcome built at runtime — its reason_substring matches the
    # canary check-run name read from the YAML loader.
    assert _CANARY_SUBSTRING in result.expected_outcome["reason_substring"]
    # PR was created.
    assert result.pr_number == 4000
    # The PR title prefix (recorded as the seeded pull) carries the canary
    # title-substring trigger.
    assert any("[dogfood-ci-fail]" in str(p["title"]) for p in client._created_pulls)
    # Seed file is under dogfood/ so the canary workflow's path filter fires.
    seeded_paths = [f["path"] for f in client._created_files]
    assert any(p.startswith("dogfood/") for p in seeded_paths)
    # Per-run JSON emitted with passed=True.
    written = list(tmp_path.glob("*-dog_03.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["scenario_id"] == "dog_03"


def test_run_scenario_failure_mode_when_reason_missing_check_name(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Status comment lands without the canary check-name substring → timeout.

    The predicate must only succeed when the eject reason names the failed
    canary check; an eject comment with some other reason ("merge conflict
    with develop", for example) must NOT be accepted as DOG-03 evidence.
    """
    _patch_timing(monkeypatch)
    eject_body = "<!-- rocm-mq-status -->\n## Ejected: merge conflict with develop\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    with pytest.raises(TimeoutError):
        dog_03.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            inject_eject_after=client.inject_eject_comment,
        )


def test_run_scenario_emits_json_with_d04_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Per-run JSON includes every D-04 schema field."""
    _patch_timing(monkeypatch)
    eject_body = "<!-- rocm-mq-status -->\n" f"Ejected: `{_CANARY_CHECK_NAME}` failed\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    dog_03.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_comment,
    )

    written = list(tmp_path.glob("*-dog_03.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    # D-04 schema check — every field present.
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


def test_run_scenario_timeline_contains_required_checks_failed_event(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """The timeline must record a ``required_checks_failed`` event naming
    the canary check (RESEARCH.md Area #12 event vocabulary).
    """
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: `{_CANARY_CHECK_NAME}` failed\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_03.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_comment,
    )

    event_types = [evt for (_ts, evt, _state) in result.timeline]
    assert "required_checks_failed" in event_types
    # The required_checks_failed event must name the canary check.
    failed_evt = next(
        s for (_ts, evt, s) in result.timeline if evt == "required_checks_failed"
    )
    assert _CANARY_SUBSTRING in json.dumps(failed_evt)


# (test_expected_outcome_derived_from_yaml_payload removed per 03-wr-09 —
# the driver no longer reads the canary check name from the YAML's
# required_checks map (that map was deleted). The canary name is now a
# module constant in dog_03; test_dog_03_canary_check_name_pins_load_bearing_prefix
# below pins that contract.)


# ---------------------------------------------------------------------------
# Pinned-fork-yaml sanity check — guards against a drift between the
# real .github/merge-queue/path_to_queues.yml and the canary check-name
# substring the driver matches on.
# ---------------------------------------------------------------------------


def test_dog_03_canary_check_name_pins_load_bearing_prefix() -> None:
    """The dog_03 module-level canary name constant must contain the
    load-bearing 'mq-dogfood-canary' substring (the substring the eject-
    reason match uses). Post 03-wr-09 the canary name lives in the driver
    module, not in path_to_queues.yml; this test guards against the
    constant drifting out of alignment with the canary workflow."""
    from rocm_mq.dogfood.dog_03 import _CANARY_CHECK_NAME

    assert "mq-dogfood-canary" in _CANARY_CHECK_NAME, (
        f"_CANARY_CHECK_NAME={_CANARY_CHECK_NAME!r} no longer contains the "
        "'mq-dogfood-canary' prefix; update dog_03 driver or re-name the "
        "canary workflow file."
    )
