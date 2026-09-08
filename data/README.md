# Clean15 Evaluation Inputs

This directory contains the normalized JSONL inputs used by the MAGNET evaluation launchers.

| Dataset | File | Samples | SHA-256 |
|---|---|---:|---|
| SWE-bench Multimodal Clean15 | `swebench_multimodal-full-dev.clean15.samples.jsonl` | 92 | `f792c513c07214979168cb778fcbc63986d65b39768f7121b2527b2afedf9c6b` |
| OmniGIRL Clean15 | `omnigirl-full-candidates.clean15.v458.samples.jsonl` | 458 | `dfb479d8731e5ebcea0012cba5cb90e003b955a2d0dbc5d93fc394c641694847` |

The launchers prefer these repository-local files and fall back to an external `clean_subsets_new/` directory. Evaluation code must preserve the benchmark policy documented in `docs/INSTALLATION.md` and must not expose gold fields to localization prompts.
