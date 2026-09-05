# Evidence-Aware Dynamic Localization Prototype

This directory contains the first prototype for the new localization framework.

The current implementation now covers the first runnable vertical slice:

- load Clean15 benchmark samples
- extract issue URLs and image links from `problem_statement` only
- classify URL roles and image evidence
- build a structured evidence sketch per sample
- build an Evidence Understanding Agent packet with URL/reproduction/image plans
- execute optional URL/playground/browser/image tools
- optionally ask an LLM to interpret the structured evidence packet
- build a local repository index from cloned repos and `repo_structures`
- run concern + entity + light graph + flow-query dynamic localization
- evaluate file-level `acc@k`, `recall@15`, `MRR@15`, and `MAP@15`

The current evidence layer intentionally ignores auxiliary dataset fields such
as `hints_text`, `web_urls`, `website links`, `image_urls`, and `image_assets`.
This keeps the localization input aligned with the issue body used by the
baseline prompt and avoids accidental leakage from structured metadata.

## Quick Start

```bash
cd /home/like/locCode/alltry/mycode

PYTHONPATH=src python3 scripts/build_clean15_evidence.py \
  --dataset swe \
  --samples /home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl \
  --output outputs/evidence/swe_clean15/evidence.jsonl \
  --summary outputs/evidence/swe_clean15/summary.json

PYTHONPATH=src python3 scripts/build_clean15_evidence.py \
  --dataset omni \
  --samples /home/like/locCode/clean_subsets_new/omnigirl-full-candidates.clean15.v458.samples.jsonl \
  --output outputs/evidence/omni_clean15/evidence.jsonl \
  --summary outputs/evidence/omni_clean15/summary.json
```

Inspect only URL or image distributions:

```bash
PYTHONPATH=src python3 scripts/inspect_url_distribution.py --samples <samples.jsonl>
PYTHONPATH=src python3 scripts/inspect_image_distribution.py --samples <samples.jsonl>
```

Build Evidence Understanding Agent packets:

```bash
PYTHONPATH=src python3 scripts/build_evidence_packets.py \
  --dataset swe_clean15 \
  --samples /home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl \
  --output outputs/evidence_packets/swe_clean15/evidence_packets.jsonl \
  --summary outputs/evidence_packets/swe_clean15/summary.json \
  --print-summary
```

Run the Chart.js LLM evidence test:

```bash
cp .env.example .env.local
# Fill BASE_URL/API_KEY/MODEL_NAME/MODEL_API_NAME in .env.local.

PYTHONPATH=src python3 tests/test_chartjs_llm_evidence.py
```

Run one end-to-end localization case without network or LLM:

```bash
PYTHONPATH=src python3 scripts/run_localization_pipeline.py \
  --samples /home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl \
  --dataset swe_clean15 \
  --instance-id chartjs__Chart.js-10301 \
  --output outputs/tests/chartjs_10301_localization.json \
  --top-k 15
```

For `chartjs__Chart.js-10301`, this offline run ranks
`src/plugins/plugin.legend.js` at Top1 and reports `acc@1=1.0`,
`acc@5=1.0`, `recall@15=1.0`, `MRR@15=1.0`.

## Evidence Understanding Agent

The packet builder is a localization-specific preprocessing agent. It does not
predict target files directly. Instead, it turns multimodal issue evidence into
search guidance for the later dynamic localization agent.

- URL Inspector parses GitHub code, PR/commit/diff, docs, product routes, and
  playground URLs. GitHub code URLs are evidence seeds, not direct gold targets.
- Reproduction Extractor parses playground/reproduction URLs such as
  `mypy-play.net`, Prettier playground, TypeScript playground, CodePen,
  JSFiddle, CodeSandbox, and StackBlitz into input/config/semantic queries.
- Browser Snapshot optionally fetches docs/discussion pages and extracts title,
  headings, code blocks, and semantic terms. It is disabled by default.
- Image Inspector maps issue images to visual symptom types and likely program
  layers. By default it only verifies/cache-checks assets; with `--use-vlm`
  it sends processable images to the configured OpenAI-compatible VLM and
  turns the visual analysis into downstream search queries.
- LLM Evidence Agent consumes the structured packet and produces an issue-level
  interpretation, evidence roles, reproduction understanding, visual symptom
  summary, graph navigation plan, and missing tools. This stage does not change
  the benchmark label; it only improves the downstream search plan.

For `chartjs__Chart.js-10301`, the offline parser identifies:

- `https://www.chartjs.org/docs/latest/samples/legend/events.html` as a
  `docs_sample` reproduction entry.
- `https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx`
  as a CodeSandbox reproduction entry with `file=/src/App.tsx`.
- Three `user-images.githubusercontent.com` PNGs as visual evidence.

With the LLM step enabled, the test asks the model to explain how these
evidence items should guide search toward legend event handling, hover state,
and event dispatch logic.

## Dynamic Localization Slice

The current dynamic layer is intentionally lightweight but complete enough for
end-to-end experiments:

- `repo_index` loads local source files and benchmark `repo_structures`.
- `dynamic_retrieval` merges issue evidence queries, reproduction queries,
  VLM queries, concern expansions, and flow terms.
- `flow_analysis` extracts state/event/API terms such as `onLeave`, `onHover`,
  `redirect_to`, `client_id`, and URL/config parameters.
- `repo_index.light_graph` adds same-directory, import/export-like, and symbol
  reference edges. This is a navigation graph, not a full call graph.
- `evaluation` reports ranking metrics against Clean15 gold files. Gold is not
  used before evaluation.

Useful switches:

- `--allow-network`: permit docs/playground/image fetching.
- `--allow-browser`: permit Playwright page observation when available.
- `--download-images`: cache image URLs locally.
- `--use-vlm`: run configured VLM on cached/downloaded images.
- `--use-llm --use-llm-planning`: let the evidence agent call the configured
  LLM for planning and final evidence synthesis.

## Design Boundary

The project is organized by concrete functions, not by baseline names:

- `data`: benchmark loading and sample normalization
- `evidence`: issue, URL, and image evidence understanding
- `repo_index`: repository structures and searchable indexes
- `dynamic_retrieval`: concern/call/used-by retrieval
- `flow_analysis`: data/state/parameter flow verification
- `agent`: dynamic localization loop
- `evaluation`: Clean15 metrics and failure analysis
