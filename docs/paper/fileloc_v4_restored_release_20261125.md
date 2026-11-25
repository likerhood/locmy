# Restored fileloc-v4: experimental release, 2026-11-25

Branch: `magnet-kimi-compat-v3-fileloc-v4`.

This release publishes the complete v4 working version restored at the user's
request, including changes which were uncommitted before the operation-chain
experiment. It is not merely the previous HEAD (`5268809`).

## Included and excluded

Included: repository-wide file nominations, explicit-read execution, independent
supplemental nomination review, source-grounded incumbent protection, Qwen
`enable_thinking` request compatibility, regression tests and offline scoring /
same-trajectory audit scripts.

The prior optional review-conflict guard and supplemental-function reservoir
remain available but **default off**. No stable extra benefit is established for
these two options.

The later failed operation-chain experiment has been rolled back. Its operation
chain module, local Tree-sitter overlay, diagnostics flags, runner, tests and
dependency changes are **not included**. Its archived predictions and code
snapshots remain local; no experimental output, private environment or credential
is part of this release.

## Intended complete-v4 configuration

Feature flags remain opt-in for backward compatibility. A checkout alone does not
activate the measured complete-v4 configuration. In addition to the normal model,
dataset, repository and localization configuration, the restored complete-v4
comparisons enabled:

```bash
export MYCODE_FAST_SEED_PLANNER=1
export MYCODE_FAST_SEED_LLM=1
export MYCODE_READ_EXPLICIT_PATHS=1
export MYCODE_JOINT_EVIDENCE_V2=1
export MYCODE_FUNCTION_SOURCE_LEDGER=1
export MYCODE_FILE_FRONT_RERANK=verified_frontier
export MYCODE_FUNCTION_FINAL_POLICY=consensus
export MYCODE_LLM_CANDIDATE_REVIEW=1
export MYCODE_LLM_REVIEW_CANDIDATES=6
export MYCODE_GALA_GLOBAL_FILES=1
export MYCODE_GALA_SEED_GUARD=1
export MYCODE_GALA_VERIFY_NOMINATIONS=1
export MYCODE_GALA_ADDITIVE_REVIEW=1
export MYCODE_GALA_REVIEW_CONFLICT_GUARD=0
export MYCODE_GALA_FUNCTION_RESERVOIR=0
```

For the latest eight-case control-arm comparison, both sides used Qwen
`qwen3.5-397b-a17b`, thinking disabled (`MYCODE_LLM_ENABLE_THINKING=false`), four
rounds, twelve ReAct steps, Top15, controller output limit 2400, function reread
limit 48 (`MYCODE_FUNCTION_REREAD_LIMIT=48`) and an 1800-second task timeout.
These settings are not all implied by the feature flags above. Do not silently
pool runs with different budgets, source or upstream evidence.

The cached SWE development runner's `--phase localize --variant additive`
compares its v1-compatible arm with complete additive v4. It is a development
runner requiring local cached evidence, not a standalone SWE92/Omni296 full-run
command. `--variant conflict` and `--variant reservoir` test other configurations
and must not be substituted for the restored complete-v4 configuration.

## Verification and result boundaries

After restoration, source, scripts, tests and configuration matched the pre-change
snapshot file by file. All 148 targeted regression tests passed.

Earlier six-case SWE paired development run, Qwen, two rounds / four steps:

| File metric | v1-compatible | Additive v4 |
| --- | ---: | ---: |
| A1 | 3/6 | 5/6 |
| A3 | 3/6 | 6/6 |
| A15 | 5/6 | 6/6 |
| REC15 | 54.64% | 66.19% |

This was not a joint file/function acceptance pass. Historical GALA/Qwen had
6/6 A1 on those cases under different evidence and budgets.

In the later eight-case paired operation-chain experiment, the **control arm**
was precisely this restored complete-v4 source. With the larger common budget
above, it obtained:

| Dataset panel | File A1 | File A3–A10 / A15 | File REC15 | Function REC15 |
| --- | ---: | ---: | ---: | ---: |
| SWE, 4 selected cases | 4/4 | 4/4 at each cutoff | 45.27% | 25%, N=4 |
| Omni, 4 selected cases | 4/4 | 4/4 at each cutoff | 100% | 25%, N=2 |

Function denominators exclude empty function gold. These are small, selected
development panels with different budgets from the six-case run. They do not
establish full SWE92 / Omni296 performance, statistical generalization, or strict
superiority over external methods. Publishing this branch is authorized by the
user for subsequent testing, not a claim of joint-validation success.

No server benchmark is automatically started by publishing this release.
