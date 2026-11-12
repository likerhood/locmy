from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from mycode.utils.model_profile import active_model_profile, profile_int


ROOT = Path(__file__).resolve().parents[1]


def test_kimi_profile_is_inferred_from_api_model() -> None:
    profile = active_model_profile({"MODEL_API_NAME": "kimi-k2.6"})

    assert profile.name == "kimi"
    assert profile.candidate_review_limit == 4
    assert profile.final_patch_set_limit == 3
    assert profile.require_counterevidence is True
    assert profile.require_verified_mechanism_for_complete is True
    assert profile.require_owner_contrast is True
    assert profile.require_entity_snippet_alignment is True
    assert profile.strict_partial_owner_evidence is True
    assert profile.verified_entity_owner_boost == 240.0


def test_explicit_profile_overrides_model_name() -> None:
    profile = active_model_profile(
        {"MYCODE_MODEL_PROFILE": "qwen", "MODEL_API_NAME": "kimi-k2.6"}
    )

    assert profile.name == "qwen"


def test_profile_default_can_be_overridden_by_explicit_setting() -> None:
    values = {
        "MYCODE_MODEL_PROFILE": "kimi",
        "MYCODE_LLM_REVIEW_CANDIDATES": "7",
    }

    assert profile_int(
        "MYCODE_LLM_REVIEW_CANDIDATES",
        "candidate_review_limit",
        6,
        minimum=3,
        environ=values,
    ) == 7


def test_unknown_explicit_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported MYCODE_MODEL_PROFILE"):
        active_model_profile({"MYCODE_MODEL_PROFILE": "invented"})


def test_omni_launcher_reports_inferred_kimi_protocol(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.kimi"
    env_file.write_text(
        "BASE_URL=https://example.invalid/v1\n"
        "API_KEY=test-only\n"
        "MODEL_NAME=Kimi2.6\n"
        "MODEL_API_NAME=kimi-k2.6\n"
    )
    env = os.environ.copy()
    env.pop("MYCODE_MODEL_PROFILE", None)

    completed = subprocess.run(
        [
            "bash",
            str(ROOT / "newtest/run_omni_clean15_agent_full.sh"),
            "--env-file",
            str(env_file),
            "--print-model-config",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "MAGNET protocol profile: kimi" in completed.stdout
    assert "test-only" not in completed.stdout
