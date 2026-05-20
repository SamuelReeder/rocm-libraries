"""
rocm_mq.dogfood._base — Driver scaffolding for the Phase 3 fork-dogfood suite.

Consumed by every DOG-02..DOG-08 driver (plans 03-11..03-15) and by the
aggregator (plan 03-16). Provides:

  * ``DogfoodResult`` — frozen-slots dataclass pinning the per-run JSON schema
    locked by CONTEXT.md D-04.  The 12-field shape is the stable on-disk
    contract that the aggregator (plan 03-16) reads via ``json.loads`` and
    that the Phase 5 porting-prep replay kit ships verbatim — additions are
    forward-compatible only if every field stays present.
  * ``create_dogfood_pr`` — opens a fresh dogfood PR off ``develop`` with a
    deterministic branch name (``dogfood/{scenario_id}-{8-hex}``).  Drivers
    use this to seed a clean PR per scenario per CONTEXT.md D-04 ("Each
    driver creates a fresh PR per scenario").
  * ``post_command`` — convenience wrapper around ``issues.create_comment``
    that returns the created comment's numeric id (drivers post ``/merge``
    or ``/dequeue`` and later assert the eyes-reaction landed on that id).
  * ``poll_pr_state`` — bounded polling helper (``timeout_s`` REQUIRED;
    threat T-03-10-02 mitigation — drivers must explicitly opt into a
    timeout, no infinite-loop default).  Calls a caller-supplied predicate
    until it returns truthy or ``timeout_s`` seconds have elapsed
    (``time.monotonic`` clock).
  * ``emit_result`` — writes the per-run JSON to
    ``.planning/phases/03-handler-processor-on-fork/dogfood-runs/{ISO}-{id}.json``
    with the D-04 key set.  Colons in the ISO timestamp are sanitized to
    keep the filename Windows-safe.
  * ``download_cycle_summary_artifact`` — fetches the ``cycle-summary-*``
    artifact uploaded by ``mq-processor.yml`` (RESEARCH.md Area #11) and
    returns its UTF-8 contents.  Gracefully returns ``""`` on any failure
    (missing artifact, no match, fetch error) so a driver missing a single
    cycle summary does not crash the whole scenario.

PURE-09 compliance: this module is I/O layer (CONTEXT.md D-02); it is NOT
in ``tests/test_pure_layer_imports.py::PURE_LAYER_MODULES`` and freely
imports ``rocm_mq.gh``, ``base64``, ``json``, ``zipfile``.

Timeline event vocabulary lives in RESEARCH.md Area #12 (17 documented
``event_type`` names).  Drivers emit a subset; this module does not constrain
the event_type strings — it only requires ``DogfoodResult.timeline`` to be
a tuple of ``(iso_ts, event_type, observed_state)`` tuples (Pitfall 11:
tuple, not list, so the result is hashable + immutable).
"""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import secrets
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rocm_mq.gh import GitHubClient

# ---------------------------------------------------------------------------
# Default output directory (CONTEXT.md D-04).  Drivers in CI mode write here;
# tests pass an explicit ``output_dir`` to ``emit_result`` to redirect.
# ---------------------------------------------------------------------------

_DEFAULT_OUTPUT_DIR = Path(".planning/phases/03-handler-processor-on-fork/dogfood-runs")

# Artifact-name prefix the processor uploads with (RESEARCH.md Area #11).
_CYCLE_SUMMARY_ARTIFACT_PREFIX = "cycle-summary-"


# ---------------------------------------------------------------------------
# DogfoodResult — the D-04 schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DogfoodResult:
    """Per-scenario result emitted by a dogfood driver.

    Field set is LOCKED by CONTEXT.md D-04; the aggregator (plan 03-16) and
    the Phase 5 porting-prep replay kit both read the JSON shape this
    dataclass serializes to.  Adding a field requires updating both
    consumers; removing a field is a breaking change.

    Frozen + slots: hashable, no ``__dict__``, no per-instance mutation
    (CLAUDE.md "Frozen ``@dataclass(frozen=True, slots=True)`` (NOT
    Pydantic)").  ``timeline`` and ``processor_run_urls`` are tuples (NOT
    lists) so the whole instance is hashable and the JSON sort order is
    stable; this also addresses Pitfall 11 (Hypothesis-bundle / mutation
    trap — future invariant testing over the timeline must operate on an
    immutable view).
    """

    scenario_id: str
    run_started_at: str  # ISO-8601 with tzinfo
    run_ended_at: str
    pr_number: int
    pr_url: str
    expected_outcome: dict[str, Any]
    observed_outcome: dict[str, Any]
    timeline: tuple[tuple[str, str, dict[str, Any]], ...]
    processor_run_urls: tuple[str, ...]
    step_summary_excerpt: str
    passed: bool
    notes: str


