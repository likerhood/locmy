from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from unittest.mock import patch

from mycode.evidence.runtime import tool_executor
from mycode.evidence.tools import vlm_image_reader
from mycode.schemas.evidence import EvidenceCollectionPlan, ToolRequest


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def test_vlm_image_reader_builds_multimodal_request() -> None:
    seen = {}

    def fake_chat_completion(messages, **kwargs):
        seen["messages"] = messages
        seen["kwargs"] = kwargs
        return {
            "id": "fake-vlm",
            "model": "fake-model",
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"visible_text":["Legend"],'
                            '"visual_entities":["pie chart","legend"],'
                            '"symptom":"legend hover leave state is wrong",'
                            '"search_queries":["legend onLeave onHover handleEvent"]}'
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 1},
        }

    old = vlm_image_reader.chat_completion
    vlm_image_reader.chat_completion = fake_chat_completion
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "one.png"
            image.write_bytes(ONE_PIXEL_PNG)
            result = vlm_image_reader.analyze_image_with_vlm(
                image_path=image,
                image_format="png",
                issue_summary="Legend onLeave not fired.",
                repo="chartjs/Chart.js",
            )
    finally:
        vlm_image_reader.chat_completion = old

    assert result["search_queries"] == ["legend onLeave onHover handleEvent"]
    content = seen["messages"][1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result["_image_transport"] == "data_uri"


def test_vlm_image_reader_can_use_remote_url() -> None:
    seen = {}

    def fake_chat_completion(messages, **kwargs):
        seen["messages"] = messages
        return {
            "id": "fake-vlm-url",
            "model": "fake-model",
            "choices": [{"message": {"content": '{"visible_text":["Settings"]}'}}],
        }

    old = vlm_image_reader.chat_completion
    vlm_image_reader.chat_completion = fake_chat_completion
    try:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MYCODE_VLM_IMAGE_TRANSPORT": "url"}
        ):
            image = Path(tmp) / "one.png"
            image.write_bytes(ONE_PIXEL_PNG)
            result = vlm_image_reader.analyze_image_with_vlm(
                image_path=image,
                image_format="png",
                issue_summary="Settings page is incorrect.",
                repo="example/repo",
                source_url="https://example.com/screenshot.png",
            )
    finally:
        vlm_image_reader.chat_completion = old

    content = seen["messages"][1]["content"]
    assert content[1]["image_url"]["url"] == "https://example.com/screenshot.png"
    assert result["_image_transport"] == "url"


def test_vlm_image_reader_auto_falls_back_to_remote_url() -> None:
    seen_urls = []

    def fake_chat_completion(messages, **kwargs):
        uri = messages[1]["content"][1]["image_url"]["url"]
        seen_urls.append(uri)
        if len(seen_urls) == 1:
            raise vlm_image_reader.LLMClientError("HTTP 400: Invalid request parameters")
        return {
            "id": "fake-vlm-fallback",
            "model": "fake-model",
            "choices": [{"message": {"content": '{"visible_text":["Address"]}'}}],
        }

    old = vlm_image_reader.chat_completion
    vlm_image_reader.chat_completion = fake_chat_completion
    try:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MYCODE_VLM_IMAGE_TRANSPORT": "auto"}
        ):
            image = Path(tmp) / "one.png"
            image.write_bytes(ONE_PIXEL_PNG)
            result = vlm_image_reader.analyze_image_with_vlm(
                image_path=image,
                image_format="png",
                issue_summary="Address form is incorrect.",
                repo="example/repo",
                source_url="https://example.com/screenshot.png",
            )
    finally:
        vlm_image_reader.chat_completion = old

    assert seen_urls[0].startswith("data:image/png;base64,")
    assert seen_urls[1] == "https://example.com/screenshot.png"
    assert result["_image_transport"] == "url"
    assert result["_image_transport_fallback"]["from"] == "data_uri"


