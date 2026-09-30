from mycode.evidence.tools.json_response import parse_json_response
from mycode.dynamic_retrieval.controller import _parse_llm_decisions


def test_parse_json_response_accepts_plain_json() -> None:
    parsed, diagnostics = parse_json_response(
        '{"additional_tool_requests": [], "stop_reason": "enough evidence"}',
        required_keys=("additional_tool_requests", "stop_reason"),
    )
    assert parsed["stop_reason"] == "enough evidence"
    assert diagnostics == {
        "parse_status": "parsed",
        "repair_method": "direct",
        "schema_valid": True,
    }


def test_parse_json_response_repairs_kimi_markdown_fence() -> None:
    parsed, diagnostics = parse_json_response(
        '```json\n{"additional_tool_requests": [{"tool": "web_snapshot_fetcher"}], '
        '"stop_reason": "one followup"}\n```',
        required_keys=("additional_tool_requests", "stop_reason"),
    )
    assert parsed["additional_tool_requests"][0]["tool"] == "web_snapshot_fetcher"
    assert diagnostics["parse_status"] == "repaired"
    assert diagnostics["repair_method"] == "markdown_fence"
    assert diagnostics["schema_valid"] is True


def test_parse_json_response_extracts_object_from_short_preamble() -> None:
    parsed, diagnostics = parse_json_response(
        'Here is the requested JSON:\n{"decisions": []}\nEnd.',
        required_keys=("decisions",),
    )
    assert parsed == {"decisions": []}
    assert diagnostics["repair_method"] == "object_extraction"


def test_parse_json_response_does_not_recover_truncated_object() -> None:
    parsed, diagnostics = parse_json_response('{"decisions": [', required_keys=("decisions",))
    assert parsed == {}
    assert diagnostics["parse_status"] == "failed"
    assert diagnostics["schema_valid"] is False


def test_parse_json_response_does_not_promote_inner_object_from_truncation() -> None:
    parsed, diagnostics = parse_json_response(
        '{"issue_understanding":{"problem":"x"},"evidence_roles":',
    )
    assert parsed == {}
    assert diagnostics["parse_status"] == "failed"


def test_parse_json_response_can_preserve_legacy_top_level_array() -> None:
    parsed, diagnostics = parse_json_response(
        '[{"tool": "SearchAnchor"}]',
        allow_list=True,
    )
    assert parsed == [{"tool": "SearchAnchor"}]
    assert diagnostics["schema_valid"] is True


def test_controller_recovers_fenced_decisions() -> None:
    decisions = _parse_llm_decisions(
        '```json\n{"decisions": [{"tool": "TraceFlow", "mode": "call_chain", '
        '"reason": "follow the call edge", "queries": ["serialize"], '
        '"preferred_edge_types": ["calls"]}]}\n```'
    )
    assert len(decisions) == 1
    assert decisions[0].tool == "TraceFlow"
    assert decisions[0].source == "llm"