# ---------------------------------------------------------------------------
# create_dogfood_pr — open a fresh PR off develop with deterministic branch
# ---------------------------------------------------------------------------


def create_dogfood_pr(
    client: GitHubClient,
    owner: str,
    repo: str,
    scenario_id: str,
    file_path: str,
    file_content: str,
    title_suffix: str = "",
    title_prefix: str | None = None,
) -> tuple[int, str]:
    """Open a dogfood PR; return ``(pr_number, pr_html_url)``.

    Steps (RESEARCH.md Area #10):
      1. Read the current ``heads/develop`` tip SHA.
      2. Create a new branch ``dogfood/{scenario_id}-{8-hex}`` off that tip.
      3. Commit ``file_content`` to ``file_path`` on the new branch
         (``create_or_update_file_contents``; base64-encoded content per the
         GitHub REST contract).
      4. Open a PR with title ``"{title_prefix or '[dogfood {scenario_id}]'} {title_suffix}"``
         (whitespace-collapsed).

    The 8-hex suffix comes from ``secrets.token_hex(4)`` so concurrent runs
    of the same scenario_id do not collide on the branch name.  Drivers do
    NOT delete the branch/PR; CONTEXT.md D-04 ("drivers DO NOT delete PR or
    branch — they leave them for audit").
    """
    short_hash = secrets.token_hex(4)
    branch = f"dogfood/{scenario_id}-{short_hash}"

    # 1. Resolve develop tip. The githubkit GitRef pydantic model exposes
    # the target commit as ``object_`` (trailing underscore — ``object`` is
    # a Python builtin); accept either spelling so a fake that uses bare
    # ``.object`` continues to work alongside the real client.
    ref_resp = client.rest.git.get_ref(owner, repo, "heads/develop")
    _ref_obj = ref_resp.parsed_data
    _target = getattr(_ref_obj, "object_", None) or _ref_obj.object  # type: ignore[attr-defined]
    develop_tip: str = _target.sha

    # 2. Create branch ref off the tip.
    client.rest.git.create_ref(
        owner,
        repo,
        ref=f"refs/heads/{branch}",
        sha=develop_tip,
    )

    # 3. Commit the file (base64-encoded content per Contents API spec).
    encoded = base64.b64encode(file_content.encode("utf-8")).decode("ascii")
    client.rest.repos.create_or_update_file_contents(
        owner,
        repo,
        file_path,
        message=f"[dogfood {scenario_id}] seed {file_path}",
        content=encoded,
        branch=branch,
    )

    # 4. Open the PR.
    prefix = title_prefix if title_prefix is not None else f"[dogfood {scenario_id}]"
    title = f"{prefix} {title_suffix}".strip()
    body = (
        f"Auto-generated by `rocm_mq.dogfood` for scenario `{scenario_id}`. "
        f"This PR is part of the Phase 3 fork-dogfood evidence pack."
    )
    pr_resp = client.rest.pulls.create(
        owner,
        repo,
        title=title,
        head=branch,
        base="develop",
        body=body,
        # Same-repo dogfood PRs have maintainer_can_modify=False by default
        # on creation, which trips the handler's maintainer-edits at-enqueue
        # gate. Set explicitly so the dogfood drivers exercise the
        # accepted-merge path. Real PRs (not dogfood) are expected to set
        # this themselves; the gate stays meaningful for upstream traffic.
        maintainer_can_modify=True,
    )
    pr = pr_resp.parsed_data
    return int(pr.number), str(pr.html_url)


# ---------------------------------------------------------------------------
# post_command — post a /merge or /dequeue comment; return its id
# ---------------------------------------------------------------------------


def post_command(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    cmd: str = "/merge",
) -> int:
    """Post ``cmd`` as an issue comment on ``pr_number``; return the comment id.

    Drivers use the returned id to assert the eyes-reaction landed on the
    exact trigger comment (RESEARCH.md Area #7 idempotency) and to attach
    the ``merge_command_posted`` event to a stable URL.
    """
    resp = client.rest.issues.create_comment(owner, repo, pr_number, body=cmd)
    return int(resp.parsed_data.id)


# ---------------------------------------------------------------------------
# poll_pr_state — bounded polling helper
# ---------------------------------------------------------------------------


