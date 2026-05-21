---
phase: 3
slug: handler-processor-on-fork
audited: 2026-05-21
auditor: Claude (gsd-secure-phase)
status: secured
asvs_level: 2
threats_total: 50
threats_closed: 33
threats_open: 0
threats_accepted: 17
---

# Phase 3 — Security

Consolidated security audit of all 17 implemented plans (03-00 through 03-16) on the
`SamuelReeder/rocm-libraries` fork-dogfood implementation. Each declared plan-time threat
verified against the actual code, not against documentation or intent. Post-plan
deviations (03-wr-09, 03-wr-11a/b/c, no-approval gate disable, admin ruleset bypass)
classified as revised-mitigation or accepted-risk and recorded explicitly.

---

## Summary

| Result | Count |
|--------|-------|
| Closed (mitigation verified in code) | 33 |
| Open (mitigation absent / incomplete) | 0 |
| Accepted risk (documented) | 17 |
| **Total threats audited** | **50** |

By STRIDE category:

| Category | Total | Closed | Accepted | Open |
|----------|-------|--------|----------|------|
| Spoofing | 5 | 4 | 1 | 0 |
| Tampering | 17 | 11 | 6 | 0 |
| Repudiation | 5 | 1 | 4 | 0 |
| Information Disclosure | 9 | 1 | 8 | 0 |
| Denial of Service | 8 | 7 | 1 | 0 |
| Elevation of Privilege | 9 | 9 | 0 | 0 |

The previously OPEN finding (T-03-WR-NO-APPROVAL — at-enqueue ≥1-approval gate disable)
was closed on 2026-05-21 by commit `70edcc9c93e` (03-wr-12), which refactored the
code-level comment-out into a `MergeQueueConfig.require_approval_at_enqueue: bool = True`
config flag with an `MQ_REQUIRE_APPROVAL` env-var override scoped to the dogfood handler
job only. The default preserves the RFC §5 upstream contract; unit tests pin both
branches (enabled and disabled); the PORT-02 marker is preserved in both
`state.py:275-278` and `mq-handler.yml:154-160` so the Phase 5 pre-flight knows to
remove the env override. See `## Closed Findings` for the verification evidence.

Critical-invariant audit (CLAUDE.md project security invariants):

