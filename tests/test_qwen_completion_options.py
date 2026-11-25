from mycode.evidence.tools.llm_client import _completion_request_options


def test_qwen_thinking_overrides_legacy_provider_option(monkeypatch):
    monkeypatch.setenv("MYCODE_LLM_THINKING", "disabled")
    monkeypatch.setenv("MYCODE_LLM_ENABLE_THINKING", "false")
    _, _, extra = _completion_request_options(128)
    assert extra["enable_thinking"] is False
    assert "thinking" not in extra


def test_unconfigured_qwen_preserves_existing_behavior(monkeypatch):
    monkeypatch.delenv("MYCODE_LLM_ENABLE_THINKING", raising=False)
    monkeypatch.setenv("MYCODE_LLM_THINKING", "enabled")
    _, _, extra = _completion_request_options(128)
    assert extra["thinking"] == {"type": "enabled"}
    assert "enable_thinking" not in extra
