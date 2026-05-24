"""Tests for the RFC §4.3.1 audit command."""

from __future__ import annotations

import json
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

@pytest.mark.parametrize(
    ("event_name", "payload", "detail", "live_base", "live_draft"),
    [
        (
            "pull_request_target",
            _event("opened", labels=("mq:queued",)),
            "labels-at-open",
            "develop",
            False,
        ),
        (
            "pull_request_target",
            _event("labeled", label_name="mq:queued"),
            "post-open-add",
            "develop",
            False,
        ),
        (
            "pull_request_target",
            _event("unlabeled", label_name="mq:hipdnn"),
            "post-open-remove",
            "develop",
            False,
        ),
        (
            "pull_request_target",
            _event(
                "edited",
                labels=("mq:queued",),
                base_ref="release/6.0",
                changes={"base": {"ref": {"from": "develop"}}},
            ),
            "base-ref-change",
            "release/6.0",
            False,
        ),
        (
            "pull_request_target",
            _event("converted_to_draft", labels=("mq:queued",), draft=True),
            "draft-conversion",
            "develop",
            True,
        ),
        (
            "pull_request_target",
            _event("reopened", labels=("mq:queued",)),
            "stale-label-reopen",
            "develop",
            False,
        ),
    ],
)
def test_main_ejects_from_live_pr_state_and_posts_audit_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    event_name: str,
    payload: dict[str, Any],
    detail: str,
    live_base: str,
    live_draft: bool,
) -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()
    state = FakeRepoState()
    state.prs[42] = FakePR(
        number=42,
        head_sha="live-sha",
        labels={"mq:queued", "mq:hipdnn", "mq:active", "unrelated"},
        base_ref=live_base,
        draft=live_draft,
    )
    fake = FakeGitHub(state)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setattr(cmd_audit, "_build_client", lambda token: fake)
    monkeypatch.setattr(
        cmd_audit, "_load_config", lambda client, owner, repo: config
    )

    rc = cmd_audit.main(["--repo=owner/repo", f"--event-path={event_path}"])

    assert rc == 0
    assert state.prs[42].labels == {"unrelated"}
    status = state.status_store[("live-sha", config.activation_status_context)]
    assert status["state"] == "error"
    assert status["description"] == "audit ejected"

    comments = list(state.comments_store[42].values())
    status_comments = [
        body for body in comments if "<!-- rocm-mq-status -->" in body
    ]
    audit_comments = [
        body for body in comments if "<!-- rocm-mq-status -->" not in body
    ]
    assert len(status_comments) == 1
    assert "Ejected from merge queue" in status_comments[0]
    assert len(audit_comments) == 1
    audit_body = audit_comments[0]
    assert "label tamper" in audit_body or "lifecycle invalidation" in audit_body
    assert detail in audit_body
    assert "@alice" in audit_body
    assert f"action={payload['action']}" in audit_body
    assert f"event={event_name}" in audit_body
    assert "/merge" in audit_body


def test_main_updates_existing_status_comment_and_posts_separate_audit_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()
    state = FakeRepoState(next_comment_id=1001)
    state.prs[42] = FakePR(
        number=42,
        head_sha="live-sha",
        labels={"mq:queued", "mq:hipdnn"},
    )
    state.comments_store[42] = {1000: "old\n<!-- rocm-mq-status -->"}
    fake = FakeGitHub(state)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(_event("labeled", label_name="mq:queued")), encoding="utf-8"
    )

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setattr(cmd_audit, "_build_client", lambda token: fake)
    monkeypatch.setattr(
        cmd_audit, "_load_config", lambda client, owner, repo: config
    )

    assert (
        cmd_audit.main(["--repo=owner/repo", f"--event-path={event_path}"]) == 0
    )
    assert "Ejected from merge queue" in state.comments_store[42][1000]
    assert 1001 in state.comments_store[42]
    assert "post-open-add" in state.comments_store[42][1001]


def test_repeated_delivery_with_already_removed_labels_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()
    state = FakeRepoState()
    state.prs[42] = FakePR(number=42, head_sha="live-sha", labels=set())
    fake = FakeGitHub(state)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(_event("unlabeled", label_name="mq:queued")), encoding="utf-8"
    )

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setattr(cmd_audit, "_build_client", lambda token: fake)
    monkeypatch.setattr(
        cmd_audit, "_load_config", lambda client, owner, repo: config
    )

    rc = cmd_audit.main(["--repo=owner/repo", f"--event-path={event_path}"])

    assert rc == 0
    assert state.prs[42].labels == set()
    assert (
        state.status_store[("live-sha", config.activation_status_context)]["state"]
        == "error"
    )


def test_lifecycle_event_with_no_live_mq_labels_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from rocm_mq import cmd_audit

    config = canonical_merge_queue_config()
    state = FakeRepoState()
    state.prs[42] = FakePR(number=42, head_sha="live-sha", labels=set())
    fake = FakeGitHub(state)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(_event("reopened", labels=())), encoding="utf-8"
    )

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setattr(cmd_audit, "_build_client", lambda token: fake)
    monkeypatch.setattr(
        cmd_audit, "_load_config", lambda client, owner, repo: config
    )

    rc = cmd_audit.main(["--repo=owner/repo", f"--event-path={event_path}"])

    assert rc == 0
    assert state.status_store == {}
    assert state.comments_store == {}
