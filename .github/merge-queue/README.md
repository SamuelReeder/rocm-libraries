## rocm_mq — Federated Merge Queue (Phase 1: Pure Decision Layer)

This directory contains the `rocm_mq` Python package implementing the pure decision layer
of the federated merge queue described in `docs/rfcs/0001_MergeQueue.md`.

Phase 5 (porting prep, `PORT-04`) will flesh out this README with full operational
documentation. For now, see the RFC for design rationale and the `.planning/` directory
for implementation context.

### Installation (development)

```bash
# From the repository root:
pip install -e .github/merge-queue[dev]

# Verify:
python -c "import rocm_mq"
```

> **Note:** `uv` is the preferred package manager per the locked tech stack (STACK.md),
> but is not currently available on this dev box. Plain `pip install -e` is used instead.
> CI workflows will specify `actions/setup-python` + `pip` or adopt `uv sync --frozen`
> when `uv.lock` is added in a later phase.

### Running tests

```bash
cd .github/merge-queue
pytest tests/
```