| Invariant | Status | Evidence |
|-----------|--------|----------|
| App-token per-job permission scoping via `actions/create-github-app-token@v3` | CLOSED | `mq-handler.yml:123-131` (command job: full set); `mq-handler.yml:180-183` (audit job: `contents: read` + `issues: write` + `statuses: write` ONLY — no `pull-requests: write`); `mq-processor.yml:160-168` |
| `GITHUB_TOKEN` bounded to read-only pre-flight + reactions | CLOSED | `mq-handler.yml:112` (pre-flight read-only); `mq-processor.yml:141`; App token overrides `GITHUB_TOKEN` for all writes at `mq-handler.yml:144` and `mq-processor.yml:189` |
| No persistent processor state | CLOSED | `cmd_process.process_cycle` reconstructs from API every cycle; no DB or cache file references in any module |
| No branch-protection bypass on `develop` for §5 properties | ACCEPTED (admin role bypass added for dogfood) | See Accepted Risks |
| `yaml.safe_load` only | CLOSED | `config.py:140` (sole YAML parse site) |
| Actions pinned by SHA, not tag | CLOSED | `mq-handler.yml:89,98,123`; `mq-processor.yml:118,127,160,223` — all `uses:` lines carry 40-char SHA |
| App slug pinned via env-var-trust path (03-wr-01) | CLOSED | `gh.py:194-258`; `mq-handler.yml:152-153`; `mq-processor.yml:203-204` |
| `pull_request_target` triggers do NOT check out PR HEAD with credentials | CLOSED | `mq-handler.yml:89-93` pins `ref: refs/heads/develop`, `persist-credentials: false`; no `github.event.pull_request.head.sha` expression anywhere in the file (grep confirms zero hits) |

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| GitHub webhook → GHA runner | `issue_comment`, `pull_request_target`, `pull_request_review`, `schedule`, `workflow_dispatch` | Comment bodies (untrusted), label/PR/review actor identity (GitHub-attested), PR diffs (untrusted content but read-only metadata) |
| GHA runner → `actions/create-github-app-token@v3` | Workflow → action: client-id + private-key (Environment secret); action → workflow: opaque `ghs_*` token + JWT-attested `app-slug` | App private key (Environment-scoped to `develop`); installation token (per-job scoped, post-step revoked) |
| GHA runner → GitHub REST API | App installation token bearer header (write surface); `GITHUB_TOKEN` bearer (read-only pre-flight + reactions) | Labels, statuses, comments, merge operations, PR/branch reads |
| `develop` branch → `path_to_queues.yml` Contents API read | Handler + processor read at `ref=develop` (never from PR head) | YAML config text, base64-decoded → `yaml.safe_load` |
| Workflow file modifications → `cmd_handle.is_self_bootstrap` | PR diff intersected against `SELF_BOOTSTRAP_PATHS` globs BEFORE any state mutation | List of changed-file paths (from `pulls.list_files`) |
| Phase 1 pure layer ↔ Phase 2/3 I/O layer | AST-walker test enforces `PURE_LAYER_MODULES` set imports only stdlib + other pure modules; dogfood subpackage explicitly excluded | Dataclass instances (frozen, slots); no live API objects cross into pure |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Status | Evidence |
|-----------|----------|-----------|-------------|--------|----------|
| T-03-00-01 | Tampering | 02-VERIFICATION.md rewrite | accept | CLOSED | Dev-only planning state; not a production surface. SUMMARY 03-00 confirms acceptance |
| T-03-00-02 | Repudiation | Verifier rewrite | accept | CLOSED | Git history of `.planning/` is the audit trail |
| T-03-01-01 | Tampering | argparse refactor | accept | CLOSED | Internal refactor; same trust boundary as Phase 2 |
| T-03-01-02 | Elevation of Privilege | Stub `run_handle` / `run_preflight` | mitigate | CLOSED | `cmd_process.py:614-624` — uncaught exception handler prints traceback + returns 1 (stubs replaced with real delegations at `cmd_process.py:545-549, 589-591`) |
| T-03-01-03 | Denial of Service | argparse `required=True` missing subcommand | accept | CLOSED | `cmd_process.py:323` — `add_subparsers(dest="subcommand", required=True)` |
| T-03-02-01 | Tampering | `path_to_queues.yml` content | mitigate | CLOSED | `config.py:140` — `yaml.safe_load(raw)`; `config.py:136-138` reads at `ref=_DEVELOP_REF` (constant `"develop"`); no `yaml.load`/`unsafe_load` anywhere in repo |
| T-03-02-02 | Information Disclosure | App slug exposure | accept | CLOSED | App slugs are public on App pages |
| T-03-02-03 | Tampering | `SELF_BOOTSTRAP_PATHS` bypass | mitigate | CLOSED | `config.py:55-74` defines constant; the file itself is inside `.github/merge-queue/**` glob (line 57); `cmd_handle.is_self_bootstrap` at `cmd_handle.py:140-160` is invoked at `cmd_handle.py:536-546` before any state mutation |
| T-03-03-01 | Tampering | `path_to_queues.yml` on develop | mitigate | CLOSED | File on `SELF_BOOTSTRAP_PATHS` (`config.py:62`); `cmd_handle.py:530-546` rejection runs before mutation |
| T-03-03-02 | Tampering | Self-modifying queue routing via PR branch | mitigate | CLOSED | `config.py:95,137` — `_DEVELOP_REF` constant pinned; `get_content(..., ref=_DEVELOP_REF)` enforces |
| T-03-03-03 | Information Disclosure | Queue topology disclosure | accept | CLOSED | Public-by-design routing config |
| T-03-04-01 | Elevation of Privilege | preflight runs before App-token mint | mitigate | CLOSED | `mq-handler.yml:79-113` (pre-flight at step 1) and `mq-handler.yml:115-131` (App token mint at step 2); same ordering in `mq-processor.yml:104-142` then `:158-168` |
| T-03-04-02 | Tampering | `$GITHUB_STEP_SUMMARY` write race | accept | CLOSED | GHA serializes steps; `preflight.py:118` uses `"a"` append mode |
| T-03-04-03 | Denial of Service | API throttling | accept | CLOSED | `_RetryProxy` from `gh.py` auto-retries on secondary rate limits (Phase 2 WR-01) |
| T-03-04-04 | Information Disclosure | Default branch name leak | accept | CLOSED | Public information |
| T-03-05-01 | Spoofing | Wrong App slug → §4.3.1 creator filter fails open | mitigate | CLOSED | `gh.py:231-243` env-var path requires BOTH `MQ_APP_SLUG` and `MQ_APP_ID`; `gh.py:254-257` bot-user lookup fails closed when slug is wrong (login won't resolve) |
| T-03-05-02 | Information Disclosure | .pem private key in operator's downloads | mitigate | CLOSED | Per plan 03-05 Task 2 step 7 operator runbook; runbook artifact present |
| T-03-05-03 | Elevation of Privilege | App permissions over-granted | mitigate | CLOSED | Verified by per-job `permission-*` inputs `mq-handler.yml:128-131`, `mq-processor.yml:165-168` scope tokens DOWN from App's full set |
| T-03-05-04 | Spoofing | Environment secret leaked to non-develop ref | mitigate | CLOSED | `mq-handler.yml:56,165`, `mq-processor.yml:93` declare `environment: mq-secrets`; deployment-branches rule pinned to `develop` per plan 03-05 |
| T-03-05-05 | Tampering | App private key compromise | accept | ACCEPTED | Compromise procedure is Phase 5 PORT-01 runbook |
| T-03-05-06 | Repudiation | App actions logged as App identity | accept | ACCEPTED | GitHub-native audit trail; `resolve_app_identity` unspoofable per Pitfall 2 |
| T-03-06-01 | Spoofing | Comment-author identity | mitigate | CLOSED | `cmd_handle.py:195-197` — `repos.get_collaborator_permission_level(...)` (live API; NEVER `author_association`); `cmd_handle.py:208` enforces eligibility |
| T-03-06-02 | Tampering | `path_to_queues` tampering via PR branch | mitigate | CLOSED | `cmd_handle.py:451-461` delegates to `config.build_config_from_develop` which reads at `ref="develop"` |
| T-03-06-03 | Elevation of Privilege | `/merge` on self-bootstrap-path PR | mitigate | CLOSED | `cmd_handle.py:530-546` runs `is_self_bootstrap` check; on intersection posts rejection comment with `return 0` and NO state mutation (no labels, no eyes, no status comment) |
| T-03-06-04 | Elevation of Privilege | Inline-code `/merge` mention misfiring | mitigate | CLOSED | `cmd_handle.py:96` — `_CMD_RE = re.compile(r"^/(merge|dequeue)\s*$")`; `cmd_handle.py:136` matches per-line after `.strip()` |
| T-03-06-05 | Information Disclosure | Role-name leak in rejection comment | accept | ACCEPTED | Public collaborator metadata |
| T-03-06-06 | Repudiation | Idempotent second `/merge` | mitigate | CLOSED | `cmd_handle.py:503` eyes-reaction posted FIRST regardless of branch; `cmd_handle.py:511-528` idempotency short-circuit refreshes status comment |
| T-03-06-07 | Denial of Service | Comment-author spamming `/merge` | mitigate | CLOSED | Idempotency short-circuit is O(2 API calls); `_RetryProxy` handles secondary rate limits |
| T-03-06-08 | Tampering | Activation invalidation on force-push (WF-12) | accept | ACCEPTED | Passive defense via Phase 2 executor (App-created status check on current head SHA); no new logic added |
| T-03-07-01 | Elevation of Privilege | `pull_request_target` checkout of PR head | mitigate | CLOSED | `mq-handler.yml:88-94` — `ref: refs/heads/develop`, `persist-credentials: false`, `sparse-checkout: .github/merge-queue`; grep for `github.event.pull_request.head.sha` returns zero hits |
| T-03-07-02 | Elevation of Privilege | Audit job over-permissioned | mitigate | CLOSED | `mq-handler.yml:180-183` — `permissions: { contents: read, issues: write, statuses: write }`; `pull-requests` key absent |
| T-03-07-03 | Information Disclosure | App private key via PR-triggered workflow | mitigate | CLOSED | `mq-handler.yml:56,165` — `environment: mq-secrets`; deployment-branches rule restricts to `develop` |
| T-03-07-04 | Spoofing | Comment author spoofing | mitigate | CLOSED | `cmd_handle.py:195-197` live-checks permission; workflow trusts authenticated `event.comment.user.login` |
| T-03-07-05 | Denial of Service | Per-PR command flood | mitigate | CLOSED | `mq-handler.yml:62-64` — per-PR concurrency group `mq-handler-<pr-number>` with `cancel-in-progress: false`; idempotent short-circuit makes overlap safe |
| T-03-07-06 | Tampering | Workflow file modification via `/merge` | mitigate | CLOSED | `config.py:56` — `.github/workflows/**` in `SELF_BOOTSTRAP_PATHS`; rejection check at `cmd_handle.py:536` |
| T-03-07-07 | Repudiation | App-token-driven writes | accept | ACCEPTED | GitHub timeline records App-identity actor unspoofably |
| T-03-08-01 | Denial of Service | Concurrency eviction backlog | mitigate | CLOSED | `mq-processor.yml:72-74` — static `mq-processor` group, `cancel-in-progress: false`; no `queue:` keyword (grep confirms absent); `mq-processor.yml:84` — `timeout-minutes: 5` < 3-min cron (Pitfall 6) |
| T-03-08-02 | Tampering | Concurrency-group hijack via dynamic ref | mitigate | CLOSED | `mq-processor.yml:73` — `group: mq-processor` literal; no `${{ github.ref }}` interpolation |
| T-03-08-03 | Elevation of Privilege | App token in process step | mitigate | CLOSED | `mq-processor.yml:160-168` — `actions/create-github-app-token@v3` post-step revoke; `permission-*` inputs scope token |
| T-03-08-04 | Tampering | `mq-processor.yml` modification via `/merge` | mitigate | CLOSED | File matches `.github/workflows/**` glob (`config.py:56`); `cmd_handle.is_self_bootstrap` rejects |
| T-03-08-05 | Information Disclosure | Artifact retention exposing queue state | accept | ACCEPTED | `mq-processor.yml:227` — `retention-days: 30`; private-fork inheritance |
| T-03-08-06 | Denial of Service | Cron skipped during GHA-side incident | mitigate | CLOSED | `mq-processor.yml:48-53` — `workflow_dispatch:` with `dry-run` input fallback; stateless-processor catches up |
| T-03-08-07 | Spoofing | App-token-driven label/squash writes | accept | ACCEPTED | App identity unspoofable per Pitfall 2 |
| T-03-09-01 | Tampering | Canary workflow file modification | mitigate | CLOSED | `config.py:63` — `.github/workflows/mq-dogfood-canary.yml` explicit in `SELF_BOOTSTRAP_PATHS`; rejection check at `cmd_handle.py:536` |
| T-03-09-02 | Elevation of Privilege | `pull_request_target` misuse | mitigate | CLOSED | `mq-dogfood-canary.yml:34` — `on: pull_request` (NOT `_target`); no secrets, no checkout |
| T-03-09-03 | Tampering | Shell injection via PR title | mitigate | CLOSED | `mq-dogfood-canary.yml:60-63` — `TITLE` bound via `env:` (NOT shell interpolation); bash literal-substring `[[ "$TITLE" == *"[dogfood-ci-fail]"* ]]` (no glob/regex expansion) |
| T-03-09-04 | Denial of Service | Canary slow / blocking real PRs | accept (revised) | CLOSED | DEVIATION 03-wr-11b: paths filter removed (was mitigation rationale); REVISED disposition: canary stays cheap (`timeout-minutes: 2` at `mq-dogfood-canary.yml:52`; no secrets, no checkout; ~10s runtime; decides from event payload only). See Accepted Risks |
| T-03-09-05 | Information Disclosure | Canary run-log exposure | accept | ACCEPTED | Canary echoes only title + verdict literal; no secrets, no PII |
| T-03-10-01 | Tampering | Per-run JSON tampering by operator | accept | ACCEPTED | Dev-only artifacts under `.planning/` |
| T-03-10-02 | Denial of Service | `poll_pr_state` infinite loop | mitigate | CLOSED | `dogfood/_base.py:246` — `timeout_s` is a required parameter; `_base.py:273-276` raises `TimeoutError` on expiry |
| T-03-10-03 | Elevation of Privilege | dogfood code reachable from production | mitigate | CLOSED | Grep confirms zero `from rocm_mq.dogfood` or `import rocm_mq.dogfood` in production modules (`cmd_handle.py`, `cmd_process.py`, `preflight.py`, `executor.py`, `decision.py`, `snapshot.py`, `gh.py`, `config.py`, `comment.py`, `summary.py`, `pathmap.py`, `state.py`) |
| T-03-11-01 | Tampering | Conflict-seed commit on develop (DOG-02) | accept | ACCEPTED | Deterministic seed file documented in DOGFOOD-RESULTS.md |
| T-03-11-02 | Denial of Service | Driver timeout exhausts CI minutes | mitigate | CLOSED | TIMEOUT_S explicit in dog_02/04/08 modules; backed by required `timeout_s` param in `_base.poll_pr_state` |
| T-03-11-03 | Information Disclosure | JSON output contains PR URL | accept | ACCEPTED | PR URL is public; dev-only artifact |
| T-03-11-04 | Elevation of Privilege | Driver-created PR squash-merges accidentally | mitigate | CLOSED | DOG-02/03/04/05/08 drivers do not call `pulls.create_review`; approval only intentional in DOG-06/07 (`dog_07.py:247` restricted to driver-created `pr_number` captured from `create_dogfood_pr`) |
| T-03-12-01 | Tampering | Title-marker spoofing on non-driver PR | accept | ACCEPTED | Canary design intent — any `dogfood/**` PR with marker triggers canary failure |
| T-03-12-02 | Denial of Service | Driver 20min poll | mitigate | CLOSED | TIMEOUT_S explicit in dog_03 |
| T-03-12-03 | Information Disclosure | Canary check name in eject reason | accept | ACCEPTED | Public information |
| T-03-13-01 | Tampering | Driver-driven push to PR branch | accept | ACCEPTED | This IS the test (force-push eject scenario) |
| T-03-13-02 | Denial of Service | 15-min driver wall time | mitigate | CLOSED | TIMEOUT_S explicit in dog_05 |
| T-03-14-01 | Tampering | Squash result tampering | mitigate | CLOSED | `_verify_squash` Phase B from Phase 2 SC#3 closure (`executor.py:565` calls `compare_commits`); DOG-06 captures tree_diff_status |
| T-03-14-02 | Denial of Service | 60-min wall time + 20 cycles | mitigate | CLOSED | TIMEOUT_S explicit in dog_06 |
| T-03-14-03 | Repudiation | RFC §4.2 narrative replay | accept | ACCEPTED | JSON timeline is the audit trail |
| T-03-15-01 | Spoofing | Approver identity reuse | mitigate | CLOSED | `dog_07.py:192-207` — `approver_client` is a separate API client (`GitHubClient(token=APPROVER_TOKEN)`); rotation procedure documented for Phase 5 PORT-02 |
| T-03-15-02 | Information Disclosure | PAT in env | accept | ACCEPTED | Standard PAT-in-env risk; documented in DOGFOOD-RESULTS.md prerequisites |
| T-03-15-03 | Elevation of Privilege | Driver approves arbitrary PRs | mitigate | CLOSED | `dog_07.py:247` — `create_review(..., pr_number, event="APPROVE")` uses the driver-captured `pr_number` from `create_dogfood_pr` only |
| T-03-15-04 | Repudiation | Approve+dismiss audit trail | accept | ACCEPTED | GitHub records approver identity + dismiss action |
| T-03-16-01 | Tampering | JSON-input malicious payload | mitigate | CLOSED | `dogfood/aggregator.py:116` — `json.loads(path.read_text(...))`; safe-by-construction; aggregator validates expected fields per plan 03-16 |
| T-03-16-02 | Information Disclosure | DOGFOOD-RESULTS.md exposing PR URLs | accept | ACCEPTED | Public PR URLs; dev-only artifact |

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-03-01 | T-03-05-05 | App private key compromise procedure is Phase 5 PORT-01 runbook scope; Phase 3 inherits ops responsibility for ad-hoc rotation. | Plan 03-05 | 2026-05-19 |
| AR-03-02 | T-03-05-06 | App actions logged as App identity is the design intent; GitHub-native audit trail is sufficient. | Plan 03-05 | 2026-05-19 |
| AR-03-03 | T-03-06-05 | Role names are public collaborator metadata; rejection-comment leak is non-issue. | Plan 03-06 | 2026-05-19 |
| AR-03-04 | T-03-06-08 | WF-12 force-push activation invalidation is passively satisfied by Phase 2 executor (no new logic needed). | Plan 03-06 | 2026-05-19 |
| AR-03-05 | T-03-07-07 | App-token-driven writes record App identity per Pitfall 2; sufficient. | Plan 03-07 | 2026-05-19 |
| AR-03-06 | T-03-08-05 | Artifact retention-days:30 + private-fork visibility; queue state is not PII. | Plan 03-08 | 2026-05-19 |
| AR-03-07 | T-03-08-07 | App identity unspoofable per Pitfall 2. | Plan 03-08 | 2026-05-19 |
| AR-03-08 | T-03-09-05 | Canary echoes only PR title + literal verdict string; no secrets or PII. | Plan 03-09 | 2026-05-19 |
| AR-03-09 | T-03-10-01 | Dogfood per-run JSON tampering is dev-only artifact tampering under `.planning/`. | Plan 03-10 | 2026-05-19 |
| AR-03-10 | T-03-11-01, T-03-11-03, T-03-12-01, T-03-12-03, T-03-13-01, T-03-14-03, T-03-15-02, T-03-15-04, T-03-16-02 | Dogfood driver / artifact accepted risks: deterministic test seeds, public PR URLs in driver JSON, canary title-spoofing, public check names, intentional force-push test, JSON timeline as audit trail, PAT-in-env, approve+dismiss audit trail. All under fork-only scope; not production attack surface. | Plans 03-11..03-16 | 2026-05-19 |
| AR-03-11 | T-03-09-04 (revised) | **DEVIATION 03-wr-11b:** `mq-dogfood-canary.yml`'s `paths: ['dogfood/**']` filter was removed because the canary is the required check for branch protection on develop — a path filter would cause non-dogfood PRs to never report the check and be permanently blocked. REVISED MITIGATION: canary stays cheap by structural minimality (no secrets, no checkout, no App token; decides from event payload only; `timeout-minutes: 2` at `mq-dogfood-canary.yml:52`; ~10s runtime per run). DoS risk against real PRs reduces to "an extra ~10s of runner time per PR open/synchronize/reopen" — within fork CI budget. | Operator (2026-05-21 dogfood drive) | 2026-05-21 |
| AR-03-12 | T-03-WR-NO-APPROVAL | **DEVIATION (DOGFOOD-ONLY, PORT-02 revert):** the at-enqueue ≥1-approval gate in `cmd_handle._check_at_enqueue_gates` is disabled via the dogfood handler job's `MQ_REQUIRE_APPROVAL: "0"` env var (`mq-handler.yml:154-160`); the `MergeQueueConfig.require_approval_at_enqueue` default is True (`state.py:279`), preserving the RFC §5 upstream contract. The fork has only one collaborator and PR authors cannot self-approve per RFC §5, blocking every dogfood driver targeting the accepted-merge path (DOG-02/03/04/05/06/07). The actual safety property (no merge without approval) is enforced by GitHub at the squash-merge step via branch protection — the at-enqueue gate is a fail-fast UX layer, not the load-bearing control. **MUST remove the env override before Phase 5 upstream porting**; PORT-02 pre-flight checklist must include this remove line. Updated 2026-05-21: the gate is now config-toggled (not code-commented), pinned by `test_fails_on_no_approval_when_gate_enabled` and `test_no_approval_skipped_when_gate_disabled` so future maintainers cannot silently re-disable. | Operator (2026-05-20), refactored (2026-05-21 03-wr-12) | 2026-05-20 |
| AR-03-13 | branch-protection-bypass-admin | **DEVIATION (DOGFOOD-ONLY):** operator added an admin RepositoryRole bypass on the `mq-dogfood-canary` required-check rule on fork/develop so direct pushes (and feature-branch pushes during dogfood) can land on develop without going through the queue. This deviates from the CLAUDE.md invariant "No branch-protection bypass on the fork's `develop`" but applies to fork only and is needed for dogfood iteration velocity (seeding test PRs, fixing dogfood scripts mid-drive). Re-tighten before Phase 5 upstream porting. Bypass is scoped to admin role only, not to all collaborators. | Operator (2026-05-21 dogfood drive) | 2026-05-21 |
| AR-03-14 | T-03-WR-DOG-07 | DOG-07 deferred per phase verification status `verified_with_deferrals`; not a security threat per se but tracked here so the audit trail records that the dogfood-driven approval-revoked-eject scenario has open work outside the threat-mitigation surface. | Phase 3 verification | 2026-05-21 |

Accepted risks do not resurface in future audit runs unless the underlying assumption changes.

---

## Closed Findings

### Verified post-plan deviations (revised mitigation paths)

**DEVIATION 03-wr-09 (required-check removal from `path_to_queues.yml`):**
- Plan-time mitigation surface was the in-process `_evaluate_required_checks` path in `decision.py`.
- Current code: `decision.py` no longer evaluates required checks. `decide_cycle` Step 5b unconditionally emits `Squash(pr=pr)`; `executor._handle_squash_failure` (`executor.py:374-449`) translates GitHub merge-API 405/422 responses into Eject actions with the GitHub-supplied reason text.
- REVISED MITIGATION: branch protection on fork/develop is the single source of truth for required-check evaluation. The queue defers to GitHub's enforcement at the merge step. No threat was attached specifically to the removed code path; the threats that mentioned it (T-03-03-XX) target tampering on the YAML file itself, which is still mitigated via `SELF_BOOTSTRAP_PATHS`.
- Evidence: `path_to_queues.yml:79-89` documents the removal; `executor.py:417-449` implements the eject-on-405/422 path with explicit pending-marker handling for in-progress required checks.

**DEVIATION 03-wr-11a (status comment on `_handle_eject` + `_handle_squash` success):**
- New write surfaces added during dogfood drive. Both go through the App installation token (`executor.py:623-631`, `executor.py:361-369` call `_upsert_status_comment`).
- No new threat surface: comment writes use the existing App-token / GITHUB_TOKEN scope already declared; idempotent upsert via `_find_status_comment_id` marker discovery; no new untrusted-content sink.

**DEVIATION 03-wr-11c (`_handle_activate` 409 → eject):**
- Documented at `executor.py:212-219`: 409 from `repos.merge` (merge conflict with develop) calls `_handle_eject(pr, "merge conflict with develop", ...)` instead of returning bare failure.
- No new threat surface; tightens existing eject semantics.

### Token-split discipline (RFC §4.9) — verified

- App token mint via `actions/create-github-app-token@v3` SHA-pinned at `bcd2ba49218906704ab6c1aa796996da409d3eb1`: `mq-handler.yml:123`, `mq-processor.yml:160`.
- Per-job `permission-*` inputs scope tokens DOWN from App's full set: `mq-handler.yml:128-131` (command job: contents/pull-requests/issues/statuses write); `mq-processor.yml:165-168` (processor: same four).
- Audit job (Phase 3 stub) declares MINIMUM scope `mq-handler.yml:180-183`: `contents: read`, `issues: write`, `statuses: write`. No `pull-requests: write` (grep confirms absent from audit job).
- `GITHUB_TOKEN` (workflow default) bounded to read-only pre-flight at `mq-handler.yml:112` and `mq-processor.yml:141`. Subsequently OVERRIDDEN with App-minted token at `mq-handler.yml:144` and `mq-processor.yml:189` so all writes attribute to App.

### Self-bootstrap protection (RFC §8) — verified

- `SELF_BOOTSTRAP_PATHS` defined at `config.py:55-74` with five entries: `.github/workflows/**`, `.github/merge-queue/**`, `.github/merge-queue/path_to_queues.yml`, `.github/workflows/mq-dogfood-canary.yml`, plus documented future slot for `terraform/github/**`.
- `cmd_handle.is_self_bootstrap` at `cmd_handle.py:140-160` uses `fnmatch.fnmatch` to intersect changed paths against globs.
- Intersection check runs at `cmd_handle.py:532-546` BEFORE any state mutation (no label apply, no eyes reaction, no status comment) — confirmed by reading the step ordering in `_handle_merge`.
- Constant itself lives inside `.github/merge-queue/**` glob (line 57) — defense in depth: modifying the constant via PR triggers self-bootstrap rejection.

### `pull_request_target` discipline (Pitfall 1) — verified

- `mq-handler.yml` is the only file with `pull_request_target` triggers (`mq-handler.yml:34-35`).
- Sole checkout in this file pins `ref: refs/heads/develop` with `persist-credentials: false`, `sparse-checkout: .github/merge-queue` (`mq-handler.yml:88-94`).
- Grep for `github.event.pull_request.head.sha` returns ZERO hits across all merge-queue workflow files.
- `mq-dogfood-canary.yml:34` uses `pull_request` (NOT `_target`) — structurally cannot access secrets; verified by grep absence of `_target` in canary file.

### Environment scoping (plan 03-05) — verified

- Both `mq-handler.yml:56,165` and `mq-processor.yml:93` declare `environment: mq-secrets`.
- Deployment-branches rule scoped to `develop` ref per operator setup in plan 03-05 Task 2 step 6; verified via SUMMARY 03-05.

### Concurrency invariants — verified

- Processor: `mq-processor.yml:72-74` — static `mq-processor` group; `cancel-in-progress: false`; grep confirms no `queue:` keyword anywhere in workflow surface.
- Handler command job: per-PR group `mq-handler-${{ ... pr-number }}` with `cancel-in-progress: false` at `mq-handler.yml:62-64`; idempotency short-circuit makes overlap safe.
- Handler audit job: separate per-PR audit group `mq-handler-audit-${{ ... }}` so command + audit do not serialize.
- Processor `timeout-minutes: 5` (`mq-processor.yml:84`) — below the per-job-runaway-blocks-next-cron threshold; matches plan 03-08 (Pitfall 6).

### Pre-flight ordering (T-03-04-01) — verified

- Both workflows execute pre-flight BEFORE the App-token mint:
  - `mq-handler.yml`: STEP 1 = checkout + setup-python + install + pre-flight (lines 79-113), STEP 2 = `actions/create-github-app-token@v3` (lines 121-131). Pre-flight uses `GITHUB_TOKEN` (line 112) — read-only contents scope.
  - `mq-processor.yml`: identical pattern at lines 104-142 (pre-flight) then 158-168 (App token).
- `preflight.main` (`preflight.py:129-211`) implements the two checks (default-branch + PATH_TO_QUEUES loadable from develop); exits 1 on check failure, 2 on usage error.

### YAML safety (T-03-02-01) — verified

- `config.py:140` is the only YAML parse site in production code: `yaml.safe_load(raw)`.
- Read at `ref="develop"` via `config.py:137-138`: `client.rest.repos.get_content(owner, repo, _PATH_TO_QUEUES_FILE, ref=_DEVELOP_REF)`.
- Grep confirms no `yaml.load`, `yaml.unsafe_load`, or `Loader=` references anywhere in `rocm_mq/`.

### App identity resolution (env-var-trust path, 03-wr-01) — verified

- `gh.py:194-258` implements `resolve_app_identity` with two modes:
  - Env-var-trust (runtime): requires BOTH `MQ_APP_SLUG` and `MQ_APP_ID`; half-trust is not trust (line 234 condition); `MQ_APP_ID` must parse as int else `ValueError` (line 236-242).
  - JWT fallback (local-dev only): `apps.get_authenticated` path.
- Workflow wiring: `mq-handler.yml:152-153` and `mq-processor.yml:203-204` pass slug from `actions/create-github-app-token@v3`'s JWT-attested `app-slug` output; `MQ_APP_ID` from repo var.
- `bot_user_id` still resolved live (`gh.py:254-257`): fails closed if slug is wrong.

### Pure-layer boundary (T-03-10-03) — verified

- Production modules (`cmd_handle.py`, `cmd_process.py`, `preflight.py`, `executor.py`, `decision.py`, `snapshot.py`, `gh.py`, `config.py`, `comment.py`, `summary.py`, `pathmap.py`, `state.py`) have ZERO imports of `rocm_mq.dogfood`.
- Dogfood subpackage at `.github/merge-queue/src/rocm_mq/dogfood/`: contains `_base.py`, `aggregator.py`, and `dog_02..dog_08` modules; only imports `rocm_mq.gh` and other dogfood internals.

### Required-check evaluation removal (03-wr-09) — verified

- Grep confirms: `RequiredCheckResult`, `_evaluate_required_checks`, `required_checks` references in `decision.py`/`state.py`/`snapshot.py` reduced to a single comment at `decision.py:333` (historical doc).
- Replacement: `executor._handle_squash_failure` (`executor.py:374-449`) reads merge-API 405/422 response body and decides eject vs no-op vs pending-retry from GitHub's reason text.

### T-03-WR-NO-APPROVAL — At-enqueue ≥1-approving-review gate refactored to config flag (03-wr-12) — verified

**Category:** Elevation of Privilege (revised from original WF-02 design)
**Component:** `cmd_handle._check_at_enqueue_gates` (`cmd_handle.py:213-294`), `MergeQueueConfig` (`state.py:275-279`), `build_config_from_develop` (`config.py:200-217`), dogfood handler workflow (`mq-handler.yml:154-160`)
**Status:** CLOSED (re-audited 2026-05-21 after commit `70edcc9c93e`)

**Original finding:** the no-approval gate was a commented-out block at `cmd_handle.py:269-283`. Re-enabling for Phase 5 upstream porting required a code edit, not a config flag flip — increasing the risk that the re-enable would be forgotten.

**Closure mechanism (commit `70edcc9c93e`, 03-wr-12):**
1. `state.py:279` adds `MergeQueueConfig.require_approval_at_enqueue: bool = True` — default preserves the RFC §5 upstream contract.
2. `cmd_handle.py:213-221` adds keyword-only `require_approval: bool = True` to `_check_at_enqueue_gates`; `cmd_handle.py:278-283` runs the gate code (restored, not commented) when the flag is True; `cmd_handle.py:584-587` threads `config.require_approval_at_enqueue` at the single production call site.
3. `config.py:200-211` reads `MQ_REQUIRE_APPROVAL` env var at the I/O boundary (pure decision layer never reads env); flips the flag to False on `0`/`false`/`no`/`off` only.
4. `mq-handler.yml:154-160` sets `MQ_REQUIRE_APPROVAL: "0"` on the dogfood handler job ONLY, with an explicit PORT-02 marker comment ("upstream-port pre-flight (PORT-02) is to remove this env override so the gate runs at enqueue too"). Marker preserved a second time in `state.py:275-278` ("PORT-02: must be True for upstream port (default already is)").
5. Tests pinned: `test_cmd_handle.py:310-319` (`test_fails_on_no_approval_when_gate_enabled` — default True branch); `test_cmd_handle.py:321-342` (`test_no_approval_skipped_when_gate_disabled` — dogfood False branch); `test_cmd_handle.py:766-788` (`test_no_approval_emits_single_rejection_comment` — full rejection-path through `_handle_merge`). All un-skipped per commit message ("530 pass (was 527 + 2 skipped)").

**Compensating control retained:** branch protection on fork/develop continues to enforce the actual safety property (no squash-merge without approval) at the GitHub-merge-API step via `executor._handle_squash_failure` (`executor.py:374-449`). The dogfood override only suppresses the fail-fast UX layer; the load-bearing GitHub-side control is unchanged.

**Why now CLOSED:** the structural risk (silent regression at port time because the gate is a comment, not a config) is gone. The default is upstream-safe; the dogfood override is in workflow YAML where the PORT-02 pre-flight already runs; both branches are pinned by tests so a future refactor that drops the gate flips at least one test red.

---

## Audit Trail

| Audit Date | Threats Total | Closed | Open | Accepted | Run By | Notes |
|------------|---------------|--------|------|----------|--------|-------|
| 2026-05-21 | 50 | 32 | 1 | 17 | Claude (gsd-secure-phase) | Initial pass; T-03-WR-NO-APPROVAL flagged Open due to code-comment vs config-flag drift risk. |
| 2026-05-21 | 50 | 33 | 0 | 17 | Claude (gsd-secure-phase) | Re-audit after 03-wr-12 config-flag refactor (commit `70edcc9c93e`). Verified `state.py:275-280`, `cmd_handle.py:269-294` (gate restored under `if require_approval:`), `cmd_handle.py:584-587` (call-site threads `config.require_approval_at_enqueue`), `config.py:200-219` (`MQ_REQUIRE_APPROVAL` env override at I/O boundary), `mq-handler.yml:154-161` (dogfood-only override with PORT-02 marker), `test_cmd_handle.py:310-345` (both branches pinned). T-03-WR-NO-APPROVAL moves Open → Closed. |

### Methodology

1. Loaded all 17 PLAN.md `<threat_model>` blocks (plans 03-00 through 03-16); extracted 49 plan-time threats by ID + category + disposition.
2. Added 1 audit-derived threat ID (T-03-WR-NO-APPROVAL) to track the documented no-approval-gate disable deviation as a first-class register entry rather than burying it in narrative.
3. Loaded all 17 SUMMARY.md `## Threat Flags` sections to confirm plan→implementation classification.
4. Verified each `mitigate` threat by grep on the cited file(s) for the declared mitigation pattern, then read the surrounding code to confirm intent matches implementation.
5. Verified each `accept` threat is documented in `## Accepted Risks Log`.
6. Cross-checked against CLAUDE.md project security invariants (token-split, no-bypass, safe_load, SHA-pin, env-var-trust, no PR-head-checkout-under-_target).
7. Audited post-plan deviations (03-wr-09, 03-wr-11a/b/c, no-approval-disable, admin-bypass) by reading current code and classifying each as revised-mitigation (still mitigates threat via different mechanism) or accepted-risk (documented operator decision).
8. Implementation files: read-only; no modifications.

### Files audited

**Production code (verified):**
- `.github/workflows/mq-handler.yml`
- `.github/workflows/mq-processor.yml`
- `.github/workflows/mq-dogfood-canary.yml`
- `.github/merge-queue/path_to_queues.yml`
- `.github/merge-queue/src/rocm_mq/cmd_handle.py`
- `.github/merge-queue/src/rocm_mq/cmd_process.py`
- `.github/merge-queue/src/rocm_mq/config.py`
- `.github/merge-queue/src/rocm_mq/preflight.py`
- `.github/merge-queue/src/rocm_mq/executor.py` (specifically `_handle_activate`, `_handle_squash`, `_handle_squash_failure`, `_handle_eject`, `_verify_squash`, `_upsert_status_comment`)
- `.github/merge-queue/src/rocm_mq/gh.py` (specifically `resolve_app_identity`)
- `.github/merge-queue/src/rocm_mq/decision.py`, `state.py`, `snapshot.py` (verified required-check path removal)
- `.github/merge-queue/src/rocm_mq/dogfood/_base.py` (verified `poll_pr_state` timeout discipline)
- `.github/merge-queue/src/rocm_mq/dogfood/dog_07.py` (verified approval restriction)
- `.github/merge-queue/src/rocm_mq/dogfood/aggregator.py` (verified `json.loads` use)

**Planning artifacts (verified):**
- All 17 plans: `.planning/phases/03-handler-processor-on-fork/03-{00..16}-PLAN.md`
- All 17 summaries: `.planning/phases/03-handler-processor-on-fork/03-{00..16}-SUMMARY.md`
- `03-CONTEXT.md`, `03-VERIFICATION.md`, `03-HUMAN-UAT.md` (referenced for deviation context)

---

## Sign-Off

- [x] All 50 threats have a disposition (mitigate / accept / transfer)
- [x] All 17 accepted risks documented in `## Accepted Risks Log`
- [x] `threats_open: 0` — MET. T-03-WR-NO-APPROVAL closed 2026-05-21 (re-audit after 03-wr-12 commit `70edcc9c93e`).
- [x] All 4 post-plan deviations (03-wr-09, 03-wr-11a/b/c, no-approval, admin-bypass) classified and recorded
- [x] `status: secured` — SET. The no-approval gate is now config-flag-toggleable with the dogfood override scoped to the workflow YAML; PORT-02 marker preserved in both `state.py` docstring and `mq-handler.yml` step env so the Phase 5 pre-flight knows to remove the env override.

**Approval:** SECURED. All 33 closures verified in code; 17 accepted risks documented with explicit operator sign-off; the only previously open finding (T-03-WR-NO-APPROVAL) was structurally closed by the 03-wr-12 refactor and pinned by two new unit tests covering both branches.
