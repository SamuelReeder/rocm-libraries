# Federated Merge Queues for rocm-libraries

- Contributors: Samuel Reeder
- **Status**: First draft

## Contents

- [1. Executive Summary](#1-executive-summary)
- [2. Problem Statement](#2-problem-statement)
- [3. Goals, Non-Goals, and Prerequisites](#3-goals-non-goals-and-prerequisites)
- [4. Design](#4-design)
  - [4.1 Queues](#41-queues)
  - [4.2 Path → Queue mapping](#42-path--queue-mapping)
  - [4.3 Data model](#43-data-model)
  - [4.4 Permissions](#44-permissions)
  - [4.5 PR lifecycle](#45-pr-lifecycle)
  - [4.6 Per-cycle algorithm](#46-per-cycle-algorithm)
  - [4.7 Cadence and concurrency](#47-cadence-and-concurrency)
  - [4.8 Opt-in / extension](#48-opt-in--extension)
  - [4.9 Implementation constraints](#49-implementation-constraints)
- [5. Open-Source Contributor Policy](#5-open-source-contributor-policy)
- [6. Validation](#6-validation)
- [7. Rollout](#7-rollout)
- [8. Risks](#8-risks)
- [9. Potential future enhancements](#9-potential-future-enhancements)

## 1. Executive Summary

This RFC proposes a custom, in-repo **federated** merge queue for rocm-libraries on `develop`: per-component FIFO queues that interconnect along the dependency graph, rather than a single global queue serializing everything. A PR enters the queue of every component its changes could affect, and merges only when it is at the head of all of them. Authors trigger the queue with a `/merge` PR comment; an in-repo workflow squash-merges PRs as they reach the front.

Initial scope is the hipDNN ecosystem: `hipdnn` core, four providers (`miopen-provider`, `hipblaslt-provider`, `hip-kernel-provider`, `fusilli-provider`), and `integration-tests`. The queue is opt-in (other rocm-libraries subprojects are unaffected) and starts optional, with no branch-protection changes; any project can join later by adding centralized config entries.

## 2. Problem Statement

Multiple PRs may merge into rocm-libraries without their cumulative state ever being tested. Because nothing serializes these merges, PRs that touch coupled code frequently experience semantic collisions: they may pass CI in isolation, but immediately break develop when merged concurrently.

The hipDNN ecosystem is especially susceptible. The various hipDNN-related components in rocm-libraries can be thought of as a Directed Acyclic Graph (DAG). To guarantee merge safety, downstream components must strictly serialize behind upstream changes. Without an automated system that enforces this serialization safely by default, we risk constant mainline regressions while forcing contributors to manually orchestrate their merges.

We desire an automated system that serializes with concurrent queues, is cheap to opt into, and stays out of contributors' way.

## 3. Goals, Non-Goals, and Prerequisites

### Goals
- One FIFO queue per opted-in component. (Initial scope is the hipDNN ecosystem; see [§4.1](#41-queues).)
- Cross-blocking that follows dependencies (see [§4.2](#42-path--queue-mapping)). A PR is blocked by every PR ahead of it in any queue it belongs to.
- Opt-in per project. Subprojects outside the hipDNN ecosystem are unaffected unless they opt in.
- `develop` is the only managed target branch.
- Start optional; provide a clear path to "required" via branch protection.
- External contributors can use the queue with the same safeguards as anyone else.

### Non-Goals
- Replacing existing CI workflows.
- Batched merges (`batch_size > 1`).
- Release or feature branches — `develop` only.
- Priority labels to skip to the front of the queue — potentially future work.

### Prerequisites

Two external preconditions the queue depends on:

- **`develop` branch protection enforces required reviewer approvals and required CI checks.** Per-component reviewer ownership is already declared in [`.github/CODEOWNERS`](https://github.com/ROCm/rocm-libraries/blob/develop/.github/CODEOWNERS); the required check is `TheRock CI Summary` ([`.github/workflows/therock-ci.yml`](https://github.com/ROCm/rocm-libraries/blob/develop/.github/workflows/therock-ci.yml)). The queue serializes; it does not authorize or bypass — see [§5](#5-open-source-contributor-policy) for why this is sufficient even for external contributors.
- **The processor uses a GitHub App installation token** (a separate identity from `GITHUB_TOKEN`) for four operations: pushing the `develop`-merge to the PR branch, squash-merging the PR to `develop`, creating/overwriting the `merge-queue/active` activation status (see [§4.3](#43-data-model)), and applying/removing `mq:*` labels. The first two need to trigger downstream workflows — `pull_request: synchronize` for the develop-merge, and `push: branches: [develop]` post-submit CI for the squash — and `GITHUB_TOKEN`-driven writes do not re-trigger workflows ([GitHub docs](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication#using-the-github_token-in-a-workflow)). The activation status and label writes both go through the App so the audit's exemption can slug-pin to a single trusted identity rather than to the shared `github-actions[bot]` that every workflow on the repo authenticates as — primarily to catch accidental `mq:*` writes from sibling workflows (a poorly-named auto-labeller, a third-party action) and to keep the discipline in place for the tightening sketched in [§9](#9-potential-future-enhancements) (see [§4.3.1](#431-tamper-resistance)).
- **The App's private key is scoped to a GitHub Environment with `develop`-only deployment branches.** The key is stored in a repo-level Environment (e.g. `mq-secrets`) with "Deployment branches and tags" set to `develop` only. Every legitimate queue trigger runs from `develop` by GitHub Actions' execution model — `schedule:` only fires on the default branch, and `pull_request_target` and `issue_comment` run from the base/default branch, not the PR head — so `develop`-only scoping aligns the secret's reachability with the actual execution surface. Workflows running from any other ref fail secret resolution and cannot mint a token. The processor mints a short-lived installation token from the key at workflow start (1-hour TTL, [GitHub docs](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)); only the ephemeral token authenticates API calls. Rotation policy applies to the private key (see [§8](#8-risks)). See [§4.9](#49-implementation-constraints) for the full token split.

## 4. Design

### 4.1 Queues

Each opted-in component gets one FIFO queue. The initial scope is the hipDNN ecosystem, with six queues:

- `hipdnn`
- `miopen-provider`
- `hipblaslt-provider`
- `hip-kernel-provider`
- `fusilli-provider`
- `integration-tests`

Other rocm-libraries subprojects opt in by adding entries to the centralized config (see [§4.8](#48-opt-in--extension)); they have no queue otherwise.

Each PR enters zero or more queues based on the paths it touches. A PR merges only when at the head of **every** queue it belongs to.

### 4.2 Path → Queue mapping

**Membership rule.** A PR enters its own queue plus the queue of every downstream component its changes could break.

**Dependency graph.** Providers depend on both `hipdnn` core (through hipDNN's plugin interface) and `integration-tests` (each provider's CI runs the integration suite). Integration-tests in turn depend on hipDNN's public headers:

```
hipdnn (core) ──► integration-tests ──► providers (×4)
       │                                       ▲
       └───────────────────────────────────────┘
```

The relationship between providers and integration-tests is asymmetric:

- *Provider → integration-tests is decoupled.* Provider changes can only affect integration-tests through hipDNN's stable plugin contract, so even coupled provider changes are unlikely to break the integration suite — though a contract violation could still cause a regression. Provider PRs therefore don't enter the integration-tests queue.
- *Integration-tests → providers is not decoupled.* A change to integration-tests directly changes what every provider's CI must satisfy. Integration-test PRs therefore enter every provider queue.

Paths outside this graph (other rocm-libraries projects) are not opted in and enter no queues.

#### Membership at a glance

```
                                                       QUEUES
                    ┌────────┬──────────┬───────────┬────────────┬──────────┬─────────────┐
                    │        │  miopen  │ hipblaslt │ hip-kernel │ fusilli  │ integration │
PR touches          │ hipdnn │ provider │ provider  │  provider  │ provider │    tests    │
────────────────────┼────────┼──────────┼───────────┼────────────┼──────────┼─────────────┤
hipdnn core         │   ●    │    ●     │     ●     │     ●      │    ●     │      ●      │
miopen-provider     │   ·    │    ●     │     ·     │     ·      │    ·     │      ·      │
hipblaslt-provider  │   ·    │    ·     │     ●     │     ·      │    ·     │      ·      │
hip-kernel-provider │   ·    │    ·     │     ·     │     ●      │    ·     │      ·      │
fusilli-provider    │   ·    │    ·     │     ·     │     ·      │    ●     │      ·      │
integration-tests   │   ·    │    ●     │     ●     │     ●      │    ●     │      ●      │
────────────────────┴────────┴──────────┴───────────┴────────────┴──────────┴─────────────┘
   ● = PR enters this queue     · = PR does not enter this queue
```

Three patterns follow:

- The `hipdnn core` row is fully marked — core sits upstream of every component and could break any of them.
- The `integration-tests` row is marked in every provider column — provider CI runs the integration suite, so an integration-tests change can break any provider. The `hipdnn` column is unmarked because `hipdnn` doesn't depend on integration-tests.
- Each provider row has a single mark — providers are leaves of the dependency graph, with nothing downstream of them. The plugin contract keeps integration-tests insulated from provider changes.

A PR editing both `projects/hipdnn/api/foo.h` and `dnn-providers/miopen-provider/src/bar.cpp` enters all six queues — core's row is a superset of every provider's row.

#### Worked example: five PRs across four time-steps

Four PRs are enqueued at T₀ — A (core), B (miopen-provider), C (hipblaslt-provider), D (integration-tests). A second core PR, E, is enqueued mid-flight at T₁ to demonstrate how core serializes behind in-flight downstream work. Heads shown leftmost; `→` is "behind". Each step represents one full activation cycle — the head PR is activated (`mq:queued` → `mq:active`, `develop` merged in), CI runs, and the result is evaluated next cycle. T₀→T₃ compresses these for readability; in wall-clock terms each step is at minimum one CI cycle.

```
T₀ — A, B, C, D enqueued

  hipdnn              │ A
  miopen-provider     │ A → B           → D
  hipblaslt-provider  │ A      → C      → D
  hip-kernel-provider │ A               → D
  fusilli-provider    │ A               → D
  integration-tests   │ A               → D

  A is at the head of every queue → A merges next.

T₁ — A merged; E (a second core PR) enqueued

  hipdnn              │ E
  miopen-provider     │ B → D → E
  hipblaslt-provider  │ C → D → E
  hip-kernel-provider │ D → E
  fusilli-provider    │ D → E
  integration-tests   │ D → E

  B and C each sit at the head of their only queue → they merge in parallel.
  D is at the head of {hip-kernel-provider, fusilli-provider, integration-tests}
  but blocked by B and C in the other two provider queues.
  E is alone at the head of hipdnn but blocked behind D in all 5 other queues.

T₂ — B and C merged

  hipdnn              │ E
  miopen-provider     │ D → E
  hipblaslt-provider  │ D → E
  hip-kernel-provider │ D → E
  fusilli-provider    │ D → E
  integration-tests   │ D → E

  D is at the head of every queue it belongs to → D merges.
  E remains blocked behind D in 5 of its 6 queues.

T₃ — D merged

  hipdnn              │ E
  miopen-provider     │ E
  hipblaslt-provider  │ E
  hip-kernel-provider │ E
  fusilli-provider    │ E
  integration-tests   │ E

  E is now at the head of every queue → E merges.
```

Two takeaways:

- **B and C merge in parallel; D is held up behind them.** Distinct provider PRs share no queues with each other (no double-serialization through integration-tests), but an integration-tests PR crosses every provider queue. The integration suite is shared infrastructure that providers depend on, so changes to it must serialize against any in-flight provider work.
- **E (core) joins the tail of every queue at T₁ and merges last, after B/C/D have all cleared.** Core is never allowed to overtake an in-flight downstream PR — even though E sits alone at the head of `hipdnn` from T₁ onward, it has to wait through three full CI cycles (B/C, then D, then itself) before merging.

### 4.3 Data model

Queue state lives in the following PR-attached primitives, each chosen so that its mutability is either constrained by GitHub's own access controls or explicitly accounted for by the tamper-resistance design (see [§4.3.1](#431-tamper-resistance)):

- **Membership labels.** One `mq:<queue>` label per queue the PR belongs to. Set at enqueue time; immutable so long as the PR remains in the queue.
- **State labels.** Either `mq:queued` (waiting at some position in one or more queues) or `mq:active` (at the head of all its queues; CI cycle in progress).
- **Issue timeline.** GitHub records every label application as a timeline event with a timestamp. The processor reads it once per cycle for the FIFO sort key — the timestamp of the most recent `mq:queued` event applied by the App identity (slug-pinned per [§4.3.1](#431-tamper-resistance)). If a recently-enqueued PR's timeline event is not yet visible to the processor's same-cycle read (timeline events are eventually consistent), the PR is deferred to the next cycle rather than treated as missing — its position is bounded by one cycle of latency, well within FIFO tolerance.
- **Activation marker.** A `merge-queue/active` commit status, created by the bot on the head SHA when the PR transitions to `mq:active`.
  - *Binding check.* "Does the *current* head SHA carry a `merge-queue/active` status created by the App identity (see [§4.9](#49-implementation-constraints))?" Force-pushes and new commits produce a new head SHA which lacks the App's status, naturally invalidating the activation.
  - *State = `success`.* Posted with a description like `"activation binding for cycle CI; not for use as a required check"`. The binding check looks at presence + creator, never state — `success` is chosen so the status cannot accidentally block branch protection if it were ever added to the required-check set.
  - *Creator filter.* GitHub does not scope status creation by context: anyone with `statuses: write` can post a status with the same name. The lookup ignores statuses from other principals — a competing `merge-queue/active` posted by a maintainer PAT, an external app, or any sibling workflow using `GITHUB_TOKEN` (which authenticates as `github-actions[bot]`, not as the App) does not satisfy the activation check. The App's private key is Environment-scoped to `develop`-only refs ([§3](#3-goals-non-goals-and-prerequisites)), so workflows on PR branches cannot mint the App token to forge it.
  - *On eject.* The processor (using the same App identity) overwrites the status with `error` state (description: `"ejected"`) for human readability. Commit statuses can't be deleted, only overwritten on the same `(SHA, context)` pair; the overwrite is purely cosmetic since the headship check is gated by the `mq:active` *label*, not by status state.
  - *Distinct from `merge-queue/managed`* (§4.5): `managed` enforces queue routing on `develop`; `active` binds activation to a specific commit.
- **Status comment.** A single bot-posted comment that renders the human-readable status block. A hidden `<!-- rocm-mq-status -->` marker lets the processor find and edit its own comment in place rather than posting a fresh one on every state change. The comment is a UI surface only; the processor reads no state from it.

The processor reconstructs the full queue picture on every cycle by issuing one label-filtered API search per queue. A PR labelled `mq:hipdnn` *and* `mq:miopen-provider` is returned by both searches and appears in both queues' member lists.

#### Status comment format

The comment carries the marker and a human-readable body:

```html
<!-- rocm-mq-status -->
```

Derived state (queue position, CI status, eject reason) is recomputed each cycle from labels + the live queue picture and rendered into the visible body. Example queued-state body for a miopen-provider PR:

```markdown
## ⏳ Queued for merge

Enqueued by @samuel-reeder at 2026-04-22 15:23 UTC.

This PR touches `dnn-providers/miopen-provider/**`, entering 1 queue:

| Queue             | Position | Blocked by |
| ----------------- | -------- | ---------- |
| `miopen-provider` | 2 of 3   | #4521      |

Comment `/dequeue` to leave the queue. Processor runs every 3 minutes.

_Updated 2026-04-22 15:26 UTC._
```

The active, ejected, and merged states swap the heading and body — active shows required-check status; ejected names the failing check and asks the author to re-enqueue with `/merge`; merged shows the squash SHA. The processor only edits when something actually changed (state, position, or check status), keeping edit history meaningful.

### 4.3.1 Tamper resistance

Labels are the only canonical state surface mutable by non-bot actors (any user with `triage` repo permission can add or remove them). The issue timeline is append-only and not editable through any API; the `merge-queue/active` commit status is scoped by GitHub to the creating principal, so other apps and users cannot overwrite it; and the status comment is not authoritative — it's a UI surface the bot regenerates as needed. Every tampering vector maps to one of three outcomes:

- *Cannot tamper* — GitHub access controls prevent the action (commit-status overwrites by other principals).
- *No-op* — the surface is non-authoritative; the bot regenerates or re-posts as needed (any edit, deletion, or duplication of the status comment).
- *Detect → eject* — the bot observes the tamper and clears the PR from all queues with an explanatory comment (any external `mq:*` label change).

No tamper produces an unsafe merge.

This protects against (a) accidental edits by repo maintainers, (b) other automation that fires on `mq:*` labels by mistake, and (c) a single non-bot account misusing repo permissions. It does *not* defend against a compromised workflow runner — at that point the bot's auth is the attacker's auth, and the workflow itself can do anything the queue can (acknowledged in [§8](#8-risks)).

#### Tamper matrix

| Tamper vector                                               | Who can do it                  | Response                                                                                                       |
| ----------------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| Add any `mq:*` label                                        | Triage+                       | **Eject.** Real-time audit fires; clears all `mq:*` labels, removes activation status, posts comment.          |
| Remove any `mq:*` label                                     | Triage+                       | **Eject.** Same path — clear remaining `mq:*` labels, post comment.                                            |
| Edit, delete, or plant a duplicate of the status comment    | Comment author / Maintain+ / Sibling workflow | **No-op for correctness; possible UI degradation.** The comment is a UI surface, so no merge decision is affected. If the bot's marker-lookup picks a non-bot comment (sibling workflow planting `<!-- rocm-mq-status -->`, or a maintainer crafting one), status updates may land on the wrong comment until labels clear. Accepted as UI-only degradation. |
| Plant a competing `merge-queue/active` status               | Anyone with `statuses: write` | **No-op.** The processor's lookup filters by creator = the App identity (see [§4.3](#43-data-model), [§4.9](#49-implementation-constraints)). Maintainer PATs, external apps, and sibling workflows using `GITHUB_TOKEN` (which authenticates as `github-actions[bot]`, not as the App) cannot satisfy the filter. |
| Force-push / new commit on the PR branch                    | PR author                     | **Eject (next cycle).** New head SHA has no `merge-queue/active` status — covered in [§4.5](#45-pr-lifecycle). |
| Change PR base branch away from `develop`                   | PR author / Maintain+         | **Eject.** Handler subscribes to `pull_request_target: [edited]`; detects `base.ref` change. Without this, the squash would silently target a non-`develop` branch. |
| Convert PR to draft                                         | PR author / Maintain+         | **Eject.** Required checks may pause on drafts, stranding `mq:active` indefinitely. Handler subscribes to `pull_request_target: [converted_to_draft]`. |
| Close-then-reopen with `mq:*` labels still applied          | PR author                     | **Eject; require fresh `/merge`.** Stale labels carry an `enqueued_at` from the prior lifecycle. The audit's `reopened` trigger catches this. |
| Manually invoke `/merge` without eligibility                | Anyone who can comment        | **Rejected at handler.** Eligibility check in [§4.4](#44-permissions).                                         |
| Concurrent `/merge` from multiple commenters                | Anyone                        | **Idempotent.** Handler short-circuits if PR already carries `mq:queued` or `mq:active`.                       |
| Spoof the bot identity (compromised token / app key)        | Workflow runner               | **Out of scope.** Compromised runner has the bot's authority; documented as a [§8](#8-risks) risk.             |

The matrix walks single-action tampers; combined sequences within a single window (e.g., a triage user removing one `mq:*` label and re-adding another in quick succession) are not separately enumerated and rely on the eject-on-detection path being hit by *any* one of the constituent actions.

#### Real-time tamper audit

The audit is implemented as a second job inside `mq-handler.yml` — the same workflow that handles `/merge` and `/dequeue`. Collapsing both into one file keeps every event-reactive queue-state mutator in one place. Both jobs mint the App installation token at workflow start: the command job to apply/remove `mq:*` labels, and the audit job for the same purpose on eject. The handler workflow declares two triggers: `issue_comment: [created]` for commands, and `pull_request_target: [labeled, unlabeled, opened, reopened, edited, converted_to_draft]` for the audit. Jobs dispatch on `github.event_name`. The audit job is gated to label events whose `label.name` starts with `mq:`, plus base-ref changes (from `[edited]`) and draft conversions (from `[converted_to_draft]`) on PRs carrying any `mq:*` label. If `event.sender` matches the App identity (slug-pinned per the identity-check note below, with `user.type == "Bot"`), the job is a no-op. Otherwise it clears all `mq:*` labels on the PR, overwrites the `merge-queue/active` commit status (if present) with `error: "ejected"` per [§4.3](#43-data-model), and posts a comment explaining the eject. The comment text distinguishes labels-at-open ("`mq:*` labels were applied at PR creation time, which the queue does not allow — use `/merge` to enqueue cleanly") from post-open tampering ("user @X applied/removed `mq:*` labels manually; queue state cleared. Re-enqueue with `/merge`"). Labels are repo-scoped, so the at-open case is always an intentional API-time application (e.g. `gh pr create --label`), never inheritance from a template or fork.

**Why `pull_request_target` is safe here.** The standard `pull_request_target` risk (running untrusted PR code with the upstream's full token) requires checking out PR code, which the audit never does — it reads only event-payload fields and calls GitHub APIs. This is the same pattern the existing `.github/workflows/labeler.yml` uses for fork-PR labelling. The audit's trust scope matches the cron processor, which already exercises full upstream authority on fork PRs (squash-merge, `develop`-merge, label and status writes), so this adds no new privilege boundary.

Required hygiene for `mq-handler.yml`:

- Pass event-payload strings via env vars rather than `${{ }}`-interpolating into shell.
- Declare a minimal `GITHUB_TOKEN` `permissions:` block: `issues: write` (for comments), `statuses: write` (for the `merge-queue/managed` post), `contents: read`. Label writes go through the App token, not `GITHUB_TOKEN`, so neither `pull-requests: write` nor a label-scoped `issues: write` is needed for that path.
- Set `concurrency: mq-handler-${{ github.event.pull_request.number }}` to serialize per-PR.

**The audit is the primary mechanism for label tampering.** Once an `mq:*` label is removed, the PR no longer appears in `search_prs(label:mq:<queue>)`, so the processor's discovery loop cannot see removed-label PRs — the real-time event is the natural place to catch a removal. For *additions*, the audit fires before the processor's next cycle. v1 doesn't add a separate cycle-time tamper check: the labeller and the processor share the same workflow infrastructure, so any failure mode that disables one tends to degrade the other.

A defence-in-depth option using the timeline as a second-layer check is sketched in [§8](#8-risks); not implemented in v1.

#### Activation-status verification

Independently of the labeller, the processor performs one verification every cycle as part of the normal headship check: for every PR labelled `mq:active`, the current head SHA must carry the `merge-queue/active` commit status whose creator is the App identity (see [§4.3](#43-data-model)). This catches force-pushes between activation and evaluation. It isn't only a correctness check — because the creator is the App rather than `github-actions[bot]`, no other workflow in the repo can forge a status that satisfies it (see [§4.3](#43-data-model)'s creator-filter discussion).

#### Identity-check robustness

The merge queue's GitHub App is the one identity whose actions the audit's self-trigger exemption and the activation-status creator filter trust; it must be pinned by slug (or the App ID, which is immutable), never by `login`/`type` alone. The check verifies `user.type == "Bot"` **and** pins the App slug, so a coincidentally-named user account, a fork of the GitHub Actions bot, or a separately-installed App with similar attribution cannot impersonate the identity — `login` + `type` is satisfied by every Actions-style install on the repo, and slug-pinning is the only thing that disambiguates them. Combined with [§3](#3-goals-non-goals-and-prerequisites) Environment scoping (App key only mintable from `develop` refs), no workflow on a non-`develop` ref can mint the App token to forge a label write or activation-status post. This is a must, not an ideal: every other tamper defence rests on it.

The corresponding eligibility check for `/merge` lives in [§4.4](#44-permissions); see there for why `author_association` from the issue-comment payload is not trustworthy.

`mq:*` is a reserved label namespace; CONTRIBUTING will document that maintainers should not apply or remove these labels by hand.

### 4.4 Permissions

- `/merge`: PR author **or** any user with write/maintain/admin on the repo. The PR-author allowance is what makes the queue usable for external contributors on their own PRs (see [§5](#5-open-source-contributor-policy)). The handler verifies write-access by calling `repos.getCollaboratorPermissionLevel` live at the moment the command is issued — *not* by trusting `author_association` from the issue-comment payload, which can be stale or report `CONTRIBUTOR`/`NONE` for users who do hold write access. This live check is load-bearing because `triage` permission is broader than label management — it also lets a user dismiss reviews and re-request changes — so the simpler "did a triage user comment?" shortcut would over-grant.
- `/dequeue`: PR author or any write-access user (verified the same way).
- **`/merge` on a PR that touches no opted-in component path is rejected at the handler.** With an empty queue set, applying labels would still satisfy a vacuous head-of-all-queues check and produce an instant squash. The handler computes the queue set first and posts a comment ("This PR doesn't touch any opted-in component path; merge it manually after CI passes") instead of enqueuing.
- The squash-merge is performed via GitHub's merge API by the App identity (see [§4.9](#49-implementation-constraints)). Branch-protection rules on `develop` apply unchanged (see [§5](#5-open-source-contributor-policy)).

### 4.5 PR lifecycle

A PR moves through three observable states. Every transition is captured in the diagram below:

```
              /merge
   (none) ──────────────► mq:queued ─── external mq:* edit ──► eject
                             │
              at head of all queues
                             ▼
                         mq:active ─── CI green ────► squash-merge
                          │  ▲
                          │  └── CI pending (next cycle)
                          │
                          ├── CI red ────────────────► eject
                          ├── merge conflict ────────► eject
                          ├── HEAD changed ──────────► eject
                          └── external mq:* edit ────► eject
```

Solid arrows are processor-driven (next cycle). The `external mq:* edit → eject` arrows are handler-driven (real-time, on `pull_request_target: [labeled, unlabeled, edited, converted_to_draft]`).

Two GitHub Actions workflows drive these transitions:

1. **Command handler + tamper audit** *(event-driven, `mq-handler.yml`)*. Two jobs in one workflow file, dispatched on `github.event_name`:
   - On `issue_comment: [created]` matching `/merge`: validates eligibility (see [§4.4](#44-permissions)), checks the PR has at least one approving review, confirms maintainer-edits is enabled for fork PRs (see [§5](#5-open-source-contributor-policy)), rejects if any required check on the current head SHA is in `failure` or `error` state (a known-bad PR would otherwise activate, fail, eject, and consume a full activation cycle blocking other PRs in its queues; pending or not-yet-reported checks are allowed since they'll run as part of the activation merge), computes the queue set from the PR's changed paths via the rule in [§4.2](#42-path--queue-mapping), applies `mq:queued` and one `mq:<queue>` label per queue, and posts the status comment (see [§4.3](#43-data-model)). PRs with an empty queue set are rejected with a comment instead of enqueued (see [§4.4](#44-permissions)). On `/dequeue`, removes all `mq:*` labels. This is the only workflow that assigns membership labels. The handler is idempotent — if the PR already carries `mq:queued` or `mq:active`, a second `/merge` is acknowledged as a no-op so concurrent invocations don't post duplicate status comments.
   - On `pull_request_target: [labeled, unlabeled, opened, reopened, edited, converted_to_draft]` for `mq:*` labels (and base-ref / draft changes on PRs already carrying `mq:*`): runs the real-time tamper audit (see [§4.3.1](#431-tamper-resistance)) — eject if the actor is not the App identity (slug-pinned per [§4.3.1](#431-tamper-resistance)).
2. **Processor** *(scheduled, every 3 minutes, `mq-processor.yml`)*. Activates the head PR of each queue (`mq:queued` → `mq:active`, then merges `develop` into the PR branch and creates a `merge-queue/active` commit status on the resulting head SHA — see [§4.3](#43-data-model)), and on the next cycle evaluates CI to either squash-merge or eject. The cycle's headship check verifies that the PR's current head SHA still carries the `merge-queue/active` status; if a new commit was pushed in between, the new head SHA lacks the status and the PR is ejected. Both squash-merge and eject clear all `mq:*` labels and overwrite the activation status with a terminal state (see [§4.3](#43-data-model)), so terminal PRs disappear from the discovery search on the next cycle (parallel to `/dequeue`'s cleanup). Algorithm in [§4.6](#46-per-cycle-algorithm).

After ejection, the author addresses the reported cause and re-enqueues with `/merge`.

#### Squash-merge author

The squash is created by the App identity (see [§3](#3-goals-non-goals-and-prerequisites), [§4.9](#49-implementation-constraints)) via the merge API; `mq:*` label writes also go through the App so the audit's exemption is slug-pinnable to a single trusted identity. The squash commit's `author` is preserved as the original PR author (the merge API's default behaviour) — this is what GitHub's contribution graph and `git log --author` key off of, so PR authors retain credit. The `committer` is the App.

**Multi-author PRs.** The merge API does not auto-collect `Co-authored-by:` trailers from the squashed commits the way the web UI's squash button does, so collaborative PRs would silently drop non-head authors from the contribution graph. The processor could optionally compensate by reading the PR's commit list and constructing the squash `commit_message` with `Co-authored-by:` trailers for every distinct author beyond the head — worth considering if multi-author PRs are common.

#### Branch protection integration

A `merge-queue/managed` GitHub status check serves as the enforcement path, promoted to required after the rollout in [§7](#7-rollout). A lightweight handler on `pull_request_target: [opened, synchronize, reopened]` posts the check on every PR's head SHA: `pending` for PRs touching opted-in component paths, and `success` for PRs touching no opted-in path — required-check semantics treat a missing check as blocking, so non-opted-in PRs must be explicitly marked `success` rather than skipped. `pull_request_target` is required (rather than `pull_request`) so the handler can post statuses on fork PRs — `pull_request` runs with a read-only `GITHUB_TOKEN` for fork heads, which would silently fail the status write and leave fork PRs permanently blocked once `managed` is required. The same no-checkout safety pattern as the audit handler ([§4.3.1](#431-tamper-resistance)) applies. The processor flips opted-in PRs from `pending` to `success` immediately before the squash API call. Once the check is required on `develop`, branch protection rejects any merge to `develop` for opted-in components except the queue's bot-driven squash, while non-opted-in PRs merge freely. (Distinct from the per-cycle `merge-queue/active` status from [§4.3](#43-data-model), which binds activation to a specific commit; `managed` enforces queue routing on `develop`.)

### 4.6 Per-cycle algorithm

*The following is functional pseudocode illustrating the algorithm's logic and sequencing. It is not intended as a literal implementation.*

```
# ALL_QUEUES is the set of opted-in queue names from §4.1 (six in the initial scope).

process_cycle():
    # 1. Discover: rebuild each queue by searching for its membership label,
    #    scoped to open PRs (terminal PRs have their mq:* labels cleared on
    #    squash-merge or eject; is:open is defense-in-depth against any leak).
    #    A PR labeled mq:hipdnn AND mq:miopen-provider is returned by both
    #    searches and appears in both members lists. PRs with no mq:<queue>
    #    label (e.g., rejected at the handler for touching no opted-in path,
    #    §4.4) are not returned by any search and never enter the algorithm.
    members = {q: [] for q in ALL_QUEUES}
    for q in ALL_QUEUES:
        for pr in search_prs(query=f"is:pr is:open label:mq:{q}"):
            members[q].append(pr)
    pulls = {pr for q in ALL_QUEUES for pr in members[q]}

    # 2. Derive canonical state for each PR:
    #      - enqueued_at         — timestamp of the bot's most recent
    #                              mq:queued label-application event (timeline)
    #      - queues              — mq:<queue> labels minus the state labels
    #      - is_validly_active   — mq:active label AND a merge-queue/active
    #                              commit status created by the App identity
    #                              present on the current head SHA
    #    is_validly_active is consumed by step 5b's eject path, not here.
    for pr in pulls:
        derive_canonical_state(pr)

    # 3. Build queues: each queue is a FIFO sorted by enqueue time.
    for q in ALL_QUEUES:
        members[q].sort(key=lambda p: p.enqueued_at)

    # 4. Identify ready PRs: head of every queue they belong to.
    ready = {pr for pr in pulls
             if all(members[q][0] == pr for q in pr.queues)}

    # 5. For each ready PR, advance its state machine.
    for pr in ready:
        if "mq:active" not in pr.labels:
            # 5a. Activate: merge develop into the branch (pushed via the
            #     App-token credential from §3 so CI re-triggers), create the
            #     merge-queue/active commit status on the resulting SHA, then
            #     wait one cycle for CI. Three terminal outcomes from the merge
            #     step — see §4.9 for the no-op / transient / race handling
            #     that lives below this layer.
            result = merge_develop_into(pr.branch, author=BOT_USER)
            if result.is_conflict:
                eject(pr, reason="merge conflict with develop")
                continue
            if not result.ok:
                continue                              # deferred — retry next cycle
            mark_active(result.head_sha)              # creates merge-queue/active commit status
            set_labels(pr, add="mq:active", remove="mq:queued")
            update_status_comment(pr)                 # rewrite visible body for new state
            continue                                  # CI must run before evaluation

        # 5b. Already active → evaluate the post-merge CI cycle.
        if not pr.is_validly_active:
            # Either branch was updated since activation (new head SHA lacks the
            # status), or mq:active was applied externally without an activation
            # status — both are unsafe to merge.
            eject(pr, reason="activation invalid (branch updated or label tampered)")
        elif all_required_checks_passed(pr):          # queries develop's branch protection
            squash_merge(pr, sha=pr.head.sha)         # each cycle, so newly-required
        elif any_required_check_failed(pr):           # checks are observed without a
            eject(pr, reason=failed_check_name(pr))   # code change.
        else:
            pass                                      # checks still pending; retry next cycle
```

A few invariants worth calling out:

- **No persistent processor state.** Every cycle starts from `search_prs(...)` and rebuilds `members` from scratch. If a cycle crashes mid-flight, the next cycle reconstructs the same picture from canonical sources (labels, timeline, commit status). State lives in the PR, not in the runner.
- **Label tampering is handled out-of-band by the audit job** (see [§4.3.1](#431-tamper-resistance)), not by the processor. The processor's only checks are algorithmic (merge conflict at activation, CI verdict, activation-status presence at evaluation) — it has no tamper-detection logic of its own.
- **Activation and evaluation never happen in the same cycle.** Step 5a ends with `continue`, so a freshly activated PR is evaluated only by step 5b on the *next* cycle — CI sees the post-merge tip before the queue acts on its result.
- **Per-queue FIFO is enforced by sort, not by a stored cursor.** The head of each queue is always the open PR with the oldest `enqueued_at` in that queue's member list — no separate "current head" pointer to fall out of sync.
- **Activation order within a single cycle is unspecified across disjoint queue sets.** When step 4 returns multiple ready PRs whose `pr.queues` sets do not intersect, they are activated in arbitrary order — correctness is unaffected because their CI cycles are independent. The queue's safety guarantee is bounded by opt-in: it serializes correctly within the opted-in set; PRs touching only non-opted-in paths are not coordinated by this design at all.
- **Multiple squashes within a single cycle do not produce stale-base merges.** When step 5b squashes more than one PR in the same cycle, `develop` advances between squashes — but those PRs are necessarily in disjoint queue sets (otherwise only the head would be in `ready`), and the membership rule in [§4.2](#42-path--queue-mapping) guarantees disjoint queue sets ⇒ independent PRs by construction. Each PR's CI ran against an earlier `develop` snapshot, but the merges that land between snapshots and squash cannot affect it. (Same-queue PRs serialize naturally — only the head ever reaches `ready`, so the second is re-activated against the post-squash `develop` on a later cycle.)

Crash-safety follows from the single concurrency group ([§4.7](#47-cadence-and-concurrency)) plus per-step idempotency. Activation does three writes — the `develop`-merge, the `merge-queue/active` status creation, and the `mq:queued` → `mq:active` label flip — and each is individually idempotent: re-merging `develop` after a successful merge is a no-op (or fast-forward), creating a commit status on the same SHA with the same context overwrites, and the label flip is idempotent on each side. A crash between any two writes leaves the PR in a state where the next cycle treats it as not-yet-activated and re-runs activation, converging to the same end state.

Each cycle also writes a markdown summary to `$GITHUB_STEP_SUMMARY`, rendered inline in the Actions UI for the run, containing:

- **Per-queue depth** — queued and active counts for each opted-in queue.
- **Active PRs** — PR number, head SHA, activation time, and current required-check status for every `mq:active` PR.
- **Cycle outcomes** — every activation, squash, and eject performed during this processor run, with the eject reason where applicable.
- **Cycle duration** — wall-clock time for the run.

The summary is a UI surface only; the processor reads no state from it (parallel to the status-comment rule in [§4.3](#43-data-model)). This is the v1 operational surface — enough to answer "why didn't my PR merge?" and "what is the queue doing right now?" by opening the latest `mq-processor` run, without standing up external metrics infrastructure. A parallel JSON artifact (via `actions/upload-artifact`) carrying the same per-cycle data is the natural extension if a machine-readable surface is needed later; not implemented in v1 (see [§8](#8-risks)).

### 4.7 Cadence and concurrency

- 3-minute processor cron. Single concurrency group named `mq-processor`, with `cancel-in-progress: false` — no overlap, no cancellation. GitHub serializes runs in the group, so two processor invocations cannot race on the same labels, statuses, or merge calls; a long cycle simply delays the next one rather than running alongside it.
- One PR processed per queue per cycle. No batching.
- The `merge-queue/active` commit status is created *on* the SHA produced by the bot's `develop`-merge, so subsequent cycles' presence checks compare against the post-merge tip and the bot's own merge commit doesn't trigger an eject.

### 4.8 Opt-in / extension

Any component (in or outside the hipDNN ecosystem) opts in by editing the centralized `PATH_TO_QUEUES` config (likely a YAML file under `.github/`, exact location TBD):

1. **Add a path entry for your component.** Map your path prefix to a list of queues: your own queue, plus the queue of every downstream component your changes could break (the membership rule from [§4.2](#42-path--queue-mapping)). A *leaf* component (nothing downstream) lists only its own queue; an upstream component lists every downstream queue too.
2. **Update entries of components upstream of you.** Any existing entry that names a component you're downstream of must add your queue to its list, so upstream PRs serialize with you.

Concrete example — opting in a new provider in the hipDNN ecosystem:

1. Add the new provider's path entry. Providers are leaves of the dependency graph (nothing downstream of them), so it enters only its own queue:
   ```python
   "dnn-providers/new-provider/": ["new-provider"],
   ```
2. Add `"new-provider"` to the `projects/hipdnn/` and `dnn-providers/integration-tests/` entries — both are upstream of every provider, so their PRs need to serialize against the new one.

There is no per-project file. Removing the entry (and your queue from other entries) opts a project out cleanly. The initial opt-in set is hipDNN core + four providers + integration-tests; other rocm-libraries projects are explicitly out of scope for v1.

A required CI step on PRs that modify `PATH_TO_QUEUES` validates the config: every queue name appearing in any list must exist as its own path entry, and every entry whose component is upstream of another must list that downstream's queue. This catches the silent-skew failure where step 2 above is forgotten — a missing upstream entry would let upstream PRs merge without serializing against the new component.

**Config is always read from `develop`, never from PR content.** The handler and processor resolve `PATH_TO_QUEUES` via the GitHub contents API at `ref=develop` rather than from the workflow's local checkout, so a PR cannot modify its own queue routing by editing the config in its own branch. PRs that *do* modify `PATH_TO_QUEUES` are subject to the bootstrap protection in [§8](#8-risks) and require manual maintainer merge after explicit review.

### 4.9 Implementation constraints

Each processor cycle will follow a read → decide → execute structure. First, queue state is fetched from GitHub (PR labels, enqueue timestamps, head SHAs, CI status). That snapshot is passed to a pure decision function that returns the set of actions to take — squash, activate, eject, wait — without performing any I/O itself. Finally, those actions are dispatched back to GitHub. Keeping the decision layer pure means tests can construct any queue state as plain data and call the function directly, with no mocks or network access required. The `develop`-merge push, the squash-merge to `develop`, the `merge-queue/active` status writes (post + eject overwrite), and `mq:*` label add/remove all use the App installation token from [§3](#3-goals-non-goals-and-prerequisites); only the `merge-queue/managed` status post and comments go through `GITHUB_TOKEN` with workflow-scoped permissions.

The activation merge step absorbs the messy details of the GitHub merges API so the algorithm in [§4.6](#46-per-cycle-algorithm) stays clean. It treats a successful merge and a no-op merge (PR branch already contains `develop`) the same way — both yield a head SHA the bot can stamp the activation status on. Transient API errors and secondary rate-limits are retried internally with backoff before being surfaced as a deferred result that the next cycle re-attempts. Immediately before stamping the activation status, the step re-reads the PR's head SHA to catch the race where the author pushed between the merge and now; if so, it defers rather than blessing a SHA the PR has already moved past. Only true merge conflicts surface as conflict for the algorithm to eject on.

The App identity from [§3](#3-goals-non-goals-and-prerequisites) is used for four operations:

- **Push the `develop`-merge commit to the PR branch** (including fork branches, where `GITHUB_TOKEN` cannot push at all). Needs to trigger `pull_request: synchronize`, which `GITHUB_TOKEN`-driven writes do not ([GitHub docs](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication#using-the-github_token-in-a-workflow)).
- **Squash-merge the PR to `develop`.** Needs to trigger `push: branches: [develop]` post-submit CI, same reason as above.
- **Create and overwrite the `merge-queue/active` commit status** that binds activation to a specific head SHA ([§4.3](#43-data-model)). Needs an identity that no other workflow in the repo can authenticate as, so the activation-status creator filter ([§4.3.1](#431-tamper-resistance)) is unspoofable.
- **Add and remove `mq:*` labels** (enqueue, activation transition, eject, terminal cleanup). Needs an identity that no other workflow in the repo can authenticate as, so the audit's self-applied-label exemption ([§4.3.1](#431-tamper-resistance)) slug-pins to the App rather than to the unmitigated `github-actions[bot]` shared by every `GITHUB_TOKEN`-driven write.

Required App permissions: **`contents: write`** (develop-merge push), **`pull-requests: write`** (merge API), **`statuses: write`** (activation marker), **`issues: write`** (the labels-on-PR endpoint is `POST /repos/:o/:r/issues/:n/labels`, which resolves on the issues permission for GitHub Apps). Only the `merge-queue/managed` status post and comments go through `GITHUB_TOKEN` with a workflow-scoped `permissions:` block.

Two App-configuration constraints:

- **No branch-protection bypass on `develop`.** The merge API call must be gated by branch protection so the [§5](#5-open-source-contributor-policy) safety properties (post-enqueue approval revocation, required-check enforcement) hold; `contents: write` without bypass also cannot push directly to `develop`.
- **Repo-scoped installation** to `rocm-libraries` rather than org-scoped, to bound the [§8](#8-risks) leaked-key blast radius to a single repo.

## 5. Open-Source Contributor Policy

**Policy.** External contributors may use `/merge` on their own PRs. The safeguards that apply to any merge — required reviewer approvals and required CI checks — apply unchanged.

**Why this works without extra queue logic.** GitHub's branch protection rules apply to the merge API by default — bypass requires explicit per-actor configuration ([GitHub docs: protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)). If the bot account is granted bypass rights, the implementation must check that all required statuses have passed before squash-merging; otherwise branch protection enforces this automatically. `develop`'s required `TheRock CI Summary` check and CODEOWNER reviews (see prereqs in [§3](#3-goals-non-goals-and-prerequisites)) serve as the gate.

**Defense-in-depth.** The command handler must reject `/merge` on a PR that has not yet been approved, so an unapproved PR doesn't sit in the queue burning processor cycles only to fail at the squash. This is a small check at enqueue time, not a replacement for branch protection. Enabled from Phase 1 of [§7](#7-rollout).

**Approval revocation between enqueue and squash.** A required reviewer dismissing approval after the PR is enqueued causes branch protection to reject the squash API call; the processor catches the failure and ejects with reason `"approval revoked"`. Bot-bypass on `develop` would let the squash succeed despite the dismissal and is incompatible with this property — it must not be enabled.

**Fork PRs.** The processor pushes the `develop`-merge to the PR branch, which for fork PRs requires the author to have **Allow edits by maintainers** enabled (the GitHub default; authors can disable it — see [GitHub docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/allowing-changes-to-a-pull-request-branch-created-from-a-fork)). The handler enforces this at `/merge` time and rejects with a clear message if disabled. If the contributor cannot or will not enable maintainer-edits, a maintainer may manually merge the PR after deeming it safe — the queue is opt-in, not the only path.

## 6. Validation

Two layers of testing — per-commit on the queue's source, and end-to-end on a fork repo before any production exposure.

### Per-commit (Python)

Because the state-transition logic is separated from GitHub I/O (see [§4.9](#49-implementation-constraints)), the algorithm can be exercised against synthetic queue snapshots directly — no GitHub mocks required. Per-commit tests on the queue's source files should cover, at minimum:

- **Pure logic** — path → queue mapping, FIFO sort, headship check, derivation of `enqueued_at`/`queues`/`is_validly_active` from canonical sources (see [§4.3](#43-data-model), [§4.6](#46-per-cycle-algorithm)). The worked example in [§4.2](#42-path--queue-mapping) is a natural test case.
- **Algorithm invariants** — no PR squash-merged unless at the head of every queue it belongs to; no PR squash-merged whose head SHA lacks an App-created `merge-queue/active` commit status; re-enqueue is idempotent; FIFO order is respected. The exact harness is an implementation choice; what matters is that these invariants are exercised.

### Fork dogfood

Implement and validate against a fork of `rocm-libraries`. Synthetic PRs against the fork exercise the full handler + processor flow on real GitHub APIs, demonstrating the invariants above and edge cases that aren't visible to the algorithm in isolation:

- merge conflict during activation
- CI failure during evaluation
- simultaneous `/merge` on the same PR
- author push between activation and squash

## 7. Rollout

| Phase | What |
|-------|------|
| 0     | Fork dogfood — implement and validate on a fork, covering all invariants and edge cases in [§6](#6-validation). |
| 1     | Commit to rocm-libraries with hipDNN-ecosystem opt-in only. The at-enqueue approval pre-check (see [§5](#5-open-source-contributor-policy)) is enabled from day one. No branch-protection changes. |
| 2     | Add `merge-queue/managed` as **non-blocking** (see [§4.5](#45-pr-lifecycle)). |
| 3     | Promote `merge-queue/managed` to a **required** status check on `develop`. Admins retain bypass as an emergency escape hatch. (See [§8](#8-risks).) |
| 4     | Extend opt-in to other rocm-libraries subprojects on request.                                                   |

## 8. Risks

- **3-minute poll** is slow at low load. Revisit if necessary.
- **Head-of-line stalls.** Cross-queue blocking means a slow upstream PR at the front of multiple queues holds up downstream work.
- **Serialization throughput — wall-clock end-to-end CI for a PR can comfortably exceed several hours.** Three contributing factors:
  - *Per-job timeouts.* Build jobs are bounded at 30 minutes ([`therock-ci-linux.yml`](https://github.com/ROCm/rocm-libraries/blob/develop/.github/workflows/therock-ci-linux.yml), `timeout-minutes: 30`); individual test-component jobs are bounded at 210 minutes / 3.5 hours ([`therock-test-component.yml`](https://github.com/ROCm/rocm-libraries/blob/develop/.github/workflows/therock-test-component.yml), `timeout-minutes: 210`).
  - *Per-cycle setup overhead.* Machine acquisition, cache restoration, and matrix fan-out are fixed per attempt and not amortizable across queue cycles. Runner availability can itself be a bottleneck, adding significant wall-clock time before a job even starts.
  - *n × CI drain time.* Intersecting PRs (e.g. multiple core PRs in succession, an integration-tests PR queued behind providers, or any provider PR queued behind a core or integration-tests PR) merge one at a time, each waiting for a full CI cycle; a queue of *n* intersecting PRs takes roughly *n × CI time* to drain.
- **No CI retries — flakes are terminal.** The processor treats any failed required check as a real failure (`any_required_check_failed → eject`). On a heterogeneous GPU matrix, transient hardware/runner failures eject otherwise-good PRs and force a manual `/merge` re-enqueue, paying another full CI cycle. This compounds with the throughput risk above: every flake-induced eject is another *n × CI* penalty.
- **Surprise auto-eject** when an author pushes new commits mid-queue. Detection happens on the next processor cycle (up to 3 minutes after the push), so feedback is not instant — the eject comment must be explicit about why and how to re-enqueue.
- **Compromised workflow runner.** Branch protection (approvals + required checks) is the actual guarantor of merge integrity; App authority is bounded by it on every API call. What the App identity additionally protects is *queue integrity* — the activation-status creator filter ([§4.3](#43-data-model)) prevents sibling workflows using `GITHUB_TOKEN` from posting forged `merge-queue/active` statuses that would confuse the processor (causing wasted cycles and out-of-order activation, not bad merges). Environment scoping ([§3](#3-goals-non-goals-and-prerequisites)) aligns the App secret's reachability with the legitimate execution surface — every queue trigger runs from `develop` by GitHub Actions' execution model — so workflows on PR-branch refs cannot mint the App token regardless of their file name. Self-bootstrap protection (below) blocks `develop`-side changes to workflow or queue files from merging through the queue itself. The installation token is short-lived (1-hour TTL) and ephemeral, but the *private key* that mints it is long-lived, does not auto-rotate, and a leak compromises every repo the App is installed on; rotation policy and the compromise runbook (revoke installation, regenerate key, audit recent App-identity merges to `develop`) will be documented alongside the v1 implementation.
- **Self-bootstrapping via workflow or required-check edit.** A PR modifying the queue's own logic, *any* workflow file (which could mint the App token if it lands on `develop`), or the workflows that feed `develop`'s required checks could otherwise merge through the queue itself, letting a single approval ship a change that weakens the queue's own gates. The handler rejects `/merge` on PRs whose changed-files set intersects: any path under `.github/workflows/` (broad on purpose — a new workflow file with an arbitrary name on `develop` would gain access to the App's Environment-scoped secret per [§3](#3-goals-non-goals-and-prerequisites)), the queue's source directory, the `PATH_TO_QUEUES` config (see [§4.8](#48-opt-in--extension)), and any branch-protection-as-code config. Such PRs require manual maintainer merge after explicit review. CODEOWNER review on these paths mitigates but is not equivalent — a single CODEOWNER approval can ship a CI-disabling PR through the queue, e.g. turning `TheRock CI Summary` into a no-op so the queue's `all_required_checks_passed` is satisfied trivially.
- **API rate-limit budget.** Discovery does 6 `search_prs` per cycle × 480 cycles/day ≈ 2.9K search calls/day; per-PR timeline + commit-status reads add ~2 calls per active PR per cycle. Comfortably under GitHub's 5K/hr REST and 30/min secondary search limits at expected volumes (≤30 active PRs across all queues). If volume grows materially, batching via GraphQL (one query for many PRs) is the next step.
- **No aggregated metrics in v1.** Per-cycle queue depth, active PRs, activation/squash/eject events with reasons, and cycle duration are written to `$GITHUB_STEP_SUMMARY` on every processor run (see [§4.6](#46-per-cycle-algorithm)) — sufficient for case-by-case debugging via the Actions UI, but human-readable only and not aggregated as time series. Time-in-queue distributions, eject-reason rates, and missed-audit-event detection require either a parallel JSON artifact emitted alongside the markdown summary or direct GitHub API consumption (PR labels, commit statuses, and timeline events are the canonical state); deferred until the per-run summaries prove insufficient. The status comment, eject comments, and Actions run history remain the per-PR diagnostic surfaces.
- **Silent dequeue from a dropped audit event.** The real-time audit on `pull_request_target: [unlabeled]` is the *only* mechanism for catching `mq:*` label removals — once a label is gone, the processor's discovery (`search_prs(label:mq:<queue>)`) doesn't return the PR at all. If GitHub Actions is degraded, an audit job hits a transient API failure, or the workflow is rate-limited, an `unlabeled` event can be missed and the PR is silently dequeued with no detection or notification. The author has to notice their PR didn't merge and re-`/merge` themselves. If this becomes a real failure mode, defence-in-depth is available: the processor could read the issue timeline once per cycle for each PR currently in the discovery set and eject any whose most recent `mq:*` label change wasn't applied by the App identity. This catches *additions* the audit missed (the new label brings the PR into discovery scope) but still can't catch *removals* (the PR is no longer in scope). Not implemented in v1; the failure mode is rare and the author-re-`/merge` recovery path is acceptable.

## 9. Potential future enhancements

Not in scope for v1, but worth tracking as quality-of-life follow-ups once the queue is in steady state:

- **External notification channels.** Author- and team-facing notifications on enqueue, activation, squash, and eject events are delivered exclusively via the PR status comment ([§4.3](#43-data-model)) and per-event eject comments in v1, which adds to existing GitHub-comment noise and is easy to miss for authors who triage email by sender. Pushing the same events to an external channel (Microsoft Teams in particular, given existing team usage; Slack and email digest as variants) would meaningfully improve the feedback loop. The `PATH_TO_QUEUES` config ([§4.8](#48-opt-in--extension)) would likely be extended with a per-queue `teams_channel` (or equivalent webhook URL) so each component team's notifications land in the right place rather than a single firehose channel.
- **Hardening required checks against sibling-workflow forgery.** Both required checks on `develop` (`TheRock CI Summary` and `merge-queue/managed`) are gated by name only — branch protection accepts a passing status or check_run with the right name from any workflow that can mint `GITHUB_TOKEN`. Worth investigating a tightening (e.g. producing-app pinning) for both together as a follow-up.
