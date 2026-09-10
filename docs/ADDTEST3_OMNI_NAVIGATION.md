# Omni navigation follow-up on addtest3

## Evidence and scope

The historical Qwen Omni run (`102...grounded_checkpoint_r4`) has 125/458
Top-1 hits and 130/458 misses at Top-15. Among 35 Babel samples, 24 rank
Makefile.js first. For babel-15103, the canonical structure contains the
target implementation, but the issue sketch requests Python binding flow
for a JavaScript declaration/shadowing problem. These observations motivate
language gating and bounded package navigation, not additional blind rounds.

This change does not use gold for retrieval and does not change gold extraction,
metrics denominators, or the historical result files. It is not a new online
benchmark result and does not establish an Acc/SL improvement.

## Changes

- Gate heuristic Python binding labels on Python/mypy context or actual Python
  source paths. Apply the gate to issue sketches, heuristic controller decisions,
  parameter closure classification and flow-chain classification. Synthetic query
  expansion alone no longer supplies the controller's language evidence.
- For compiler issues involving syntax, scope, declarations or transformation,
  express AST/scope/transformation obligations through the existing parser flow
  family and CALL/DATA relation. This is not a new compiler analysis engine.
- Add at most six package-entry hints, at most two per matched package, from
  real nonempty `packages/modules/crates/*/src` files. Require an explicit
  package name or multiple distinctive package-name terms in the issue.
  Preserve independent global recall and require later source verification.
- Downweight unrelated build/history/readme files in FastSeed and stably move
  them behind implementation candidates at final ranking. Do not delete them.
  Explicit file mentions and relevant build/documentation tasks are exceptions.
- Record readable-file and entity-file counts, commit and checkout availability
  in repository-index phase logs. Counts are diagnostics, not proof of parser
  completeness or successful mechanism verification.

## Runtime clarification

The existing `scripts/run_omni_clean15_addtest.sh` ALREADY delegates to the shared
runtime wrapper. Use it instead of invoking the low-budget `newtest` entry
directly. No second pipeline or replacement wrapper is introduced.

In a fresh shell, in the addtest3 worktree with its installed environment:

```bash
source .venv/bin/activate
RUN_ID=omni_clean15_addtest3_navigation_v1 RESUME=0 \
  bash scripts/run_omni_clean15_addtest.sh --env-file .env.local
```

Use `.env.mimo1.local` instead for MiMo. Keep credentials local. Check the startup
model, dataset, samples path, effective budgets and runtime preflight. Explicit
exported settings override shared defaults. Never reuse a result directory
across code versions for a fresh comparison.

## Validation and remaining work

Regression coverage includes JS declaration versus Python binding, Python/mypy
preservation, compiler versus build tasks, bounded package hints, declaration
file exclusion, build/documentation exceptions and recall-preserving demotion.

Local verification: 176 passed, 1 skipped when excluding the three live-API
test files (`test_chartjs_llm_evidence.py`, `test_real_api_evidence_integration.py`,
`test_real_llm_dynamic_localization_pipeline.py`). Targeted tests: 24 passed
(overlapping, not additive). Shell syntax and git whitespace checks passed.
No paid model calls or new 458-sample benchmark run were performed.

Before a full 458-sample run, use a separately prepared, fixed SAMPLES subset
covering Babel, webpack, mypy, Netty, dateutil and tqdm. Compare hit/miss pairs,
Acc@1-8, Recall/SL@15 and time, without tuning on individual gold targets.

This patch does not complete Java dispatch resolution, missing-root patch closure,
or an exhaustive candidate-pool coverage audit. It does not prove that all files
are parsed correctly. Package hints can be wrong, and changed seeds can change
deep recall; full File/Module/Function regression remains necessary.