def poll_pr_state(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    predicate: Callable[[Any, str, str, int], Any],
    timeout_s: float,
    interval_s: float = 15.0,
) -> Any:
    """Poll ``predicate(client, owner, repo, pr_number)`` until truthy / timeout.

    ``timeout_s`` is REQUIRED (threat T-03-10-02: no infinite-loop default).
    Returns the predicate's truthy return value as soon as it returns one;
    raises ``TimeoutError`` after ``timeout_s`` seconds (``time.monotonic``
    elapsed clock — robust against wall-clock jumps).

    Sleeps ``interval_s`` seconds between polls.  Tests monkeypatch
    ``rocm_mq.dogfood._base.time.sleep`` (no-op) and
    ``rocm_mq.dogfood._base.time.monotonic`` (fake counter) to avoid real
    waits.  Production drivers receive real sleeps.

    The predicate's return-value contract: any truthy value is treated as
    "satisfied" and returned to the caller verbatim.  A driver typically
    returns a dict modelling the observed state (e.g.,
    ``{"action": "Eject", "reason": "..."}``).  Falsy returns (``None``,
    empty dict, ``False``) keep polling.
    """
    start = time.monotonic()
    while True:
        observed = predicate(client, owner, repo, pr_number)
        if observed:
            return observed
        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            raise TimeoutError(
                f"poll_pr_state timed out after {elapsed:.1f}s "
                f"(budget {timeout_s:.1f}s) waiting on PR #{pr_number} in "
                f"{owner}/{repo}"
            )
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# emit_result — write DogfoodResult JSON to per-run file
# ---------------------------------------------------------------------------


def emit_result(result: DogfoodResult, *, output_dir: Path | None = None) -> Path:
    """Write ``result`` as JSON; return the resulting path.

    Default ``output_dir`` is
    ``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` per
    CONTEXT.md D-04.  Tests override via ``output_dir=tmp_path``.

    Filename shape: ``{run_started_at_safe}-{scenario_id}.json`` where
    ``run_started_at_safe`` replaces the ISO timestamp's colons with ``-``
    so the file is Windows-safe (NTFS rejects ``:`` in filenames) and
    shell-friendly on Unix (no quoting needed).

    Parent directory is created with ``mkdir(parents=True, exist_ok=True)``
    so the first run on a fresh checkout works.

    JSON serialization: ``json.dumps(..., indent=2, sort_keys=True,
    default=str)`` — sorted keys for deterministic diffs across runs;
    ``default=str`` defensively coerces any non-JSON-native types (none
    expected today, but cheap insurance).
    """
    target_dir = output_dir if output_dir is not None else _DEFAULT_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_ts = result.run_started_at.replace(":", "-")
    filename = f"{safe_ts}-{result.scenario_id}.json"
    path = target_dir / filename

    payload = dataclasses.asdict(result)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# download_cycle_summary_artifact — fetch cycle-summary.md from a processor run
# ---------------------------------------------------------------------------


def download_cycle_summary_artifact(
    client: GitHubClient,
    owner: str,
    repo: str,
    run_id: int,
) -> str:
    """Return the cycle-summary artifact's content for ``run_id``, or ``""``.

    RESEARCH.md Area #11: ``mq-processor.yml`` uploads its
    ``$GITHUB_STEP_SUMMARY`` content as an artifact named
    ``cycle-summary-{run_id}``.  Drivers fetch this so the per-run JSON's
    ``step_summary_excerpt`` includes the discover/derive/sort/ready/
    activate-or-evaluate trace that the §6 invariant assertion rests on
    (otherwise the JSON is a black box).

    Returns the EMPTY STRING on ANY failure (no artifacts, no matching
    name, archive fetch error, unzip error, decode error).  This keeps a
    driver scenario whose cycle-summary upload happened to fail (e.g., the
    processor crashed before the upload-artifact step) from cascading into
    a driver crash — the JSON simply records an empty excerpt and the
    operator inspects the GHA run UI directly.
    """
    try:
        artifacts_resp = client.rest.actions.list_workflow_run_artifacts(owner, repo, run_id)
        artifacts = artifacts_resp.parsed_data.artifacts
    except Exception:  # graceful fallback per docstring
        return ""

    matching = [a for a in artifacts if str(a.name).startswith(_CYCLE_SUMMARY_ARTIFACT_PREFIX)]
    if not matching:
        return ""

    target = matching[0]
    try:
        archive_resp = client.rest.actions.download_artifact(
            owner, repo, int(target.id), archive_format="zip"
        )
        archive_bytes: bytes = archive_resp.content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            names = zf.namelist()
            if not names:
                return ""
            with zf.open(names[0]) as fh:
                return fh.read().decode("utf-8")
    except Exception:  # graceful fallback per docstring
        return ""
