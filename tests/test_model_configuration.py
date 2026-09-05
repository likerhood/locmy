from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mycode.evidence.tools import llm_client
from mycode.evidence.tools.llm_client import model_for_stage
from mycode.utils.env import llm_config_from_env

ROOT = Path(__file__).resolve().parents[1]


def test_stage_model_override_falls_back_to_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_NAME", "display-model")
    monkeypatch.setenv("MODEL_API_NAME", "global-api-model")
    monkeypatch.delenv("PLANNING_MODEL_API_NAME", raising=False)
    monkeypatch.setenv("CONTROLLER_MODEL_API_NAME", "controller-api-model")

    assert model_for_stage("planning") == "global-api-model"
    assert model_for_stage("controller") == "controller-api-model"
    assert model_for_stage("unknown") == "global-api-model"


def test_python_config_honors_custom_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.python-profile"
    env_file.write_text(
        "BASE_URL=https://python-profile.example/v1\n"
        "API_KEY=python-profile-secret\n"
        "MODEL_API_NAME=python-profile-model\n",
        encoding="utf-8",
    )
    original = os.environ.copy()
    try:
        for name in ("BASE_URL", "API_KEY", "MODEL_NAME", "MODEL_API_NAME"):
            os.environ.pop(name, None)
        os.environ["LOAD_ENV_FILE"] = "1"
        os.environ["ENV_FILE"] = str(env_file)

        config = llm_config_from_env()

        assert config["base_url"] == "https://python-profile.example/v1"
        assert config["api_key"] == "python-profile-secret"
        assert config["model_api_name"] == "python-profile-model"
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_python_config_can_disable_env_loading() -> None:
    original = os.environ.copy()
    try:
        for name in ("BASE_URL", "API_KEY", "MODEL_NAME", "MODEL_API_NAME", "ENV_FILE"):
            os.environ.pop(name, None)
        os.environ["LOAD_ENV_FILE"] = "0"

        config = llm_config_from_env()

        assert config == {
            "base_url": "",
            "api_key": "",
            "model_name": "",
            "model_api_name": "",
        }
    finally:
        os.environ.clear()
        os.environ.update(original)


@pytest.mark.parametrize(
    "launcher",
    [
        "newtest/run_swe_clean15_agent_full.sh",
        "newtest/run_omni_clean15_agent_full.sh",
    ],
)
def test_model_cli_overrides_selected_env_file(tmp_path: Path, launcher: str) -> None:
    env_file = tmp_path / ".env.profile"
    env_file.write_text(
        "\n".join(
            [
                "BASE_URL=https://profile.example/v1",
                "API_KEY=profile-secret",
                "MODEL_NAME=profile-label",
                "MODEL_API_NAME=profile-api-model",
                "CONTROLLER_MODEL_API_NAME=profile-controller-model",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in (
        "BASE_URL",
        "API_KEY",
        "MODEL_NAME",
        "MODEL_API_NAME",
        "CONTROLLER_MODEL_API_NAME",
        "ENV_FILE",
        "LOAD_ENV_FILE",
        "PRINT_MODEL_CONFIG",
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [
            "bash",
            str(ROOT / launcher),
            "--env-file",
            str(env_file),
            "--model",
            "cli-api-model",
            "--print-model-config",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Base URL: https://profile.example/v1" in completed.stdout
    assert "Model label: cli-api-model" in completed.stdout
    assert "Model API name: cli-api-model" in completed.stdout
    assert "Controller model: profile-controller-model" in completed.stdout
    assert "profile-secret" not in completed.stdout


def test_inherited_environment_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.profile"
    env_file.write_text(
        "MODEL_NAME=file-label\nMODEL_API_NAME=file-api-model\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "MODEL_NAME": "environment-label",
            "MODEL_API_NAME": "environment-api-model",
        }
    )

    completed = subprocess.run(
        [
            "bash",
            str(ROOT / "newtest/run_swe_clean15_agent_full.sh"),
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

    assert "Model label: environment-label" in completed.stdout
    assert "Model API name: environment-api-model" in completed.stdout


def test_chat_completion_supports_mimo_thinking_and_token_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {},
            }

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int) -> _Response:
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setenv("BASE_URL", "https://mimo.example/v1")
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("MODEL_API_NAME", "mimo-v2.5")
    monkeypatch.setenv("MYCODE_LLM_THINKING", "disabled")
    monkeypatch.setenv("MYCODE_LLM_TOKEN_FIELD", "max_completion_tokens")
    monkeypatch.setenv("MYCODE_LLM_MIN_COMPLETION_TOKENS", "1400")
    monkeypatch.setenv("MYCODE_LLM_RETRIES", "1")
    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    llm_client.chat_completion(
        [{"role": "user", "content": "Return JSON."}],
        max_tokens=900,
    )

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["max_completion_tokens"] == 1400
    assert "max_tokens" not in payload
    assert payload["thinking"] == {"type": "disabled"}
