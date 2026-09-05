from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from unittest.mock import patch

from mycode.evidence.tools import vlm_image_reader


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


if __name__ == "__main__":
    test_vlm_image_reader_builds_multimodal_request()
    test_vlm_image_reader_can_use_remote_url()
    test_vlm_image_reader_auto_falls_back_to_remote_url()
    print("PASS vlm image tool")
