# v4: repository-wide semantic file nominations on fileloc-v1

Further experiments on 2026-11-25 (preserve the chronological results below):
`--variant conflict` compares the complete additive v4 against an optional
grounded-review conflict guard. Six valid pairs in `_05_` gave File A1
5/6 -> 6/6, A2--A10 and A15 6/6 on both sides, unchanged File REC15 65.06,
but Function REC15 52.08 -> 47.92. None of these fresh trajectories triggered
the conflict guard, so these numerical changes are NOT evidence of a causal
accuracy gain from that guard. Same-log replay of `_04_` does demonstrate
that it removes contradictory certification from the Legend candidate.

`MYCODE_GALA_REVIEW_CONFLICT_GUARD=0` defaults off: retain both source-backed
review observations when verified positive and negative conclusions conflict;
keep the candidate unresolved rather than certify or delete it by confidence.

`MYCODE_GALA_FUNCTION_RESERVOIR=0` defaults off: collect actual-window-overlap
supplemental entities separately, reread their exact indexed source, and allow
only existing source/issue plus exact-entity-review or entity-flow eligibility
into the bounded tail reservoir. No extra round votes; no file-rank promotion
of functions; no direct replacement of the consensus top three. Its separate
reread limit defaults to 48. It is not a correctness certificate.

148 targeted tests pass, including a nonempty supplemental-entity end-to-end
test. `_06_` was invalidated by an extra list nesting in final ledger assembly;
the error is fixed and original partial outputs are retained. New `_07_` paired
tests completed three valid pairs: File A1--A10 remained 3/3, File REC15
76.19 -> 85.71, Function REC15 unchanged 54.17, Function A2 2/3 -> 1/3.
However same-trajectory `ablate_gala_function_reservoir.py` reproduced the
saved final functions exactly and found identical enabled/disabled function
lists on all three cases. Thus no extra accuracy benefit from the reservoir
is established. Arc source candidates were saved but lacked independent
entity-level flow/operation support. Keep both new flags off by default;
this is not a joint-validation pass. Do not combine best outputs across runs.

Latest follow-up (2026-11-25): real Qwen final-localization development pairs
have now run; the earlier MiMo quota block below is historical, not the latest
test state. On six fixed SWE diagnostic cases, the latest additive variant has
File A1 3/6 -> 5/6, A3 3/6 -> 6/6, A15 5/6 -> 6/6, REC15 54.64 -> 66.19.
However Chart.js-9678 File A1 regresses (1 -> 2), and Calypso-21648 Function
A1 regresses (1 -> 2). This is NOT a joint-validation pass or a full benchmark.
Omni has not been evaluated in this round. Historical GALA Qwen still has
6/6 File A1 on these cases under different evidence and budgets.

`MYCODE_GALA_VERIFY_NOMINATIONS=1` gives up to three selected global
hypotheses actual read/flow/review opportunities. Its first implementation
reallocated existing slots and hurt function coverage. The additional opt-in
`MYCODE_GALA_ADDITIVE_REVIEW=1` instead preserves the primary investigation,
adds a separate at-most-six-file review batch, and supplies primary snapshots
to function ranking. It does not guarantee full search independence: initial
seeds and cross-round state still interact. All four GALA flags default off;
use the paired script's `--variant additive` to test the latest combination.

Main and supplemental statuses are audited separately. Follow-up queries,
missing evidence and continue-search signals survive batch merging. A first
attempt that omitted this metadata was interrupted and kept in `_03_`
directories, excluded from the complete `_04_` comparison.

Complete latest comparison: `result/gala_v4_qwen35_additive_20261125_04_summary.json`.
It includes per-case stages and frozen historical method results. Earlier
experiments and source snapshots remain in separate result directories.

Base: `magnet-kimi-compat-v3-fileloc-v1` at `f5163fb`.
Branch: `magnet-kimi-compat-v3-fileloc-v4`.

## Evidence and design

The frozen SWE92 audit shows that the v1 lexical Top10 pool covers 66/92
tasks, while GALA MiMo's repository-tree Top5 pool covers 78/92. GALA's
three seeds cover 71/92; v1's three active seeds cover 59/92. This motivates
an independent discovery channel outside v1's lexical shortlist.

GALA's graph alignment is not uniformly beneficial: seed A1 is 54/92 and
final A1 51/92 (MiMo). Forty-five GALA tasks have empty function pools.
Therefore this change imports repository-tree discovery, not confidence
promotion from semantic graph alignment. Historical GALA uses a different
model/budget; these observations are not a causal algorithm comparison.

