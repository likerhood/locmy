from types import SimpleNamespace

from mycode.dynamic_retrieval.seed_frontier import (
    restore_seed_frontier, demote_generated_outputs, generated_source_counterpart,
)


def row(path):
    return SimpleNamespace(path=path, reasons=[])


def evidence_row(path, axes=(), *, persistent=False):
    return SimpleNamespace(
        path=path,
        reasons=[],
        belief={
            "supporting_axes": list(axes),
            "persistent_seed": persistent,
        },
    )


def test_seed_union_retains_multi_file_targets_and_tail_order():
    index = SimpleNamespace(files={p: "function f() {}" for p in ("src/a.js", "src/b.js", "src/c.js")})
    ranked, report = restore_seed_frontier(
        [row("src/c.js"), row("src/a.js")], [row("src/a.js"), row("src/b.js")],
        index=index, review={}, top_k=3,
    )
    assert [x.path for x in ranked] == ["src/a.js", "src/b.js", "src/c.js"]
    assert report["retained"] == ["src/a.js", "src/b.js"]


def test_missing_flow_is_not_rejection_but_supported_contradiction_is():
    index = SimpleNamespace(files={"src/a.js": "source", "src/b.js": "source"})
    for counter, expected in [(["no_flow"], "src/a.js"), (["wrong state writer"], "src/b.js")]:
        ranked, _ = restore_seed_frontier(
            [row("src/b.js"), row("src/a.js")], [row("src/a.js")], index=index,
            review={"candidates": [{"path": "src/a.js", "grounded": True, "counterevidence": counter}]},
            top_k=2,
        )
        assert ranked[0].path == expected


def test_generated_cjs_demoted_without_dropping_recall_or_real_lib():
    files = {"lib/parser.cjs": "// Automatically generated, do not edit", "src/parser.js": "source",
             "client/lib/parser.js": "source", "lib/native.js": "source"}
    assert generated_source_counterpart("lib/native.js", files) is None
    assert generated_source_counterpart("client/lib/parser.js", files) is None
    ranked, mappings = demote_generated_outputs(
        [row(p) for p in files], files=files, issue_text="Parsing nested lists fails",
    )
    assert ranked[-1].path == "lib/parser.cjs"
    assert set(x.path for x in ranked) == set(files)
    assert len(mappings) == 1


def test_seed_filter_rejects_missing_source_and_enforces_limit_without_mutation():
    index = SimpleNamespace(files={"src/a.js": "source", "src/b.js": "source"})
    original = [row("src/b.js"), row("src/a.js")]
    ranked, report = restore_seed_frontier(
        original, [row("src/missing.js"), row("src/a.js"), row("src/b.js")],
        index=index, review={}, top_k=2, limit=1,
    )
    assert report["retained"] == ["src/a.js"]
    assert report["rejected"] == [{"path": "src/missing.js", "reason": "source_unavailable"}]
    assert [x.path for x in ranked] == ["src/a.js", "src/b.js"]
    assert original[1].reasons == []


def test_explicit_build_request_keeps_output_order():
    files = {"lib/parser.cjs": "// generated file", "src/parser.js": "source"}
    original = [row(p) for p in files]
    ranked, mappings = demote_generated_outputs(
        original, files=files, issue_text="Regenerate the bundle after the build change",
    )
    assert ranked == original
    assert not mappings


def test_kimi_style_retention_keeps_third_multi_channel_seed():
    paths = ("src/a.js", "src/b.js", "src/c.js", "src/noise.js")
    index = SimpleNamespace(files={path: "source" for path in paths})
    seeds = [
        evidence_row("src/a.js", ("concern", "flow", "tool")),
        evidence_row("src/b.js", ("effect", "entity", "tool")),
        evidence_row(
            "src/c.js",
            ("concern", "evidence_role", "explicit_entity", "flow", "tool"),
        ),
    ]

    ranked, report = restore_seed_frontier(
        [row("src/noise.js")],
        seeds,
        index=index,
        review={},
        top_k=4,
        limit=3,
        minimum_supporting_axes=3,
    )

    assert [item.path for item in ranked] == [
        "src/a.js",
        "src/b.js",
        "src/c.js",
        "src/noise.js",
    ]
    assert report["retained"] == ["src/a.js", "src/b.js", "src/c.js"]
    assert report["minimum_supporting_axes"] == 3


def test_multi_channel_retention_rejects_weak_seed_but_keeps_persistent_seed():
    paths = ("src/strong.js", "src/weak.js", "src/persistent.js")
    index = SimpleNamespace(files={path: "source" for path in paths})
    seeds = [
        evidence_row("src/strong.js", ("concern", "flow", "tool")),
        evidence_row("src/weak.js", ("tool",)),
        evidence_row("src/persistent.js", ("tool",), persistent=True),
    ]

    ranked, report = restore_seed_frontier(
        [row("src/weak.js")],
        seeds,
        index=index,
        review={},
        top_k=3,
        limit=3,
        minimum_supporting_axes=3,
    )

    assert [item.path for item in ranked] == [
        "src/strong.js",
        "src/persistent.js",
        "src/weak.js",
    ]
    assert report["retained"] == ["src/strong.js", "src/persistent.js"]
    assert report["rejected"] == [
        {
            "path": "src/weak.js",
            "reason": "insufficient_multi_channel_support",
            "supporting_axis_count": 1,
            "supporting_axes": ["tool"],
            "persistent_seed": False,
        }
    ]
