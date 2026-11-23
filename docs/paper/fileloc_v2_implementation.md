# File localization v2: read execution and atomic responsibility evidence

## 2026-11-23 experimental handoff (not validated as an upgrade)

User requested publishing this exact tested implementation for independent server testing, including disclosed regressions. Branch: `magnet-kimi-compat-v3-fileloc-v2`, parent `f5163fb`. Runtime source hashes match the completed quality eight-case run; no subsequent algorithm fix is included in this handoff. No merge or force push is intended.

Completed quality run: eight valid outputs, four SWE and four Omni. Frozen v1 versus new hit counts:

| Dataset | Configuration | A1 | A3 | A5 | A8 | A15 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| SWE, N=4 | Frozen v1 | 2 | 3 | 3 | 3 | 3 |
| SWE, N=4 | New quality iteration | 1 | 3 | 4 | 4 | 4 |
| Omni, N=4 | Frozen v1 | 1 | 1 | 2 | 3 | 4 |
| Omni, N=4 | New quality iteration | 1 | 2 | 2 | 2 | 3 |

SWE Calypso22026 improves from a Top15 miss to rank 2 (rank 11 before final permutation), but p5.js3709 falls from v1 rank 3 to 4 (rank 2 before final permutation); Calypso21648 falls from 1 to 3. Omni AssertJ2364 improves from 4 to 2, Prettier15256 falls from 6 to a miss, Statsmodels7380 changes from 14 to 13, Babel15349 remains 1. Function A5 loses two SWE task hits and one Omni task hit; Omni function A15 loses one. These diagnostic samples do not establish full-benchmark improvement.

Mechanism gate also fails: at least one incomplete review group on p5.js/Babel/AssertJ, and final listwise fallback on Calypso21648/Chart10806/Statsmodels/Babel. The coordinator stopped without dispatching 32 cases. This is an experimental checkpoint, not a passing release. No raw large outputs, private environment files or credentials belong in the commit.

Handoff recheck: 139 targeted offline/regression tests passed, 1 skipped on 2026-11-23. The previous broader test selection recorded 159 passed, 1 skipped. This handoff reruns the 13-file targeted selection, not a new model experiment.

### Server testing

All four new switches default off; merely checking out this branch does not activate them. To test the full quality variant, use your server's existing private model environment file and exact dataset assets, then explicitly configure:

```bash
export ENV_FILE=/absolute/private/model.env
export PYTHONPATH="$PWD/src"
export MODEL_API_NAME=mimo-v2.6-pro
export MYCODE_MODEL_PROFILE=mimo
export MYCODE_VLM_IMAGE_TRANSPORT=data_uri
export MYCODE_FILELOC_READ_RECONCILE=1
export MYCODE_FILELOC_OWNER_CHALLENGE=1
export MYCODE_FILELOC_REVIEW_COVERAGE=1
export MYCODE_FILELOC_LISTWISE_RANK=1
export MYCODE_CODE_CONTEXT_LIMIT=18
export MYCODE_CANDIDATE_REVIEW_MAX_TOKENS=4000
export MYCODE_FAST_SEED_PLANNER=1
export MYCODE_FAST_SEED_LLM=1
export MYCODE_LLM_CANDIDATE_REVIEW=1
export MYCODE_JOINT_EVIDENCE_V2=1
export MYCODE_FUNCTION_SOURCE_LEDGER=1
export MYCODE_FUNCTION_REREAD_LIMIT=48
export MYCODE_FILE_FRONT_RERANK=verified_frontier
export MYCODE_FUNCTION_FINAL_POLICY=consensus
export MYCODE_CONCERN_MATCH_V4=0
export MYCODE_TOOL_EVIDENCE_HANDOFF=0
export MYCODE_RESPONSIBILITY_GUARD_V5=0
export MYCODE_WORKING_EVIDENCE_V5=0
export MYCODE_DIRECT_ENTRY_V5=0
```

Use a new output directory and an explicit sample file. Example for a single SWE diagnostic task; replace the two absolute placeholder paths first. This command is documentation only and has not been launched on the server:

```bash
MAGNET_TEST_SAMPLES=/absolute/data/swe92.samples.jsonl
MAGNET_TEST_OUTPUT=/absolute/results/fileloc_v2_quality_new_run
if [ -e "$MAGNET_TEST_OUTPUT" ]; then
  echo "Output exists; select a new output directory" >&2
  exit 1
fi
python newtest/run_clean15_dataset.py \
  --samples "$MAGNET_TEST_SAMPLES" \
  --dataset swebench_multimodal-full-dev-clean15 \
  --output-dir "$MAGNET_TEST_OUTPUT" \
  --instance-id Automattic__wp-calypso-22026 \
  --top-k 15 --dynamic-rounds 6 --max-react-steps 24 --max-tool-rounds 1 \
  --sample-timeout-seconds 3600 --structure-only --no-auto-fetch-repos \
  --use-llm --use-llm-planning --use-llm-controller \
  --use-vlm --download-images --allow-network
```

