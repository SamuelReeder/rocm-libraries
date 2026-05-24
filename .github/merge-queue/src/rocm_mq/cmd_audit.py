"""RFC §4.3.1 real-time audit command for merge-queue tamper events."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from githubkit.exception import RequestFailed

from rocm_mq.comment import render_status_body
from rocm_mq.executor import _find_status_comment_id
from rocm_mq.state import AppIdentity, MergeQueueConfig, PRState, RenderContext


@dataclass(frozen=True, slots=True)
class AuditClassification:
    """Structured classification for one audit webhook event."""

    family: str
    detail: str
    event_name: str
    action: str
    pr_number: int
    label_name: str | None
    actor_login: str
    actor_id: int | None
    should_eject: bool


def _classify_event(
    event_name: str, event: dict[str, Any], config: MergeQueueConfig
) -> AuditClassification:
    """Classify a webhook payload before any GitHub mutation.

    The payload is authoritative only for event-row attribution. Callers that
    need to mutate queue state must read the live PR after this function returns
    and use that live state as the cleanup source.
    """

    action = str(event.get("action") or "")
    pull_request = event.get("pull_request") or {}
    if not isinstance(pull_request, dict):
        pull_request = {}
    pr_number = int(pull_request.get("number") or event.get("number") or 0)
    sender = event.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    actor_login = str(sender.get("login") or "")
    actor_id = _optional_int(sender.get("id"))
    label_name = _payload_label_name(event)
    payload_labels = _payload_label_names(pull_request)

    family = "none"
    detail = "irrelevant-event"
    should_eject = False

    if action in {"labeled", "unlabeled"}:
        if label_name is None or not label_name.startswith(config.label_prefix):
            detail = "irrelevant-label"
        else:
            family = "label tamper"
            detail = "post-open-add" if action == "labeled" else "post-open-remove"
            should_eject = True
    elif action == "opened":
        if _has_mq_label(payload_labels, config):
            family = "label tamper"
            detail = "labels-at-open"
            should_eject = True
        else:
            detail = "no-current-mq-labels"
    elif action == "edited":
        if "base" in (event.get("changes") or {}):
            if _has_mq_label(payload_labels, config):
                family = "lifecycle invalidation"
                detail = "base-ref-change"
                should_eject = True
            else:
                detail = "no-current-mq-labels"
    elif action == "converted_to_draft":
        if _has_mq_label(payload_labels, config):
            family = "lifecycle invalidation"
            detail = "draft-conversion"
            should_eject = True
        else:
            detail = "no-current-mq-labels"
    elif action == "reopened":
        if _has_mq_label(payload_labels, config):
            family = "lifecycle invalidation"
            detail = "stale-label-reopen"
            should_eject = True
        else:
            detail = "no-current-mq-labels"

    if should_eject and _is_canonical_app_sender(sender, config.app_identity):
        family = "none"
        detail = "canonical-app-self-trigger"
        should_eject = False

    return AuditClassification(
        family=family,
        detail=detail,
        event_name=event_name,
        action=action,
        pr_number=pr_number,
        label_name=label_name,
        actor_login=actor_login,
        actor_id=actor_id,
        should_eject=should_eject,
    )


def _is_canonical_app_sender(sender: dict[str, Any], expected: AppIdentity) -> bool:
    """Return True only for the merge-queue App bot user webhook sender."""

    expected_login = f"{expected.slug}[bot]"
    return (
        str(sender.get("type") or "") == "Bot"
        and str(sender.get("login") or "") == expected_login
        and _optional_int(sender.get("id")) == expected.bot_user_id
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rocm-mq-audit",
        description="RFC §4.3.1 merge-queue tamper audit.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository in OWNER/REPO form.",
    )
    parser.add_argument(
        "--event-path",
        default=os.environ.get("GITHUB_EVENT_PATH", ""),
        help="Path to the GHA pull_request_target event JSON payload.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the tamper audit job.

    Returns 0 for handled ejects and handled no-ops, 2 for usage/auth setup
    errors, and 1 for uncaught operational failures after printing a traceback.
    """

    args = _parse_args(argv)
    if "/" not in args.repo or args.repo.count("/") != 1:
        print(
            f"error: --repo must be in OWNER/REPO form (got {args.repo!r}); "
            "either pass --repo or set $GITHUB_REPOSITORY.",
            file=sys.stderr,
        )
        return 2
    owner, repo = args.repo.split("/", 1)
    if not owner or not repo:
        print(
            f"error: --repo must be non-empty OWNER/REPO (got {args.repo!r}).",
            file=sys.stderr,
        )
        return 2

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; the workflow "
            "must mint an App installation token via actions/create-github-app-token "
            "and export it as GITHUB_TOKEN.",
            file=sys.stderr,
        )
        return 2

    try:
        return _run(args, owner, repo, token)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, owner: str, repo: str, token: str) -> int:
    event_path = args.event_path
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)
    if not isinstance(event, dict):
        return 0

    client = _build_client(token)
    config = _load_config(client, owner, repo)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    classification = _classify_event(event_name, event, config)
    if not classification.should_eject:
        return 0

    pr_resp = client.rest.pulls.get(owner, repo, classification.pr_number)
    live_pr = pr_resp.parsed_data
    live_labels = _live_label_names(live_pr)
    if not _live_state_requires_eject(classification, live_pr, live_labels, config):
        return 0

    now = datetime.now(tz=UTC)
    pr_state = _build_live_pr_state(
        classification.pr_number, live_pr, live_labels, config, now
    )
    _eject_live_pr(client, config, owner, repo, pr_state, classification, live_pr)
    return 0


