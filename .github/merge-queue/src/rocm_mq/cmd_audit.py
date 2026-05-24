"""RFC §4.3.1 real-time audit command for merge-queue tamper events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rocm_mq.state import AppIdentity, MergeQueueConfig


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


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint placeholder; cleanup is implemented in the next task."""

    _ = argv
    return 0


__all__ = ["main"]
