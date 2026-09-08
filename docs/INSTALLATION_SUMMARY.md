# VLM/Browser 功能安装与运行总结

## 安装完成状态 (2026-09-06)

### ✅ 已完成

| 组件 | 状态 | 版本/说明 |
|------|------|----------|
| **Python 虚拟环境** | ✅ 已重建 | .venv with pip-26.2.1 |
| **Playwright** | ✅ 已安装 | v1.62.0 |
| **Chromium 浏览器** | ✅ 已下载 | ~/.cache/ms-playwright/ |
| **VLM 模块** | ✅ 测试通过 | heuristic + API 调用 |
| **Browser 模块** | ✅ 测试通过 | CodeSandbox API + 启发式 |
| **Web Snapshot** | ✅ 测试通过 | urllib3 抓取 |
| **Evidence Packet** | ✅ 测试通过 | 多模态构建 |
| **Flow Hypotheses** | ✅ 测试通过 | 6 种流程类型 |

### ⚠️ 待完成（需要 sudo 权限）

| 组件 | 状态 | 说明 |
|------|------|------|
| **系统依赖库** | ⏳ 待安装 | libnspr4, libnss3 等 NSS 库 |
| **完整浏览器自动化** | ⏳ 待启用 | 需要系统依赖才能启动 Chromium |

---

## 当前可用功能（无需 sudo）

### 1. VLM 图片理解

**功能**: 将 issue 中的截图转换为代码搜索查询

```python
from mycode.evidence.tools.vlm_image_reader import (
    heuristic_image_understanding,  # 启发式（当前可用）
    analyze_image_with_vlm,         # LLM API 调用（需要配置）
)

# 启发式模式（无需 API）
result = heuristic_image_understanding(
    image_format="png",
    issue_summary="Chart legend not showing on hover",
    repo="chartjs/Chart.js",
)
# 输出：
# {
#   "likely_code_layers": ["chart_plugin", "canvas_render", "event_handler"],
#   "search_queries": ["legend", "tooltip", "onHover", "onLeave", ...],
#   "symptom": "chart/canvas visual behavior or rendering symptom"
# }
```

**配置 VLM API**（可选）:
```bash
# .env.local
VLM_MODEL_API_NAME=gpt-4o  # 或其他支持图片的模型
MYCODE_VLM_IMAGE_TRANSPORT=auto  # data_uri | url | auto
```

### 2. Reproduction Extractor

**功能**: 解析 Playground URL，提取代码和配置

```python
from mycode.evidence.tools.reproduction_extractor import extract_reproduction

# CodeSandbox
repro = extract_reproduction("https://codesandbox.io/s/test?file=/src/App.tsx")
# 输出：
# {
#   "platform": "codesandbox",
#   "likely_layers": ["runtime_reproduction", "component", ...],
#   "semantic_queries": [...]
# }

# MyPy Playground
repro = extract_reproduction("https://mypy-play.net/?mypy=latest&python=3.10&code=...")
# 输出：
# {
#   "platform": "mypy_play",
#   "config": {"python": "3.10", "mypy": "latest"},
#   "code_snippets": [...],
#   "likely_layers": ["type_checker", "binder", ...]
# }
```

### 3. Web Snapshot

**功能**: 抓取文档/网页，提取语义查询

```python
from mycode.evidence.tools.web_snapshot import build_web_snapshot

# 离线模式（planned）
result = build_web_snapshot(
    "https://www.chartjs.org/docs/latest/samples/legend/events.html",
    allow_network=False
)
# 输出：{"status": "planned", "doc_kind": "api_or_docs", ...}

# 在线模式
result = build_web_snapshot(url, allow_network=True, timeout=10)
# 输出：{"status": "ok", "title": "...", "headings": [...], ...}
```

### 4. Evidence Packet 构建

**功能**: 综合所有证据源，生成搜索计划

```python
from mycode.data.dataset_loader import NormalizedSample
from mycode.evidence.evidence_agent import build_evidence_packet

sample = NormalizedSample(
    instance_id="chartjs__Chart.js-10301",
    repo="chartjs/Chart.js",
    issue_text="Legend event onLeave... ![image](url) ...",
    gold_files=["src/plugins/plugin.legend.js"],
)

packet = build_evidence_packet(sample, allow_network=False)
# 输出包含：
# - modality: "image_and_url"
# - url_inspections: [...]
# - image_inspections: [...]
# - reproduction_cases: [...]
# - search_plan: [
#     {"stage": "reproduction_understanding", ...},
#     {"stage": "visual_to_program_layer", ...},
#     ...
#   ]
```

---

## 运行模式

### 模式 1: 基础 Evidence Parsing（默认）

```bash
cd /home/like/locCode/alltry/mycode
source .venv/bin/activate

# 不使用网络/浏览器/VLM
bash newtest/run_swe_clean15_full.sh
```

**特点**:
- 仅使用 problem_statement
- 启发式图片理解
- 无网络请求
- 速度最快 (~30 秒/样本)

### 模式 2: 启用网络抓取

```bash
ALLOW_NETWORK=1 bash newtest/run_swe_clean15_full.sh
```