Prepare exact repository/index assets first; `--no-auto-fetch-repos` intentionally refuses silent repository fetching. For Omni use its frozen samples and dataset label `omnigirl-full-candidates`, e.g. `--instance-id statsmodels__statsmodels-7380`. Removing the instance filter runs the supplied file, so check its task count first. Capture the actual model, flags, commit, inputs and index/gold versions; do not mix this six-round variant with the server v1 two-round/four-step run. Local diagnosis is not authorization or evidence that a server task has already run.

Set all four `MYCODE_FILELOC_*` switches to 0 for the compatibility arm, keeping every other model, budget and dataset setting identical. Also retain the independent clean `f5163fb` checkout if an exact code-baseline comparison is required. Candidate discovery/review can change function results even though the final file-only permutation itself does not modify functions.

## 2026-11-23 quality iteration implementation

New default-off `MYCODE_FILELOC_REVIEW_COVERAGE` reviews the current Top15 in groups of five, supplies bounded source windows, records source-backed responsibility and refutations, and schedules lost supported candidates for investigation without permanently retaining them in Top15. Parse omissions are not rejections. Cross-function support requires actual operation and call source; unrelated imports are not sufficient. Atomic alignment also gates legacy equivalent-mechanism shortcuts.

New default-off `MYCODE_FILELOC_LISTWISE_RANK` runs after closure and function consensus, accepts only a full source-cited file permutation, applies evidence tiers and stable equal-evidence ordering, and repairs at most once before falling back. It never receives or mutates the function list. Both pre/post file snapshots are saved.

Quality budget: MiMo-v2.6-pro, data_uri, six rounds, 24 steps, 18 context files, Top15 output, five supplementary files per round with at most two 80-line windows each, 3600 seconds/task. No aggregate token cap. Only new MAGNET runs; original v1 and external baselines are frozen historical records. Increased-budget differences are not equal-budget causal comparisons.

159 targeted regression tests passed, one skipped, including an offline flags-enabled pipeline check. Fake-review dispatch replay on actual Statsmodels/Prettier traces confirms review eligibility, not new model accuracy. The fixed eight online cases have completed with mixed results and failed the gate, as reported above.

`fileloc_v2_smoke.py --quality --reference-run-dir ...` freezes sources, harness, inputs and reference hashes in a new directory. `continue_fileloc_quality.py` checks all eight execution/coverage/permutation outputs before dispatching the frozen 32-task local panel; only a passing panel proceeds to an independent eight-task repetition. It never launches a server/full benchmark and never auto-commits/pushes.

The scorer reports A1/A2/A3/A4/A5, before/after listwise metrics, nonempty function scores, missing reference records, and frozen external MiMo/Kimi rows. Each dataset must complete sixteen new tasks with at least fifteen available old pairs; A1/A3/A5 each gain two, coverage/function metrics do not regress, and four A1 protections hold. Missing reference results are not zero and are disclosed. Repetition stability and final regressions are required before manual delivery.

## Previous 2026-11-22 iteration (preserved)

Base: clean `f5163fbcd05d1ccf9084e679e2b9bb3055ca82b0`. Branch: `magnet-kimi-compat-v3-fileloc-v2`.

Two default-off flags:

- `MYCODE_FILELOC_READ_RECONCILE`: exact/unique same-directory-stem indexed reads, separate exact symbol definition reads and bounded request-to-verification handoff. No extra file score or shared entity query mutation.
- `MYCODE_FILELOC_OWNER_CHALLENGE`: same-region entity/quote validation, import-only evidence rejection, explicit file-only anchors, and at most one bounded owner/rival/observed-delegate contrast in the next existing round. No extra model call or round.

The false-positive regression is grounded in the saved p5.js3709 trace: a textureMode snippet was accepted for the texture entity. Relatedness and independent same-file checks are insufficient evidence of change responsibility. These checks do not claim semantic proof of correctness or guaranteed online accuracy.

Do not import the failed R3 working-packet rotation or historical candidate restoration. Preserve existing v1 seed, function consensus and final file permutation. Online function coverage must still be measured because changed reads/reviews can indirectly change discovery.

Verification: 149 targeted tests passed, 1 skipped. The earlier 8-task three-arm online experiment completed (24 task runs); full v2 did not meet joint file/function nonregression. No validated upgrade claim.

Use `scripts/fileloc_v2_smoke.py` with an explicit clean baseline worktree, the previously frozen 32-task manifest and a NEW run directory. Default sentinel stage runs actual v1, read-only, and both flags; full stage runs 32 v1/improved pairs. Same MiMo-v2.6-pro, data_uri, 4 rounds, 12 steps, Top15, 1800 seconds. Source/input hashes and independent source snapshots are saved. Do not modify runtime while a run is active.

`scripts/score_fileloc_v2.py` is post-hoc only, handles Omni typed entity IDs according to the unified reference convention, excludes invalid/incomplete executions, and separately evaluates each dataset. Full release requires 16 pairs per dataset, file A1/A3 gain, nonregressing file/function coverage metrics, four protected A1 successes, and independent sentinel repetition. No automatic commit/push is configured.
