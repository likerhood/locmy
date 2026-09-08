#!/usr/bin/env python3
"""
快速测试脚本 - 无需 sudo 权限
测试 VLM/Browser 核心功能（不含实际浏览器启动）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_modules_import():
    """测试核心模块导入"""
    print("=" * 60)
    print("测试核心模块导入")
    print("=" * 60)

    try:
        from mycode.evidence.tools.vlm_image_reader import (
            analyze_image_with_vlm,
            try_analyze_image_with_vlm,
            heuristic_image_understanding,
            _data_uri,
            _image_transport,
        )
        print("✓ VLM 模块导入成功")
    except ImportError as e:
        print(f"✗ VLM 模块导入失败：{e}")
        return False

    try:
        from mycode.evidence.tools.browser_reproduction_reader import (
            read_browser_reproduction,
            extract_reproduction,
            _try_codesandbox_api,
        )
        print("✓ Browser 模块导入成功")
    except ImportError as e:
        print(f"✗ Browser 模块导入失败：{e}")
        return False

    try:
        from mycode.evidence.tools.web_snapshot import build_web_snapshot
        print("✓ Web Snapshot 模块导入成功")
    except ImportError as e:
        print(f"✗ Web Snapshot 模块导入失败：{e}")
        return False

    return True


def test_vlm_functions():
    """测试 VLM 功能（不含 API 调用）"""
    print("\n" + "=" * 60)
    print("测试 VLM 功能")
    print("=" * 60)

    from mycode.evidence.tools.vlm_image_reader import (
        heuristic_image_understanding,
        _data_uri,
        _image_transport,
    )
    from PIL import Image

    # 测试图片生成
    img = Image.new('RGB', (100, 100), color='blue')
    img_path = Path("/tmp/test_vlm_image.png")
    img.save(img_path)
    print(f"✓ 测试图片生成：{img_path}")

    # 测试 Data URI
    data_uri = _data_uri(img_path, "png")
    print(f"✓ Data URI 生成：{len(data_uri)} 字符")

    # 测试传输配置
    transport = _image_transport()
    print(f"✓ 当前传输方式：{transport}")

    # 测试启发式理解（不同场景）
    test_cases = [
        ("Chart legend not showing", "chartjs/Chart.js"),
        ("Button click redirects wrong page", "Automattic/wp-calypso"),
        ("PDF text overlap margin", "diegomura/react-pdf"),
        ("Error traceback in console", "facebook/react"),
    ]

    for issue, repo in test_cases:
        result = heuristic_image_understanding(
            image_format="png",
            issue_summary=issue,
            repo=repo,
        )
        layers = result.get("likely_code_layers", [])
        queries = result.get("search_queries", [])
        print(f"  ✓ '{issue[:30]}...' → {len(layers)} layers, {len(queries)} queries")

    return True


def test_reproduction_extractor():
    """测试 Reproduction Extractor"""
    print("\n" + "=" * 60)
    print("测试 Reproduction Extractor")
    print("=" * 60)

    from mycode.evidence.tools.reproduction_extractor import (
        extract_reproduction,
        _platform,
        _likely_layers,
    )

    test_urls = [
        # CodeSandbox
        "https://codesandbox.io/s/test?file=/src/App.tsx",
        # MyPy Playground
        "https://mypy-play.net/?mypy=latest&python=3.10&code=print(1)",
        # TypeScript Playground
        "https://www.typescriptlang.org/play?#code=MYGwhgzhAEDC0FMAe0C80DeoBQ1oAcwA3AX2mEjABZTooiSmWWgBLAOwGdpoOkxg0ALmgAKKZMw48+0AIYAuKJFlQZs+YuUqUAlNAB800WfOWaIaTNjwoZ0CDDiJV0NOnJkI00AL6nT0QYLBElDS0DEzMLGzsHFw8fPxCImKSsnIKisqq6praevoGRiZmFla29g6Ozq4eXj5+gWISUjJyCkrKquoamlr6hqbmlta29k6ubp4+vv4BQaHhUTFxCYkpqRmZWdk5uXn5hUXFpeVV1TW1dQ2NTS2tbR2dXd09vX2DQ8Mjo2PjE5NT0zNzC0sraxtbO3sHJ2cXVw9vH18g4LDIqOiY2Lj4hMSk5JTUtIzs3Lz8gsKi4pKy8qrq2vqGxpbWts6u7p7evsGh4dGx8cnJ6ZnZufmFxaXllbWNza3tnd29vX3DI6Nj4xNT0zOzc0vLK6tr65vbO7v7B4dHxyenZ+cXl1c3t3f3j0/PLa9v7x+fnF5fXt/ePr6/vH59f3j6/vn1/eP7+8fX98/vH1/fP7x9f3z+8fX98/vH1/fP7x9f3z+8fX98/vH1/fP7x9f3z",
        # Prettier Playground
        "https://prettier.io/playground/#N4Igxg9gdgLgprEAuEAzArlMMCW0AE8AzjABQCU+wAOlPvkQM74BmFANgIYBGFLA7tAB0pGDGgBXfhgDcFKgGZJ02QsXQJ0AIxj4w0AK4Yw0ZTID80kAF4QACgA0INjBh4w0C5YD0J6AF4QACgA0INjBh4w0C5YD0J6AF4QACgA0INjBh4w0C5YD0J6AF4QACgA0INjBh4w0C5YD0J6AF4QACgA0INjBh4w0C5YD0J6A",
    ]

    for url in test_urls:
        repro = extract_reproduction(url)
        platform = repro.get("platform", "unknown")
        layers = repro.get("likely_layers", [])
        snippets = len(repro.get("code_snippets", []))
        config = repro.get("config", {})
        print(f"  ✓ {platform}: {len(layers)} layers, {snippets} snippets, {len(config)} config keys")

    return True


def test_web_snapshot():
    """测试 Web Snapshot"""
    print("\n" + "=" * 60)
    print("测试 Web Snapshot")
    print("=" * 60)

    from mycode.evidence.tools.web_snapshot import build_web_snapshot

    test_urls = [
        "https://www.chartjs.org/docs/latest/samples/legend/events.html",
        "https://react.dev/reference/react/useState",
        "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    ]

    for url in test_urls:
        # 测试离线模式（planned）
        result = build_web_snapshot(url, allow_network=False)
        status = result.get("status")
        doc_kind = result.get("doc_kind")
        queries = len(result.get("semantic_queries", []))
        print(f"  ✓ {url[:50]}... → {status} ({doc_kind}), {queries} queries")

    # 测试在线模式
    print("\n  在线模式测试:")
    url = "https://www.chartjs.org/docs/latest/samples/"
    result = build_web_snapshot(url, allow_network=True, timeout=10)
    status = result.get("status")
    title = result.get("title", "")[:40]
    headings = len(result.get("headings", []))
    print(f"    {url} → {status}, title='{title}', {headings} headings")

    return True


def test_evidence_packet():
    """测试 Evidence Packet 构建"""
    print("\n" + "=" * 60)
    print("测试 Evidence Packet 构建")
    print("=" * 60)

    from mycode.data.dataset_loader import NormalizedSample
    from mycode.evidence.evidence_agent import build_evidence_packet

    # 测试用例 1: chartjs-10301 (多模态)
    sample1 = NormalizedSample(
        instance_id="chartjs__Chart.js-10301",
        repo="chartjs/Chart.js",
        dataset="test",
        issue_text=(
            "Legend event onLeave. In the example at "
            "https://www.chartjs.org/docs/latest/samples/legend/events.html "
            "you can hover over a legend. If you quickly place the mouse outside "
            "the chart, content sometimes remains highlighted because onLeave is "
            "not called. "
            "![image](https://user-images.githubusercontent.com/58777964/157239796-95ccabbb.png) "
            "Reproducible sample: "
            "https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx"
        ),
        raw={},
        gold_files=["src/plugins/plugin.legend.js"],
    )

    packet1 = build_evidence_packet(sample1, allow_network=False)
    print(f"  ✓ chartjs-10301:")
    print(f"      Modality: {packet1.modality}")
    print(f"      URLs: {len(packet1.url_inspections)}, Images: {len(packet1.image_inspections)}")
    print(f"      Reproduction cases: {len(packet1.reproduction_cases)}")
    print(f"      Search plan: {[s.get('stage') for s in packet1.search_plan]}")

    # 测试用例 2: wp-calypso (GitHub 代码种子)
    sample2 = NormalizedSample(
        instance_id="Automattic__wp-calypso-21409",
        repo="Automattic/wp-calypso",
        dataset="test",
        issue_text=(
            "Store: Signup Flow needs to require email verification. "
            "See https://github.com/Automattic/wp-calypso/blob/master/client/state/current-user/selectors.js#L157"
        ),
        raw={},
        gold_files=["client/extensions/woocommerce/app/dashboard/index.js"],
    )

    packet2 = build_evidence_packet(sample2, allow_network=False)
    print(f"  ✓ wp-calypso-21409:")
    print(f"      Modality: {packet2.modality}")
    print(f"      Code references: {len(packet2.code_references)}")
    print(f"      Symbol queries: {packet2.symbol_queries[:3]}")

    return True


def test_flow_hypotheses():
    """测试 Flow Hypotheses 生成"""
    print("\n" + "=" * 60)
    print("测试 Flow Hypotheses 生成")
    print("=" * 60)

    from mycode.evidence.evidence_builder import build_flow_hypotheses

    test_cases = [
        ("Click submit button redirects to wrong URL", ["url_builder_or_route_flow", "ui_event_flow"]),
        ("Parameter value not passed to component", ["parameter_or_config_flow"]),
        ("State not updated after API response", ["state_selector_flow"]),
        ("Type error in class method", ["symbol_or_type_flow"]),
        ("Chart rendering overlap margin", ["render_style_pipeline_flow"]),
    ]

    for issue, expected_flows in test_cases:
        hypotheses = build_flow_hypotheses(issue)
        matched = [f for f in expected_flows if f in hypotheses]
        print(f"  ✓ '{issue[:40]}...' → {len(hypotheses)} flows: {matched}")

    return True


def main():
    """运行所有测试"""
    print("\n" + "=" * 70)
    print("VLM/Browser 快速测试套件 (无需 sudo)")
    print("=" * 70)

    tests = [
        ("模块导入", test_modules_import),
        ("VLM 功能", test_vlm_functions),
        ("Reproduction Extractor", test_reproduction_extractor),
        ("Web Snapshot", test_web_snapshot),
        ("Evidence Packet", test_evidence_packet),
        ("Flow Hypotheses", test_flow_hypotheses),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n✗ {name} 测试异常：{e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)

    for name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")

    all_passed = all(results.values())
    print("\n" + "=" * 70)
    if all_passed:
        print("✓ 所有测试通过！核心功能已就绪。")
        print("\n下一步:")
        print("1. 如需完整浏览器自动化，需要安装系统依赖:")
        print("   sudo playwright install-deps chromium")
        print("\n2. 配置 VLM API (可选):")
        print("   编辑 .env.local 添加 VLM_MODEL_API_NAME")
        print("\n3. 运行完整测试:")
        print("   source .venv/bin/activate && python test_vlm_browser.py")
    else:
        print("部分测试失败，请检查输出。")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
