from __future__ import annotations

import re
from pathlib import Path


PROMPT_SOURCES = (
    "src/mycode/evidence/agents/planning_agent.py",
    "src/mycode/evidence/understanding_agent.py",
    "src/mycode/evidence/llm_evidence_agent.py",
    "src/mycode/evidence/tools/vlm_image_reader.py",
)


def test_model_prompt_sources_use_english() -> None:
    root = Path(__file__).resolve().parents[1]
    violations = []
    for relative_path in PROMPT_SOURCES:
        text = (root / relative_path).read_text(encoding="utf-8")
        if re.search(r"[\u3400-\u9fff]", text):
            violations.append(relative_path)
    assert not violations, f"Chinese text remains in model prompt sources: {violations}"


if __name__ == "__main__":
    test_model_prompt_sources_use_english()
    print("PASS English model prompts")