## Implementation

Set `MYCODE_GALA_GLOBAL_FILES=1` to enable the new channel (default off).
The selector presents exact current-index paths with the original issue and
whitelisted existing VLM observations. It nominates up to five files independently
of the lexical Top10. The old candidates remain in the joint pool; the existing
seed selection model selects at most three active seeds.

For large repositories, the selector presents all current directory branches
with numeric IDs and traverses selected directories, starting at up to three
path components. It uses at most three calls and a 48000-character tree budget.
No alphabetical prefix of the repository is silently substituted for coverage.
Budget exhaustion, invalid paths and request failures are explicit statuses.

Every nomination is labeled `global_semantic_prior`, not source proof. Unknown
paths are rejected; existing source verification and closure still run. No
task-ID or gold-path rules were added. Final Top15 and function lists are not
directly reordered by this new selector.

The global channel's failure preserves the original seed-selection prompt and
pool. Its degraded status remains visible in `search_trace.fast_seed_planner`.
This is a recovery path, not evidence that the model stage executed successfully.

## Validation

Run related tests from this worktree:

```bash
PYTHONPATH=src /path/to/python -m pytest -q \
  tests/test_gala_global_files.py tests/test_url_browser_seed_pipeline.py \
  tests/test_omni_navigation_policy.py tests/test_seed_frontier.py \
  tests/test_file_frontier_v1.py tests/test_joint_evidence_v2.py
```

Development model checks reuse saved v1 upstream task-time evidence and run
new seed selection or localization calls. They exclude evaluation fields from
agent input and score only after outputs are saved. They are cached-evidence
development runs, not independent full-benchmark results.

```bash
/path/to/python scripts/gala_v4_cached_evidence_smoke.py \
  --workspace /home/like/locCode \
  --env-file /private/model.env \
  --run-dir /new/output/directory \
  --phase seed \
  --instance-id chartjs__Chart.js-10806 \
  --instance-id diegomura__react-pdf-1306 \
  --instance-id markedjs__marked-1262
```

Use `--phase localize` to run the full localization stage with identical
two-round/four-step/Top15 budgets in both arms. Source hashes, canonical
structure hashes, selected paths, prompts and model responses are saved.
Keep the source fixed across a run and use a new directory after any edit.

## Current assessment (2026-11-25)

Initial offline tests: 43 passed. Initial seed calls move the correct file to position 1 for Chart.js-10806
and react-pdf-1306. Calypso-21648 exposed a directory-output format issue,
subsequently addressed with numeric IDs. The marked protection pair encountered
HTTP 429, classified by the provider message as quota/balance limitation, and
must not be counted as a valid model result. A model request failure
with deterministic fallback is not successful validation.

Full localization improvement and function/Top15 protection remain unverified.
No full SWE92/Omni296 run or remote push is part of this implementation record.

## Follow-up tests and seed guard (2026-11-25)

`MYCODE_GALA_SEED_GUARD=1` enables an independent conservative guard after
seed insertion. It only restores the original incumbent head ahead of
unverified seed insertions when the existing review records patch-target
responsibility, grounded mechanism evidence and a supported source quote.
It does not automatically elevate verified labels from ranks 2/3 or below.
The step permutes the already bounded Top15 set and never receives functions.
Default is off; global nomination and this guard can be tested separately.

An earlier, broader guard on the original Top3 regressed saved v1 A3 by one
task (react-pdf-1552). That iteration is retained as a failure. Narrowing the
guard to the original head avoids changing the old agent's internal order.
Frozen-stage replay across both historical SWE92 runs (92 cases per run)
shows no changes in A1/A3/A5/A8/A15, SL or REC. There is no accuracy gain to
claim from this guard. Downstream head/closure behavior still needs online tests.

Current related offline tests: 49 passed. Exact replay artifacts live under
`result/gala_v4_guard_replay_20261125_01` and `_02`.

The new model preflight aborts before task dispatch on provider failure and
writes sanitized `provider_probe.json` and `BLOCKED.json`. It must not count
deterministic fallback as successful protection. The current final-localization
attempt is blocked by HTTP 429 quota/balance limitation, with zero task runs.

The paired runner accepts `--variant global`, `guard` or `joint` (default
global), and `--prepare-only` freezes inputs/source without API calls.
Four joint-localization inputs and their code/structure fingerprints are
prepared in `result/gala_v4_joint_prepared_20261125_01`.
