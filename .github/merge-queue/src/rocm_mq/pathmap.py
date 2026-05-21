"""
rocm_mq.pathmap — RFC §4.2 path → queue mapping.

Single public function: ``queues_for_paths(paths, config) -> frozenset[str]``.

The function maps file paths changed in a PR to the set of queue names that PR
must enter, implementing the RFC §4.2 membership rule and the asymmetric
provider/integration-tests edge:

  - ``projects/hipdnn/`` changes enter ALL six queues (core + all providers +
    integration-tests), because hipdnn is upstream of everything.
  - ``dnn-providers/integration-tests/`` changes enter the five provider queues
    PLUS integration-tests, but NOT hipdnn — the dependency is decoupled in
    the downstream direction.
  - Individual provider paths enter only their own queue.

Preconditions (caller responsibility):
  - ``config.path_to_queues`` is sorted longest-prefix-first. The function
    uses the *first* matching prefix for each path; a shorter prefix earlier
    in the list would shadow a longer one (wrong-answer bug, not crash).
  - Path strings are already normalized: no ``./`` prefix, no doubled ``//``,
    no trailing ``/`` on directory names (all callers normalize before querying).

The asymmetric edge is implemented purely by the fixture data in
``path_to_queues`` — no special-case code in this module.
"""

from __future__ import annotations

from rocm_mq.state import MergeQueueConfig


def queues_for_paths(
    paths: tuple[str, ...],
    config: MergeQueueConfig,
) -> frozenset[str]:
    """Return the set of queue names this PR enters, per RFC §4.2.

    For each path in *paths*, iterates ``config.path_to_queues`` (a tuple of
    ``(prefix, queue_set)`` pairs, pre-sorted longest-prefix-first by the
    loader).  The first prefix that matches (``path.startswith(prefix)``) wins;
    its ``queue_set`` is unioned into the result and the inner loop breaks so
    that shorter prefixes do not override the longer match.

    Returns ``frozenset()`` when no path matches any opted-in prefix.  The
    empty set is a valid return value — the caller (the handler workflow)
    interprets it as "this PR has no opted-in paths; reject ``/merge``"
    (RFC §4.4).

    Args:
        paths: Normalized file paths changed in the PR (no ``./`` prefix, no
            doubled ``//``).  May be empty.
        config: Validated merge-queue configuration.  ``config.path_to_queues``
            must be pre-sorted longest-prefix-first.

    Returns:
        Frozenset of queue name strings this PR belongs to.
    """
    out: set[str] = set()
    for path in paths:
        for prefix, queues in config.path_to_queues:
            if path.startswith(prefix):
                out.update(queues)
                break  # longest-prefix-first match wins; skip shorter prefixes
    return frozenset(out)
