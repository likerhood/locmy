from __future__ import annotations

import json
import os
import threading
import time
from itertools import count
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests

from mycode.utils.env import llm_config_from_env


class LLMClientError(RuntimeError):
    pass


_STAGE_MODEL_ENV = {
    "planning": "PLANNING_MODEL_API_NAME",
    "evidence": "EVIDENCE_MODEL_API_NAME",
    "controller": "CONTROLLER_MODEL_API_NAME",
    "vlm": "VLM_MODEL_API_NAME",
}


def model_for_stage(stage: str) -> str:
    """Resolve a stage override, falling back to the global API model id."""

    config = llm_config_from_env()
    env_name = _STAGE_MODEL_ENV.get(stage.strip().lower())
    if env_name:
        override = os.environ.get(env_name, "").strip()
        if override:
            return override
    return config["model_api_name"]


_REQUEST_COUNTER = count(1)
_PRINT_LOCK = threading.Lock()


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _completion_request_options(max_tokens: int) -> tuple[str, int, dict[str, Any]]:
    """Resolve provider-specific completion controls from optional environment settings."""

    token_field = os.environ.get("MYCODE_LLM_TOKEN_FIELD", "max_tokens").strip()
    if token_field not in {"max_tokens", "max_completion_tokens"}:
        token_field = "max_tokens"
    minimum = _env_int("MYCODE_LLM_MIN_COMPLETION_TOKENS", 0)
    effective_limit = max(1, max(int(max_tokens), minimum))

    extra: dict[str, Any] = {}
    thinking = os.environ.get("MYCODE_LLM_THINKING", "").strip().lower()
    if thinking in {"enabled", "disabled"}:
        extra["thinking"] = {"type": thinking}
    response_format = os.environ.get("MYCODE_LLM_RESPONSE_FORMAT", "").strip().lower()
    if response_format in {"json_object", "text"}:
        extra["response_format"] = {"type": response_format}
    return token_field, effective_limit, extra


def _safe_endpoint(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return base_url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _message_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(item)) for item in content)
        elif content is not None:
            total += len(str(content))
    return total


def _first_response_text(payload: Dict[str, Any], limit: int) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    text = message.get("content") or message.get("reasoning") or message.get("reasoning_content") or ""
    text = " ".join(str(text).split())
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[:limit].rstrip() + "...<truncated>"
    return text


def _llm_log(line: str) -> None:
    with _PRINT_LOCK:
        print(line, flush=True)


def _retry_delays() -> list[float]:
    raw = os.environ.get("MYCODE_LLM_RETRY_DELAYS", "5,15,30,60")
    delays: list[float] = []
    for item in raw.split(","):
        try:
            delays.append(float(item.strip()))
        except ValueError:
            continue
    return delays or [5.0, 15.0, 30.0, 60.0]


def _should_retry(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429, 500, 502, 503, 504}


def chat_completion(
    messages: List[Dict[str, Any]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    temperature: float = 0,
    max_tokens: int = 1200,
    timeout: int = 120,
) -> Dict[str, Any]:
    config = llm_config_from_env()
    base_url = (base_url or config["base_url"]).rstrip("/")
    api_key = api_key or config["api_key"]
    model = model or config["model_api_name"]

    if not base_url or not api_key or not model:
        raise LLMClientError("Missing BASE_URL/API_KEY/MODEL_NAME or MODEL_API_NAME for LLM call.")

    token_field, effective_limit, extra_options = _completion_request_options(max_tokens)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        token_field: effective_limit,
        **extra_options,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    retry_budget = max(1, int(os.environ.get("MYCODE_LLM_RETRIES", "4")))
    delays = _retry_delays()
    last_error: str | None = None
    request_id = next(_REQUEST_COUNTER)
    verbose = _env_flag("MYCODE_LLM_VERBOSE")
    verbose_text_limit = _env_int("MYCODE_LLM_VERBOSE_TEXT_LIMIT", 0)
    request_started = time.time()

    if verbose:
        thinking_mode = str((extra_options.get("thinking") or {}).get("type") or "default")
        _llm_log(
            "[llm:start "
            f"#{request_id}] model={model} endpoint={_safe_endpoint(base_url)} "
            f"messages={len(messages)} input_chars={_message_chars(messages)} "
            f"{token_field}={effective_limit} thinking={thinking_mode} "
            f"timeout={timeout}s retries={retry_budget}"
        )

    for attempt in range(1, retry_budget + 1):
        attempt_started = time.time()
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= retry_budget:
                if verbose:
                    _llm_log(
                        "[llm:failed "
                        f"#{request_id}] attempt={attempt}/{retry_budget} "
                        f"elapsed={time.time() - request_started:.2f}s error={last_error}"
                    )
                raise LLMClientError(f"LLM request failed after {attempt} attempts: {last_error}") from exc
            delay = delays[min(attempt - 1, len(delays) - 1)]
            if verbose:
                _llm_log(
                    "[llm:retry "
                    f"#{request_id}] attempt={attempt}/{retry_budget} "
                    f"attempt_elapsed={time.time() - attempt_started:.2f}s sleep={delay}s error={last_error}"
                )
            time.sleep(delay)
            continue

        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}: {response.text[:1200]}"
            if attempt < retry_budget and _should_retry(response.status_code):
                delay = delays[min(attempt - 1, len(delays) - 1)]
                if verbose:
                    _llm_log(
                        "[llm:retry "
                        f"#{request_id}] attempt={attempt}/{retry_budget} "
                        f"status={response.status_code} attempt_elapsed={time.time() - attempt_started:.2f}s "
                        f"sleep={delay}s body={response.text[:240]}"
                    )
                time.sleep(delay)
                continue
            if verbose:
                _llm_log(
                    "[llm:failed "
                    f"#{request_id}] attempt={attempt}/{retry_budget} "
                    f"status={response.status_code} elapsed={time.time() - request_started:.2f}s "
                    f"body={response.text[:300]}"
                )
            raise LLMClientError(last_error)
        break
    else:
        raise LLMClientError(f"LLM request failed: {last_error or 'unknown error'}")

    try:
        parsed = response.json()
    except json.JSONDecodeError as exc:
        if verbose:
            _llm_log(
                "[llm:failed "
                f"#{request_id}] non_json elapsed={time.time() - request_started:.2f}s "
                f"body={response.text[:300]}"
            )
        raise LLMClientError(f"Non-JSON LLM response: {response.text[:1200]}") from exc
    if verbose:
        usage = parsed.get("usage") or {}
        finish_reason = ""
        choices = parsed.get("choices") or []
        if choices:
            finish_reason = str(choices[0].get("finish_reason") or "")
        _llm_log(
            "[llm:ok "
            f"#{request_id}] status={response.status_code} attempts={attempt}/{retry_budget} "
            f"elapsed={time.time() - request_started:.2f}s finish={finish_reason or '<none>'} "
            f"prompt={usage.get('prompt_tokens', 0)} completion={usage.get('completion_tokens', 0)} "
            f"total={usage.get('total_tokens', 0)}"
        )
        preview = _first_response_text(parsed, verbose_text_limit)
        if preview:
            _llm_log(f"[llm:text #{request_id}] {preview}")
    return parsed


def first_text(response: Dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning.strip()
    return ""
