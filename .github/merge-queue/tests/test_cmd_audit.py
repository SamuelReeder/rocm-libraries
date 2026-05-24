"""Tests for the RFC §4.3.1 audit command."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import CANONICAL_APP, canonical_merge_queue_config
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState


def _sender(
    login: str = "alice",
    actor_type: str = "User",
    actor_id: int = 101,
) -> dict[str, Any]:
    return {"login": login, "type": actor_type, "id": actor_id}


def _canonical_sender() -> dict[str, Any]:
    return {
        "login": "rocm-mq[bot]",
        "type": "Bot",
        "id": CANONICAL_APP.bot_user_id,
    }


def _event(
    action: str,
    *,
    labels: tuple[str, ...] = (),
    sender: dict[str, Any] | None = None,
    label_name: str | None = None,
    base_ref: str = "develop",
    draft: bool = False,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": 42,
            "labels": [{"name": label} for label in labels],
            "head": {"sha": "payload-sha"},
            "base": {"ref": base_ref},
            "draft": draft,
        },
        "sender": sender if sender is not None else _sender(),
    }
    if label_name is not None:
        payload["label"] = {"name": label_name}
    if changes is not None:
        payload["changes"] = changes
    return payload


@pytest.mark.parametrize(
    ("event_name", "payload", "family", "detail", "label_name"),
    [
        (
            "pull_request_target",
            _event("opened", labels=("mq:queued",)),
            "label tamper",
            "labels-at-open",
            None,
        ),
        (
            "pull_request_target",
            _event("labeled", label_name="mq:queued"),
            "label tamper",
            "post-open-add",
            "mq:queued",
        ),
        (
            "pull_request_target",
            _event("unlabeled", label_name="mq:hipdnn"),
            "label tamper",
            "post-open-remove",
            "mq:hipdnn",
        ),
        (
            "pull_request_target",
            _event(
                "edited",
                labels=("mq:queued",),
                base_ref="release/6.0",
                changes={"base": {"ref": {"from": "develop"}}},
            ),
            "lifecycle invalidation",
            "base-ref-change",
            None,
        ),
        (
            "pull_request_target",
            _event("converted_to_draft", labels=("mq:queued",), draft=True),
            "lifecycle invalidation",
            "draft-conversion",
            None,
        ),
        (
            "pull_request_target",
            _event("reopened", labels=("mq:queued",)),
            "lifecycle invalidation",
            "stale-label-reopen",
            None,
        ),
    ],
)
def test_classify_event_rows_have_stable_structured_fields(
    event_name: str,
    payload: dict[str, Any],
    family: str,
    detail: str,
    label_name: str | None,
) -> None:
    from rocm_mq import cmd_audit

    classification = cmd_audit._classify_event(
        event_name, payload, canonical_merge_queue_config()
    )

    assert classification.should_eject is True
    assert classification.family == family
    assert classification.detail == detail
    assert classification.event_name == event_name
    assert classification.action == payload["action"]
    assert classification.pr_number == 42
    assert classification.label_name == label_name
    assert classification.actor_login == "alice"
    assert classification.actor_id == 101


def test_canonical_app_sender_is_the_only_exempt_actor() -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()
    canonical = cmd_audit._classify_event(
        "pull_request_target",
        _event("labeled", label_name="mq:queued", sender=_canonical_sender()),
        config,
    )
    assert canonical.should_eject is False
    assert canonical.detail == "canonical-app-self-trigger"

    ejecting_senders = [
        _sender("alice", "User", 101),
        _sender("rocm-mq[bot]", "User", CANONICAL_APP.bot_user_id),
        _sender("other-app[bot]", "Bot", 55555),
        _sender("github-actions[bot]", "Bot", 15368),
    ]
    for sender in ejecting_senders:
        classification = cmd_audit._classify_event(
            "pull_request_target",
            _event("labeled", label_name="mq:queued", sender=sender),
            config,
        )
        assert classification.should_eject is True, sender
        assert classification.detail == "post-open-add"


def test_irrelevant_label_and_unqueued_lifecycle_events_are_noops() -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()

    irrelevant_label = cmd_audit._classify_event(
        "pull_request_target",
        _event("labeled", label_name="bug"),
        config,
    )
    assert irrelevant_label.should_eject is False
    assert irrelevant_label.family == "none"
    assert irrelevant_label.detail == "irrelevant-label"

    lifecycle_without_mq = cmd_audit._classify_event(
        "pull_request_target",
        _event("reopened", labels=()),
        config,
    )
    assert lifecycle_without_mq.should_eject is False
    assert lifecycle_without_mq.family == "none"
    assert lifecycle_without_mq.detail == "no-current-mq-labels"




def test_fake_pull_request_exposes_base_ref_and_draft_state() -> None:
    state = FakeRepoState()
    state.prs[42] = FakePR(
        number=42,
        head_sha="head-sha",
        base_ref="release/6.0",
        draft=True,
    )
    pr = FakeGitHub(state).rest.pulls.get("owner", "repo", 42).parsed_data

    assert pr.base.ref == "release/6.0"
    assert pr.draft is True