def test_vlm_image_reader_auto_falls_back_after_transient_provider_error() -> None:
    seen_urls = []

    def fake_chat_completion(messages, **kwargs):
        uri = messages[1]["content"][1]["image_url"]["url"]
        seen_urls.append(uri)
        if len(seen_urls) == 1:
            raise vlm_image_reader.LLMClientError("HTTP 500: Internal Server Error")
        return {
            "id": "fake-vlm-transient-fallback",
            "model": "fake-model",
            "choices": [{"message": {"content": '{"visible_text":["Address"]}'}}],
        }

    old = vlm_image_reader.chat_completion
    vlm_image_reader.chat_completion = fake_chat_completion
    try:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MYCODE_VLM_IMAGE_TRANSPORT": "auto"}
        ):
            image = Path(tmp) / "one.png"
            image.write_bytes(ONE_PIXEL_PNG)
            result = vlm_image_reader.analyze_image_with_vlm(
                image_path=image,
                image_format="png",
                issue_summary="Address form is incorrect.",
                repo="example/repo",
                source_url="https://example.com/screenshot.png",
            )
    finally:
        vlm_image_reader.chat_completion = old

    assert len(seen_urls) == 2
    assert result["_image_transport"] == "url"
    assert "HTTP 500" in result["_image_transport_fallback"]["reason"]


def test_vlm_failure_returns_heuristic_evidence() -> None:
    def fail_analysis(**kwargs):
        raise vlm_image_reader.LLMClientError(
            "ChunkedEncodingError: Response ended prematurely"
        )

    old = vlm_image_reader.analyze_image_with_vlm
    vlm_image_reader.analyze_image_with_vlm = fail_analysis
    try:
        result = vlm_image_reader.try_analyze_image_with_vlm(
            image_path="/tmp/unavailable.png",
            image_format="png",
            issue_summary="PDF layout margin is incorrect.",
            repo="diegomura/react-pdf",
        )
    finally:
        vlm_image_reader.analyze_image_with_vlm = old

    assert result["vlm_status"] == "failed"
    assert result["fallback"] == "heuristic_image_understanding"
    assert "layout_engine" in result["likely_code_layers"]
    assert "Response ended prematurely" in result["error"]


def test_image_tool_marks_vlm_fallback_as_partial(monkeypatch, tmp_path) -> None:
    image = tmp_path / "one.png"
    image.write_bytes(ONE_PIXEL_PNG)
    monkeypatch.setattr(
        tool_executor,
        "read_image_asset",
        lambda *args, **kwargs: {
            "status": "ok",
            "processable": True,
            "local_path": str(image),
            "format": "png",
            "url": "https://example.com/one.png",
        },
    )
    monkeypatch.setattr(
        tool_executor,
        "try_analyze_image_with_vlm",
        lambda **kwargs: {
            "vlm_status": "failed",
            "fallback": "heuristic_image_understanding",
            "search_queries": ["layout"],
        },
    )
    request = ToolRequest(
        tool="vlm_image_inspector",
        source="https://example.com/one.png",
        evidence_type="image_url",
        role_hypothesis="visual_evidence",
        priority="high",
        reason="test",
        expected_outputs=["search_queries"],
    )
    plan = EvidenceCollectionPlan(
        instance_id="sample",
        repo="example/repo",
        dataset="test",
        issue_summary="PDF layout is incorrect.",
    )

    observation = tool_executor.execute_tool_request(
        request,
        cache_dir=tmp_path,
        plan=plan,
        use_vlm=True,
    )

    assert observation.success is True
    assert observation.status == "partial_vlm_fallback"
    assert "vlm_failed_using_heuristic_image_evidence" in observation.warnings


if __name__ == "__main__":
    test_vlm_image_reader_builds_multimodal_request()
    test_vlm_image_reader_can_use_remote_url()
    test_vlm_image_reader_auto_falls_back_to_remote_url()
    test_vlm_image_reader_auto_falls_back_after_transient_provider_error()
    test_vlm_failure_returns_heuristic_evidence()
    print("PASS vlm image tool")
