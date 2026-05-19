"""
tests/gh_fake.py — In-memory GitHub fake for IO-02 / IO-06 contract testing.

Models the four load-bearing semantic behaviors of the real GitHub REST API
that the merge queue depends on:

  1. **(SHA, context) status overwrite** — second ``create_commit_status`` on
     the same ``(sha, context)`` REPLACES the first; ``list_commit_statuses_for_ref``
     yields only the latest.
  2. **Label add idempotency** — ``add_labels`` with an already-present label
     is a no-op (no duplicate entry; no failure).
  3. **``remove_label`` 404 when absent** — removing a label not currently on
     the PR raises a ``RequestFailed`` whose ``.response.status_code == 404``.
     The executor depends on catching this to satisfy idempotent label removal
     (RFC §4.6 idempotency contract).
  4. **``repos.merge`` 204 vs 201** — when the base branch already contains the
     head SHA, the API returns 204 No Content (no merge commit created); on a
     successful merge, returns 201 Created with the new commit SHA in
     ``.parsed_data.sha``.

Design decisions:
- ``FakePR`` and ``FakeRepoState`` are NON-frozen ``@dataclass`` instances —
  the ONLY exception to the frozen-dataclass rule. Test scenarios mutate
  state (add/remove labels, overwrite statuses, advance ``develop_tip``);
  modelling that with frozen instances would force a constant rebuild dance.
- ``FakeGitHub.rest`` exposes the same sub-namespace shape as the real client
  (``repos``, ``issues``, ``pulls``, ``search``, ``checks``, ``apps``, ``users``).
- ``FakeRequestFailed`` constructs a real ``RequestFailed`` via ``__new__`` +
  attribute assignment — matches the pattern in ``test_gh_client.py`` and
  ensures the executor's ``except RequestFailed`` clauses catch fake failures
  identically to real ones.
- Fake lives in ``tests/`` and is never imported from ``src/`` (T-02-02-04
  acceptance: fake leakage into production is structurally impossible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from githubkit.exception import RequestFailed

# ---------------------------------------------------------------------------
# Mutable state model (intentional non-frozen — see module docstring)
# ---------------------------------------------------------------------------


@dataclass
class FakePR:
    """In-memory PR. Mutated by add_labels / remove_label / pulls.merge."""

    number: int
    head_sha: str
    labels: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list)
    merged: bool = False
    merge_commit_sha: str | None = None


@dataclass
class FakeRepoState:
    """In-memory repository state shared by all FakeGitHub method calls."""

    prs: dict[int, FakePR] = field(default_factory=dict)
    label_log: list[tuple[str, str, str]] = field(default_factory=list)
    """Append-only label timeline: tuples of (pr_number_str, event, label_name)."""
    status_store: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    """Maps (head_sha, context) → latest status dict {state, created_at, creator_id}."""
    develop_tip: str = "develop_initial_tip"
    """The current SHA at the head of the develop branch."""
    timeline_lag: set[int] = field(default_factory=set)
    """PRs whose timeline events are artificially delayed (Pitfall 4 simulation)."""
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    """SHA → commit metadata (parents, message) for post-squash readback."""
    next_status_seq: int = 0
    next_comment_id: int = 1000


# ---------------------------------------------------------------------------
# FakeRequestFailed — duck-typed RequestFailed for the executor's except clauses
# ---------------------------------------------------------------------------


def _make_request_failed(status_code: int) -> RequestFailed:
    """Construct a real RequestFailed via __new__ to avoid the httpx.Response dep.

    The executor's ``except RequestFailed as e: if e.response.status_code == 404``
    pattern reads only ``.response.status_code``, so a SimpleNamespace shim is
    sufficient. Matches the pattern used in tests/test_gh_client.py.
    """
    exc = RequestFailed.__new__(RequestFailed)
    exc.response = SimpleNamespace(status_code=status_code)  # type: ignore[assignment]
    return exc


# Alias preserved for the original skeleton's import surface — tests / executor
# code can use this name for clarity; the actual class is githubkit's
# RequestFailed (so isinstance checks work uniformly).
FakeRequestFailed = RequestFailed


# ---------------------------------------------------------------------------
# Helper: SimpleUser-shaped creator stubs
# ---------------------------------------------------------------------------


_CREATOR_APP_LOGIN = "rocm-mq[bot]"
_CREATOR_WORKFLOW_LOGIN = "github-actions[bot]"


def _make_creator(creator_type: str) -> SimpleNamespace:
    """Build a SimpleUser-shaped creator for status entries.

    creator_type:
      "app"      → the merge-queue App (matches CANONICAL_APP bridging path)
      "workflow" → github-actions[bot] (sibling workflow; rejected by is_app_identity)
    """
    if creator_type == "app":
        return SimpleNamespace(login=_CREATOR_APP_LOGIN, type="Bot", id=99999)
    if creator_type == "workflow":
        return SimpleNamespace(login=_CREATOR_WORKFLOW_LOGIN, type="Bot", id=15368)
    raise ValueError(f"unknown creator_type: {creator_type!r}")


def _resp(parsed_data: Any, *, status_code: int = 200) -> SimpleNamespace:
    """Wrap parsed data in a response envelope (mimics githubkit Response)."""
    return SimpleNamespace(parsed_data=parsed_data, status_code=status_code)


# ---------------------------------------------------------------------------
# Sub-namespace implementations
# ---------------------------------------------------------------------------


class _ReposNS:
    def __init__(self, state: FakeRepoState, creator_type: str) -> None:
        self._state = state
        self._creator_type = creator_type

    def create_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        **_: Any,
    ) -> SimpleNamespace:
        """Semantic 1: (sha, context) overwrite. Second write replaces first."""
        self._state.next_status_seq += 1
        self._state.status_store[(sha, context)] = {
            "state": state,
            "context": context,
            "sha": sha,
            "created_at": f"2026-04-22T15:00:{self._state.next_status_seq:02d}Z",
            "creator_type": self._creator_type,
        }
        return _resp(SimpleNamespace(sha=sha, context=context, state=state))

    def list_commit_statuses_for_ref(
        self, owner: str, repo: str, ref: str
    ) -> SimpleNamespace:
        """Return the latest status per (ref, context). Semantic 1 readback."""
        entries = [
            SimpleNamespace(
                context=s["context"],
                state=s["state"],
                created_at=s["created_at"],
                creator=_make_creator(s["creator_type"]),
            )
            for (sha, _ctx), s in self._state.status_store.items()
            if sha == ref
        ]
        return _resp(entries)

    def merge(
        self, owner: str, repo: str, *, base: str, head: str, **_: Any
    ) -> SimpleNamespace:
        """Semantic 4: 204 No Content if already up-to-date; 201 Created otherwise.

        Simplified model: ``head`` SHA is taken as the would-be develop tip
        after merge. If ``head == develop_tip``, the base branch already
        contains it → 204. Otherwise → 201 with a synthesized new SHA.
        """
        if head == self._state.develop_tip:
            # Already up-to-date; the real API returns 204 with no body.
            return SimpleNamespace(status_code=204, parsed_data=None)
        new_sha = f"merge_{base}_{head}"
        self._state.develop_tip = new_sha
        return SimpleNamespace(
            status_code=201,
            parsed_data=SimpleNamespace(sha=new_sha),
        )

    def get_commit(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        """Read a commit by SHA — used for post-squash parent verification."""
        commit_meta = self._state.commits.get(ref)
        if commit_meta is None:
            raise _make_request_failed(404)
        parents = [SimpleNamespace(sha=p) for p in commit_meta.get("parents", [])]
        return _resp(SimpleNamespace(sha=ref, parents=parents))


class _IssuesNS:
    def __init__(self, state: FakeRepoState) -> None:
        self._state = state

    def add_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        data: Any = None,
        **kwargs: Any,
    ) -> SimpleNamespace:
        """Semantic 2: label idempotency — set semantics, no duplicates."""
        names = self._extract_label_names(data, kwargs)
        pr = self._require_pr(issue_number)
        for name in names:
            if name not in pr.labels:
                pr.labels.add(name)
                self._state.label_log.append((str(issue_number), "labeled", name))
            # else: idempotent no-op
        return _resp([SimpleNamespace(name=n) for n in sorted(pr.labels)])

    def remove_label(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        name: str,
    ) -> SimpleNamespace:
        """Semantic 3: 404 when label not on PR; otherwise remove + log."""
        pr = self._require_pr(issue_number)
        if name not in pr.labels:
            raise _make_request_failed(404)
        pr.labels.discard(name)
        self._state.label_log.append((str(issue_number), "unlabeled", name))
        return _resp([SimpleNamespace(name=n) for n in sorted(pr.labels)])

    def list_events_for_timeline(
        self, owner: str, repo: str, issue_number: int
    ) -> SimpleNamespace:
        """Return labeled/unlabeled timeline events; honours timeline_lag."""
        if issue_number in self._state.timeline_lag:
            return _resp([])
        events = []
        for pr_num_str, event, name in self._state.label_log:
            if pr_num_str != str(issue_number):
                continue
            events.append(
                SimpleNamespace(
                    event=event,
                    actor=_make_creator("app"),
                    label=SimpleNamespace(name=name),
                    created_at="2026-04-22T15:23:45Z",
                )
            )
        return _resp(events)

    def list_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        **_: Any,
    ) -> SimpleNamespace:
        # The fake does not model comment storage; tests that need to drive
        # specific comment lists monkeypatch this method. Accepts arbitrary
        # kwargs (per_page, page) so the executor's paginated _find_status_
        # comment_id implementation (WR-03) drives through here without a
        # signature mismatch.
        return _resp([])

    def create_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        comment_id = self._state.next_comment_id
        self._state.next_comment_id += 1
        return _resp(SimpleNamespace(id=comment_id, body=body))

    def update_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        *,
        body: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        return _resp(SimpleNamespace(id=comment_id, body=body))

    # -- helpers --

    def _require_pr(self, pr_number: int) -> FakePR:
        pr = self._state.prs.get(pr_number)
        if pr is None:
            raise _make_request_failed(404)
        return pr

    @staticmethod
    def _extract_label_names(data: Any, kwargs: dict[str, Any]) -> list[str]:
        """Accept the labels argument in any of the shapes githubkit allows.

        Real githubkit's ``add_labels`` accepts either a positional/data dict
        ``{"labels": [...]}`` or a kwarg list. We accept both for test ergonomics.
        """
        if data is not None:
            if isinstance(data, dict) and "labels" in data:
                return list(data["labels"])
            if isinstance(data, list):
                return list(data)
        if "labels" in kwargs:
            return list(kwargs["labels"])
        return []


class _PullsNS:
    def __init__(self, state: FakeRepoState) -> None:
        self._state = state

    def get(self, owner: str, repo: str, pull_number: int) -> SimpleNamespace:
        pr = self._state.prs.get(pull_number)
        if pr is None:
            raise _make_request_failed(404)
        return _resp(
            SimpleNamespace(
                number=pr.number,
                head=SimpleNamespace(sha=pr.head_sha),
                labels=[SimpleNamespace(name=n) for n in sorted(pr.labels)],
            )
        )

    def list_files(
        self, owner: str, repo: str, pull_number: int
    ) -> SimpleNamespace:
        pr = self._state.prs.get(pull_number)
        if pr is None:
            raise _make_request_failed(404)
        return _resp([SimpleNamespace(filename=f) for f in pr.files])

    def merge(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        merge_method: str = "squash",
        **_: Any,
    ) -> SimpleNamespace:
        pr = self._state.prs.get(pull_number)
        if pr is None:
            raise _make_request_failed(404)
        if pr.merged:
            raise _make_request_failed(405)
        squash_sha = f"squash_{pull_number}_{pr.head_sha[:8]}"
        prior_tip = self._state.develop_tip
        self._state.commits[squash_sha] = {
            "parents": [prior_tip],
            "message": f"Squash merge of #{pull_number}",
        }
        self._state.develop_tip = squash_sha
        pr.merged = True
        pr.merge_commit_sha = squash_sha
        return _resp(
            SimpleNamespace(
                sha=squash_sha,
                merged=True,
                message=f"PR #{pull_number} squash-merged",
            )
        )


class _SearchNS:
    def __init__(self, state: FakeRepoState) -> None:
        self._state = state

    def issues_and_pull_requests(self, *, q: str, **_: Any) -> SimpleNamespace:
        """Parse ``label:<name>`` from q and return matching PRs."""
        wanted_labels = self._parse_labels(q)
        items: list[SimpleNamespace] = []
        for pr in self._state.prs.values():
            if not wanted_labels:
                items.append(SimpleNamespace(number=pr.number))
                continue
            if wanted_labels & pr.labels:
                items.append(SimpleNamespace(number=pr.number))
        return _resp(
            SimpleNamespace(incomplete_results=False, items=items, total_count=len(items))
        )

    @staticmethod
    def _parse_labels(q: str) -> set[str]:
        """Extract label values from a GitHub search query string.

        Real GitHub search accepts ``label:"mq:hipdnn"`` (quoted form is the
        documented-safe shape when the value contains characters the query
        parser handles specially, such as colons — WR-02). Strip surrounding
        double quotes so the fake's match behaves the same way the real API
        does. Tokens without quotes are passed through unchanged for
        backwards compatibility with tests that emit the unquoted form.
        """
        labels: set[str] = set()
        for token in q.split():
            if not token.startswith("label:"):
                continue
            value = token[len("label:") :]
            # Strip a single matched pair of surrounding double quotes.
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            labels.add(value)
        return labels


class _ChecksNS:
    def __init__(self, state: FakeRepoState) -> None:
        self._state = state

    def list_for_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        # Fake tests do not exercise check-run detail; return empty.
        return _resp(SimpleNamespace(total_count=0, check_runs=[]))


class _AppsNS:
    def get_authenticated(self) -> SimpleNamespace:
        return _resp(SimpleNamespace(id=12345, slug="rocm-mq"))


class _UsersNS:
    def get_by_username(self, username: str) -> SimpleNamespace:
        # Return a stable id that matches CANONICAL_APP.bot_user_id when
        # username == "rocm-mq[bot]"; otherwise a generic id.
        if username == _CREATOR_APP_LOGIN:
            return _resp(SimpleNamespace(id=99999, login=username))
        return _resp(SimpleNamespace(id=42, login=username))


class _RestNS:
    def __init__(self, state: FakeRepoState, creator_type: str) -> None:
        self.repos = _ReposNS(state, creator_type)
        self.issues = _IssuesNS(state)
        self.pulls = _PullsNS(state)
        self.search = _SearchNS(state)
        self.checks = _ChecksNS(state)
        self.apps = _AppsNS()
        self.users = _UsersNS()


# ---------------------------------------------------------------------------
# Top-level fake client
# ---------------------------------------------------------------------------


class FakeGitHub:
    """In-memory stand-in for ``GitHubClient`` exposing ``.rest.*`` namespaces.

    Constructor:
        FakeGitHub(state, creator_type="app")

    ``creator_type`` controls the identity tagged on commit statuses:
      - "app"      → mimics the merge-queue App (matches is_app_identity)
      - "workflow" → mimics github-actions[bot] (sibling workflow; rejected)
    """

    def __init__(self, state: FakeRepoState, creator_type: str = "app") -> None:
        self._state = state
        self._creator_type = creator_type
        self.rest = _RestNS(state, creator_type)

    @property
    def state(self) -> FakeRepoState:
        """Expose underlying state for test inspection / mutation."""
        return self._state
