# addtest4: bounded responsibility selection

## Scope

Base commit: `9cef95a` on addtest3. This worktree is separate from addtest3;
its files, experiment results and uncommitted changes remain untouched.

This is the first implementation batch, not the entire long-term plan.
Anchored variable slicing, incremental graph expansion and isolated dynamic
reproduction are intentionally deferred until the ranking changes are evaluated.
No online benchmark score is claimed for addtest4.

## Changes

1. HeadSelector requires a reviewed `patch_target`, source grounding, supported
   quote and entity, explicit reviewer flow support, a nonempty edit mechanism,
   and no unresolved counterevidence. Raw call/graph scores alone no longer
   substitute for that responsibility evidence. Existing source eligibility,
   read checks and the replacement margin remain in force. The existing fallback
   for a blocked/non-source incumbent remains available. Explicit documentation
   or build-edit requests are respected by the fallback gate as well as the
   initial eligibility check.
2. Seed restoration preserves already-supported responsibility candidates from
   the original top six before inserting up to two entry seeds. It does not use
   gold or repository-specific case names. Missing flow alone does not reject a
   seed, and entries without source or with generated-file roles remain excluded.
3. The English reviewer prompt distinguishes faulty operations and missing-check
   insertion points from wrappers and consumers. The JSON schema stays compatible.
4. Head comparison and seed-guard diagnostics are recorded. These are gates over
   existing validated fields, not a new runtime proof or a separate LLM judge.

The head pass preserves all other candidates' relative order. Seed restoration
can change the prefix and still truncates to top_k, so deep recall must be checked.
No extra LLM call, dynamic round or flow backend is introduced.

## Ablation controls

Defaults:

```ini
MYCODE_HEAD_REQUIRE_RESPONSIBILITY=1
MYCODE_SEED_RESPONSIBILITY_GUARD=1
MYCODE_HEAD_REPLACEMENT_MARGIN=3.0
```

Use each new flag at 0/1 independently, with the same model and budgets. Turning
both off restores the previous two ranking policies, but does not revert the
English prompt change; use the addtest3 commit for an exact code baseline.
Keep distinct RUN_ID values and record runtime_capabilities.json for each run.

## Running

After installing the project's existing dependencies in an environment for this
worktree, run from its root:

```bash
RUN_ID=swe_clean15_addtest4_v1 RESUME=0 \
  bash scripts/run_swe_clean15_addtest4.sh --env-file /absolute/path/to/model.env
```

The launcher forwards all arguments to the existing validated model-profile
launcher. It uses an addtest4 timestamped RUN_ID by default and does not copy
credentials or reuse prior experiment results automatically. If another process
uses the model profile, do not modify that shared file during the experiment.

## Verification and next steps

Local verification (2026-09-12): focused tests 69 passed; full offline regression
192 passed, 1 skipped in 63.20 seconds. The three real-API test modules were
excluded. Tests used the existing environment read-only with PYTHONPATH pointing
to this worktree's src, not the other worktree's installed editable package.
The new launcher passed bash -n; git diff --check passed. No online localization
benchmark or API evaluation was started, and no accuracy gain is yet measured.

Focused regression tests cover missing evidence, navigation-only roles,
counterevidence, legacy switches, verified incumbents, weak seeds, recall-set
preservation, tail order and input non-mutation. Existing mocked mechanism tests
now explicitly include reviewer flow support and an edit mechanism.
Documentation-task regressions also check that source fallback does not override
an explicitly permitted documentation head without responsibility evidence.

Still pending: targeted pairwise source reading, anchored variable-level slicing,
incremental graph boundary completion, and measured benchmark ablations. Existing
reviewer flow support is not proof of a variable-level causal dependency. A strict
gate can retain a wrong incumbent when the correct challenger lacks evidence.

Before accepting a score improvement, evaluate newly gained and lost samples at
File Acc@1/3/6/8/15, Module/Function Acc@15, SL/REC, elapsed time and tokens.
Audit wrong head replacements and strong-candidate displacement separately.
Do not choose rules using online gold labels or advertise synthetic unit tests
as successful repairs of the historical failure cases.