**特点**:
- 可抓取文档网页
- 提取 API 语义
- +~10 秒/样本

### 模式 3: 启用 CodeSandbox API（无需浏览器）

```bash
ALLOW_NETWORK=1 ALLOW_BROWSER=0 bash newtest/run_swe_clean15_agent_full.sh
```

**特点**:
- 可解析 Playground
- 提取代码/配置
- +~30 秒/样本

### 模式 4: 完整多模态（需要 VLM API）

```bash
# 配置 VLM API
cat > .env.vlm << 'EOF'
BASE_URL=https://api.openai.com/v1
API_KEY=sk-...
VLM_MODEL_API_NAME=gpt-4o
MYCODE_VLM_IMAGE_TRANSPORT=auto
EOF

# 运行
DOWNLOAD_IMAGES=1 USE_VLM=1 --env-file .env.vlm \
bash newtest/run_swe_clean15_agent_full.sh
```

**特点**:
- VLM 图片理解
- 精确的视觉查询
- +~20 秒/图片

### 模式 5: 完整浏览器自动化（需要 sudo）

```bash
# 安装系统依赖后
sudo playwright install-deps chromium

# 运行
FULL_MM=1 ALLOW_BROWSER=1 \
bash newtest/run_swe_clean15_agent_full.sh
```

**特点**:
- 真实浏览器渲染
- 交互模拟
- 控制台/网络监听
- +1-2 分钟/样本

---

## 测试命令

### 快速测试（当前可用）

```bash
cd /home/like/locCode/alltry/mycode
source .venv/bin/activate

python test_vlm_browser_quick.py
```

**预期输出**:
```
✓ PASS: 模块导入
✓ PASS: VLM 功能
✓ PASS: Reproduction Extractor
✓ PASS: Web Snapshot
✓ PASS: Evidence Packet
✓ PASS: Flow Hypotheses
```

### 完整测试（需要浏览器依赖）

```bash
# 安装系统依赖后
python test_vlm_browser.py
```

---

## 下一步行动

### 立即可用

1. **运行基础定位流程**:
   ```bash
   source .venv/bin/activate
   bash newtest/run_swe_clean15_full.sh
   ```

2. **启用网络抓取**:
   ```bash
   ALLOW_NETWORK=1 bash newtest/run_swe_clean15_full.sh
   ```

3. **测试 CodeSandbox API 解析**:
   ```bash
   ALLOW_NETWORK=1 ALLOW_BROWSER=0 \
   bash newtest/run_swe_clean15_agent_full.sh
   ```

### 需要 sudo 权限

1. **安装系统依赖**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y \
       libnspr4 libnss3 libdbus-1-3 libatk1.0-0 \
       libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
       libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
       libgbm1 libasound2 libpango-1.0-0 libcairo2
   ```

   或:
   ```bash
   source .venv/bin/activate
   sudo playwright install-deps chromium
   ```

2. **验证浏览器启动**:
   ```bash
   source .venv/bin/activate
   python test_vlm_browser.py  # 完整测试
   ```

### 可选：配置 VLM API

如果有支持图片的 LLM API:

```bash
# .env.local
VLM_MODEL_API_NAME=gpt-4o  # 或 claude-3-sonnet 等
MYCODE_VLM_IMAGE_TRANSPORT=auto
```

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `docs/VLM_BROWSER_SETUP.md` | 完整配置指南 |
| `test_vlm_browser_quick.py` | 快速测试（无需 sudo） |
| `test_vlm_browser.py` | 完整测试（需要浏览器） |
| `src/mycode/evidence/tools/vlm_image_reader.py` | VLM 实现 |
| `src/mycode/evidence/tools/browser_reproduction_reader.py` | Browser 实现 |
| `src/mycode/evidence/tools/web_snapshot.py` | Web Snapshot |
| `src/mycode/evidence/tools/reproduction_extractor.py` | Reproduction 解析 |

---

## 常见问题

### Q: 为什么浏览器启动失败？

A: 缺少系统依赖库。运行：
```bash
sudo playwright install-deps chromium
```

### Q: VLM API 返回 400 错误？

A: 可能是图片传输方式问题。尝试：
```bash
MYCODE_VLM_IMAGE_TRANSPORT=url  # 或 data_uri, auto
```

### Q: CodeSandbox API 返回空结果？

A: 可能是网络问题或 sandbox 已删除。检查 URL 是否有效。

### Q: 如何在没有 sudo 的情况下使用？

A: 使用 `ALLOW_BROWSER=0` 模式，利用启发式和 API 模式：
```bash
ALLOW_NETWORK=1 ALLOW_BROWSER=0 DOWNLOAD_IMAGES=0 \
bash newtest/run_swe_clean15_agent_full.sh
```

---

## 总结

**当前状态**: 核心功能已就绪，可在无 sudo 权限下运行基础和多模态证据解析。

**限制**: 完整浏览器自动化需要安装系统依赖（需要 sudo）。

**下一步**: 根据需求选择运行模式，或联系系统管理员安装浏览器依赖。
