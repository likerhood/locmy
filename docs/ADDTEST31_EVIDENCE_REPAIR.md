# addtest31: document roles and missing review context

Base: addtest3 at 9cef95a. This is an isolated worktree, not an addtest4 merge.

## Scope

- Markdown, MDX, RST and AsciiDoc are navigation material by default. Explicit
  documentation/release-note edit tasks keep their exception. Candidate recall
  is retained; implementation code under lib is not categorically blocked.
- Before existing candidate review, fill at most two missing contexts using
  observed candidate entity names. Existing snippet contexts are unchanged.
  The existing context capacity and reviewer candidate limit are respected.
  Missing files or unmatched anchors do not produce fabricated snippets.
- An unlikely verdict supported only by no_source (and optional no_entity or
  no_flow), without a supported quote, becomes insufficient_evidence. Its review
  adjustment is neutral, not a patch-target promotion. Real counterevidence
  keeps the original rejection. Other downstream evidence gates remain.
- No new LLM call or increased round limit. Additional local reads have bounded
  candidate counts. Set MYCODE_REVIEW_BACKFILL_LIMIT=0 for the backfill ablation.
- Broader function ranking and graph changes are deliberately deferred.

## Verification

Offline tests: 186 passed, 1 skipped. Three real-API modules were excluded.
Shell syntax and git diff checks passed. No online experiment was launched.

Frozen-candidate diagnostic, not new model results: stable document demotion on
the 458 Omni MiMo addtest3 traces changes file hit counts as follows:

| k | Before | After | Gains | Losses |
|---|---:|---:|---:|---:|
| 1 | 222 | 233 | 11 | 0 |
| 3 | 287 | 302 | 15 | 0 |
| 8 | 335 | 337 | 2 | 0 |
| 15 | 358 | 358 | 0 | 0 |

The same replay leaves SWE addtest3 Qwen and MiMo hit counts at these k values
unchanged. This does not validate backfill or predict online search trajectories.
Gold is used only to evaluate the replay, never to select or reorder candidates.

## Agent and entity evidence follow-up

- Entity flow support deduplicates identical structural traces across repeated
  observations. Confidence updates retain the strongest observation instead of
  creating another vote; distinct structures retain their existing contribution.
- Function review boosts now require a verified source quote and use only
  `supported_entities`, not all model-proposed entity names.
- Syntax and instruction boilerplate such as `return`, `default`, `function`
  and `should` no longer earns the entity semantic-match bonus. Literal search
  remains available; operation-specific terms still contribute.
- Tests cover duplicate observations, confidence-order invariance, distinct
  traces, generic terms and quote/entity gating. These are behavioral tests,
  not an online accuracy estimate.

This does not yet repair language-specific flow applicability, package recall,
or equivalent traces with different ordering/serialization. It also does not
change file-level flow aggregation or guarantee progress on every agent round.
The document-only replay above does not evaluate these additional changes.

## Running

Install the existing project dependencies in this worktree's environment. Supply
an explicit model profile (secrets are not copied into the new worktree):

```bash
RUN_ID=omni_clean15_addtest31_mimo_v1 RESUME=0 \
  bash scripts/run_omni_clean15_addtest31.sh --env-file /absolute/path/to/model.env
```

The wrapper defaults to deep mode, 12 dynamic rounds, 6 tool rounds and 10 ReAct
steps. For a controlled comparison, also carry over the previous run's graph,
read and model budgets; these four settings alone do not establish parity.
It delegates explicit model-profile validation to the existing full runner.
The new run ID avoids resuming addtest3 results. Test held-out diagnostic samples
before the full dataset; do not claim a baseline win from offline replay.