def _build_client(token: str) -> Any:
    """Construct a real GitHub client. Tests monkeypatch this seam."""

    from rocm_mq.gh import GitHubClient

    return GitHubClient(token=token)


def _load_config(client: Any, owner: str, repo: str) -> MergeQueueConfig:
    """Load runtime config from develop; never from the local checkout."""

    from rocm_mq.config import build_config_from_develop

    return build_config_from_develop(client, owner, repo)


def _live_state_requires_eject(
    classification: AuditClassification,
    live_pr: Any,
    live_labels: tuple[str, ...],
    config: MergeQueueConfig,
) -> bool:
    if classification.family == "label tamper":
        if classification.detail == "labels-at-open":
            return _has_mq_label(live_labels, config)
        return True
    if classification.detail == "base-ref-change":
        return (
            _has_mq_label(live_labels, config)
            and _live_base_ref(live_pr) != "develop"
        )
    if classification.detail == "draft-conversion":
        return _has_mq_label(live_labels, config) and bool(
            getattr(live_pr, "draft", False)
        )
    if classification.detail == "stale-label-reopen":
        return _has_mq_label(live_labels, config)
    return False


def _eject_live_pr(
    client: Any,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
    pr_state: PRState,
    classification: AuditClassification,
    live_pr: Any,
) -> None:
    client.rest.repos.create_commit_status(
        owner,
        repo,
        pr_state.head_sha,
        state="error",
        context=config.activation_status_context,
        description="audit ejected",
    )
    for label in sorted(pr_state.labels):
        if label.startswith(config.label_prefix):
            _safe_remove_label(client, owner, repo, pr_state.number, label)
    _upsert_status_comment(client, owner, repo, pr_state, classification)
    client.rest.issues.create_comment(
        owner,
        repo,
        pr_state.number,
        body=_render_audit_comment(classification, live_pr, pr_state),
    )


def _build_live_pr_state(
    pr_number: int,
    live_pr: Any,
    live_labels: tuple[str, ...],
    config: MergeQueueConfig,
    now: datetime,
) -> PRState:
    head_sha = str(getattr(getattr(live_pr, "head", None), "sha", ""))
    state_labels = {config.queued_label, config.active_label}
    queues = frozenset(
        label[len(config.label_prefix) :]
        for label in live_labels
        if label.startswith(config.label_prefix) and label not in state_labels
    )
    return PRState(
        number=pr_number,
        head_sha=head_sha,
        labels=frozenset(live_labels),
        queues=queues,
        enqueued_at=now,
        is_validly_active=False,
    )


def _upsert_status_comment(
    client: Any,
    owner: str,
    repo: str,
    pr: PRState,
    classification: AuditClassification,
) -> None:
    cycle_run_url: str | None = None
    server = os.environ.get("GITHUB_SERVER_URL")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and run_id:
        cycle_run_url = f"{server}/{owner}/{repo}/actions/runs/{run_id}"
    reason = f"audit {classification.family}: {classification.detail}"
    ctx = RenderContext(
        author_login=classification.actor_login,
        pr_title="",
        queue_positions=(),
        blockers=(),
        cycle_run_url=cycle_run_url,
        state="ejected",
        eject_reason=reason,
        merged_sha=None,
    )
    body = render_status_body(pr, ctx, datetime.now(tz=UTC))
    comment_id = _find_status_comment_id(client, owner, repo, pr.number)
    if comment_id is None:
        client.rest.issues.create_comment(owner, repo, pr.number, body=body)
    else:
        client.rest.issues.update_comment(owner, repo, comment_id, body=body)


def _render_audit_comment(
    classification: AuditClassification, live_pr: Any, pr: PRState
) -> str:
    label_fragment = ""
    if classification.label_name is not None:
        label_fragment = f", label={classification.label_name}"
    return "\n".join(
        (
            f"## Merge queue audit: {classification.family}",
            "",
            f"@{classification.actor_login} triggered audit eject for "
            f"event={classification.event_name}, action={classification.action}, "
            f"detail={classification.detail}{label_fragment}.",
            "",
            "The merge queue removed all current `mq:*` labels and overwrote "
            f"`{pr.head_sha}` with `merge-queue/active=error`.",
            f"Live PR state at audit time: base={_live_base_ref(live_pr)}, "
            f"draft={bool(getattr(live_pr, 'draft', False))}.",
            "",
            "Re-run `/merge` after fixing the issue to re-enqueue this PR.",
        )
    )


def _safe_remove_label(
    client: Any, owner: str, repo: str, pr_number: int, label: str
) -> None:
    try:
        client.rest.issues.remove_label(owner, repo, pr_number, label)
    except RequestFailed as exc:
        if _status_code(exc) == 404:
            return
        raise


def _status_code(exc: RequestFailed) -> int | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    code = getattr(response, "status_code", None)
    return int(code) if code is not None else None


def _live_label_names(live_pr: Any) -> tuple[str, ...]:
    return tuple(
        str(getattr(label, "name", ""))
        for label in (getattr(live_pr, "labels", []) or [])
    )


def _live_base_ref(live_pr: Any) -> str:
    return str(getattr(getattr(live_pr, "base", None), "ref", ""))


def _payload_label_name(event: dict[str, Any]) -> str | None:
    label = event.get("label") or {}
    if not isinstance(label, dict):
        return None
    name = label.get("name")
    return str(name) if name is not None else None


def _payload_label_names(pull_request: dict[str, Any]) -> tuple[str, ...]:
    labels = pull_request.get("labels") or ()
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = getattr(label, "name", None)
        if name is not None:
            names.append(str(name))
    return tuple(names)


def _has_mq_label(labels: tuple[str, ...], config: MergeQueueConfig) -> bool:
    return any(label.startswith(config.label_prefix) for label in labels)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["main"]
