from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class ModelProtocolProfile:
    name: str
    candidate_review_limit: int = 6
    candidate_review_max_tokens: int = 1200
    final_patch_set_limit: int = 6
    closure_max_rounds: int = 3
    closure_expand_per_round: int = 2
    evidence_plateau_rounds: int = 2
    issue_text_limit: int = 3500
    require_counterevidence: bool = False
    require_verified_mechanism_for_complete: bool = False
    require_owner_contrast: bool = False
    require_entity_snippet_alignment: bool = False
    strict_partial_owner_evidence: bool = False
    verified_entity_owner_boost: float = 0.0


_PROFILES = {
    "default": ModelProtocolProfile(name="default"),
    "qwen": ModelProtocolProfile(name="qwen"),
    "mimo": ModelProtocolProfile(name="mimo"),
    "kimi": ModelProtocolProfile(
        name="kimi",
        candidate_review_limit=4,
        candidate_review_max_tokens=1400,
        final_patch_set_limit=3,
        closure_max_rounds=2,
        closure_expand_per_round=1,
        evidence_plateau_rounds=1,
        issue_text_limit=2400,
        require_counterevidence=True,
        require_verified_mechanism_for_complete=True,
        require_owner_contrast=True,
        require_entity_snippet_alignment=True,
        strict_partial_owner_evidence=True,
        verified_entity_owner_boost=240.0,
    ),
}


def _normalized_profile_name(value: str) -> str:
    lowered = str(value or "").strip().lower()
    aliases = {
        "kimi2.6": "kimi",
        "kimi-k2.6": "kimi",
        "kimi_k2.6": "kimi",
        "qwen3.5": "qwen",
        "qwen3.5-397b-a17b": "qwen",
        "mimo2.6": "mimo",
        "mimo-v2.6": "mimo",
    }
    return aliases.get(lowered, lowered)


def active_model_profile(environ: Mapping[str, str] | None = None) -> ModelProtocolProfile:
    values = os.environ if environ is None else environ
    explicit = _normalized_profile_name(values.get("MYCODE_MODEL_PROFILE", ""))
    if explicit:
        if explicit not in _PROFILES:
            raise ValueError(
                f"Unsupported MYCODE_MODEL_PROFILE={explicit!r}; "
                f"expected one of {', '.join(sorted(_PROFILES))}"
            )
        return _PROFILES[explicit]

    model_identity = " ".join(
        str(values.get(name, ""))
        for name in ("MODEL_API_NAME", "MODEL_NAME", "CONTROLLER_MODEL_API_NAME")
    ).lower()
    for name in ("kimi", "qwen", "mimo"):
        if name in model_identity:
            return _PROFILES[name]
    return _PROFILES["default"]


def profile_int(
    env_name: str,
    profile_field: str,
    fallback: int,
    *,
    minimum: int = 0,
    environ: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if environ is None else environ
    raw = values.get(env_name)
    value = int(raw) if raw not in {None, ""} else int(
        getattr(active_model_profile(values), profile_field, fallback)
    )
    return max(minimum, value)
