"""
tests/test_mq_dogfood_canary_yaml.py — Smoke tests for mq-dogfood-canary.yml.

PARSE-LEVEL tripwires only: confirm that the deterministic CI-fail canary
workflow (plan 03-09; CONTEXT.md D-03) keeps the load-bearing shape that
makes the rest of the dogfood pipeline correct:

  * Triggers on `pull_request` (NOT `pull_request_target`) — sidesteps
    Pitfall 1 because the canary has no secrets, no App token, no checkout.
  * Filtered to `dogfood/**` paths so real-code PRs are never blocked.
  * No `actions/checkout`, no `actions/setup-python`, no
    `actions/create-github-app-token` — the canary decides solely from the
    event payload (PR title substring match).
  * Workflow-level `permissions: contents: read` (minimum scope).
  * Job has `timeout-minutes: 2`.
  * Verdict step is literal-substring `[dogfood-ci-fail]` against
    `github.event.pull_request.title`, passed via env (NOT shell
    interpolation — T-03-09-03 mitigation).

Locate the YAML via pathlib so the test runs from any CWD.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

# .github/merge-queue/tests/test_mq_dogfood_canary_yaml.py
#   parents[0] = tests/
#   parents[1] = .github/merge-queue/
#   parents[2] = .github/
#   parents[3] = <repo root>
_YAML_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "mq-dogfood-canary.yml"
)


@pytest.fixture(scope="module")
def doc() -> dict:
    if not _YAML_PATH.exists():
        pytest.fail(f"mq-dogfood-canary.yml not found at {_YAML_PATH}")
    with _YAML_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def raw_text() -> str:
    if not _YAML_PATH.exists():
        pytest.fail(f"mq-dogfood-canary.yml not found at {_YAML_PATH}")
    return _YAML_PATH.read_text(encoding="utf-8")


def test_parses_as_yaml(doc: dict) -> None:
    """yaml.safe_load succeeds and returns a top-level mapping."""
    assert isinstance(doc, dict)


def test_workflow_name(doc: dict) -> None:
    """Top-level `name:` is `mq-dogfood-canary` — half of the registered
    check-run context name (the other half is the job name)."""
    assert doc.get("name") == "mq-dogfood-canary"


def test_trigger_is_pull_request_not_target(doc: dict) -> None:
    """Trigger is `pull_request` (NOT `pull_request_target`) — Pitfall 1
    mitigation; no secrets are accessible on this trigger."""
    # PyYAML parses bare `on:` as the Python boolean True (the YAML 1.1
    # quirk); accept either key form so the test does not become a YAML-
    # version assertion.
    on_block = doc.get("on") if "on" in doc else doc.get(True)
    assert on_block is not None, "workflow has no `on:` block"
    assert "pull_request" in on_block
    assert "pull_request_target" not in on_block


def test_trigger_types_and_paths(doc: dict) -> None:
    on_block = doc.get("on") if "on" in doc else doc.get(True)
    pr = on_block["pull_request"]
    assert pr["types"] == ["opened", "synchronize", "reopened"]
    assert pr["paths"] == ["dogfood/**"]


def test_workflow_level_permissions_minimum_scope(doc: dict) -> None:
    """Workflow-level `permissions: contents: read` — Pitfall 16 minimum
    scope. The canary reads nothing more than the event payload."""
    perms = doc.get("permissions")
    assert perms == {"contents": "read"}


def test_canary_job_present(doc: dict) -> None:
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict)
    assert "canary" in jobs, "expected `canary` job (defines check-run name suffix)"


def test_canary_job_timeout(doc: dict) -> None:
    job = doc["jobs"]["canary"]
    assert job.get("timeout-minutes") == 2


def test_canary_job_runs_on(doc: dict) -> None:
    job = doc["jobs"]["canary"]
    assert job.get("runs-on") == "ubuntu-latest"


def test_no_checkout_no_setup_python_no_app_token(raw_text: str) -> None:
    """The canary has no checkout, no Python install, no App-token mint —
    CONTEXT.md D-03 ('no secrets, no checkout'). This is a textual check on
    the workflow source because the action references live inside `uses:`
    strings that we want to forbid by literal substring."""
    assert "actions/checkout" not in raw_text
    assert "actions/setup-python" not in raw_text
    assert "actions/create-github-app-token" not in raw_text


def test_no_permission_inputs(raw_text: str) -> None:
    """No `permission-*` inputs anywhere — confirms no App-token-mint step
    snuck in. (The plan's verification surface lists `grep -c permission- = 0`.)"""
    assert "permission-" not in raw_text


def test_no_environment_declaration(raw_text: str) -> None:
    """The canary MUST NOT declare `environment: mq-secrets`. It runs on
    `pull_request` (no secrets accessible anyway) and adding an environment
    would force a deployment-branch policy check on every PR."""
    # Check inside the job block specifically; allow the word elsewhere in
    # comments.
    for line in raw_text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        # `environment:` as a YAML key (line ending with `environment:`).
        assert not stripped.endswith("environment:") and " environment:" not in stripped, (
            f"unexpected `environment:` declaration on line: {line!r}"
        )


def test_verdict_step_reads_title_via_env(doc: dict, raw_text: str) -> None:
    """The decision step exposes the PR title via the `TITLE` env var and
    matches it as a bash literal substring — NOT via shell interpolation
    (T-03-09-03: shell-injection mitigation)."""
    steps = doc["jobs"]["canary"]["steps"]
    assert isinstance(steps, list)
    decide = next((s for s in steps if "canary verdict" in (s.get("name") or "").lower()), None)
    assert decide is not None, "expected a step named 'Decide canary verdict'"
    # env.TITLE binds the title.
    env = decide.get("env") or {}
    assert "TITLE" in env
    assert "github.event.pull_request.title" in env["TITLE"]
    # The shell body must compare $TITLE as a bash literal substring.
    body = decide.get("run") or ""
    assert "[dogfood-ci-fail]" in body
    assert '"$TITLE"' in body or "$TITLE" in body
    # No `${{ github.event.pull_request.title }}` inside the run-block
    # itself — that would be the unsafe shell-interpolation pattern.
    assert "${{ github.event.pull_request.title }}" not in body


def test_verdict_step_exits_1_on_marker(doc: dict) -> None:
    """Shell body has an `exit 1` path for the marker match and an
    implicit/explicit pass path otherwise."""
    steps = doc["jobs"]["canary"]["steps"]
    decide = next(s for s in steps if "canary verdict" in (s.get("name") or "").lower())
    body = decide["run"]
    assert "exit 1" in body
    # Pass-branch acknowledgement (echo "Canary PASS" or similar) — at
    # minimum the script should reach a benign tail after the failure exit.
    assert "PASS" in body or "pass" in body


def test_header_banner_notes_self_bootstrap(raw_text: str) -> None:
    """The file opens with a comment banner that flags SELF_BOOTSTRAP_PATHS
    membership so future maintainers reading the workflow source know the
    file is /merge-blocked."""
    first_lines = raw_text.splitlines()[:10]
    joined = "\n".join(first_lines).lower()
    assert "self_bootstrap" in joined or "self-bootstrap" in joined or "rfc §8" in joined
