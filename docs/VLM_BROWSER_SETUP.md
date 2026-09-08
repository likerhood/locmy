# VLM/Browser 功能配置指南

本指南帮助你启用 mycode 的完整 VLM 图片理解和 Browser 浏览器自动化功能。

## 当前状态 (截至 2026-09-06)

| 组件 | 状态 | 说明 |
|------|------|------|
| **Playwright Python** | ✅ 已安装 | v1.62.0 in .venv |
| **Chromium 浏览器** | ✅ 已下载 | ~/.cache/ms-playwright/ |
| **系统依赖库** | ⚠️ 需要 sudo | libnspr4.so 等 NSS 库 |
| **VLM 图像传输** | ✅ 可用 | data_uri/url/auto |
| **Web Snapshot** | ✅ 可用 | urllib3 抓取 |
| **CodeSandbox API** | ✅ 可用 | 无需浏览器 |
| **完整 Browser** | ⏳ 待安装 | 需要系统依赖 |

## 快速开始（无需 sudo）

如果无法安装系统依赖，可以使用**无浏览器模式**：

```bash
cd /home/like/locCode/alltry/mycode

# 激活虚拟环境
source .venv/bin/activate

# 测试当前功能
python test_vlm_browser.py

# 运行不带浏览器的完整流程
ALLOW_NETWORK=1 ALLOW_BROWSER=0 DOWNLOAD_IMAGES=0 \
bash newtest/run_swe_clean15_agent_full.sh
```

## 完整安装（需要 sudo）

### 1. 安装系统依赖

```bash
# Ubuntu/Debian 系统
sudo apt-get update
sudo apt-get install -y \
    libnspr4 \
    libnss3 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2

# 或使用 playwright 自动安装
source .venv/bin/activate
playwright install-deps chromium
```

### 2. 验证安装

```bash
source .venv/bin/activate
python test_vlm_browser.py
```

---

## 前置要求（完整安装）

### 2. VLM API 配置

你的项目使用 OpenAI 兼容 API 格式，支持以下配置：

```bash
# .env.local 或 .env.<name>
BASE_URL=https://your-api-endpoint.com/v1
API_KEY=your-api-key

# VLM 专用模型 (如果与文本模型不同)
VLM_MODEL_API_NAME=gpt-4o  # 或其他支持图片的模型

# 图片传输方式
# - data_uri: 将图片编码为 Base64 (默认，兼容性好)
# - url: 直接传递 HTTP URL (需要服务端支持)
# - auto: 优先 data_uri，失败时自动尝试 url
MYCODE_VLM_IMAGE_TRANSPORT=auto
```

## 运行模式

### 模式 1: 仅 Evidence Parsing (无需额外配置)

```bash
# 默认配置，不使用真实网络/浏览器/VLM
bash newtest/run_swe_clean15_full.sh
```

### 模式 2: 启用网络抓取 (Web Snapshot)

```bash
# 允许 HTTP 请求抓取文档/网页
ALLOW_NETWORK=1 bash newtest/run_swe_clean15_full.sh
```

### 模式 3: 启用 Browser 自动化 (需要 Playwright)

```bash
# 启用浏览器自动化读取 CodeSandbox/Playground
ALLOW_NETWORK=1 ALLOW_BROWSER=1 bash newtest/run_swe_clean15_full.sh
```

### 模式 4: 启用 VLM 图片分析 (需要支持图片的 API)

```bash
# 下载图片并调用 VLM 分析
DOWNLOAD_IMAGES=1 USE_VLM=1 \
MYCODE_VLM_IMAGE_TRANSPORT=auto \
bash newtest/run_swe_clean15_full.sh
```

### 模式 5: 完整多模态 Agent

```bash
# 所有功能全开
FULL_MM=1 \
bash newtest/run_swe_clean15_agent_full.sh
```

等价于：
```bash
USE_LLM=1 \
USE_LLM_PLANNING=1 \
USE_LLM_CONTROLLER=1 \
ALLOW_NETWORK=1 \
ALLOW_BROWSER=1 \
DOWNLOAD_IMAGES=1 \
USE_VLM=1 \
MYCODE_VLM_IMAGE_TRANSPORT=auto \
bash newtest/run_swe_clean15_agent_full.sh
```

## 命令行参数

`run_swe_clean15_agent_full.sh` 支持以下参数：

```bash
# 指定模型
bash newtest/run_swe_clean15_agent_full.sh --model Qwen3.5-397B-A17B --model-api-name xopqwen35397b

# 指定 VLM 图片传输方式
bash newtest/run_swe_clean15_agent_full.sh --vlm-image-transport auto

# 使用自定义环境文件
bash newtest/run_swe_clean15_agent_full.sh --env-file .env.aliyun-kimi.local

# 查看配置
bash newtest/run_swe_clean15_agent_full.sh --print-model-config

# 仅跑 1 条样本测试
MAX_SAMPLES=1 bash newtest/run_swe_clean15_agent_full.sh

# 指定样本
INSTANCE_ID=chartjs__Chart.js-10301 bash newtest/run_swe_clean15_agent_full.sh
```

## 配置文件示例

### `.env.vlm` (支持图片的 API)

```dotenv
BASE_URL=https://api.openai.com/v1
API_KEY=sk-...
MODEL_NAME=gpt-4o
MODEL_API_NAME=gpt-4o

# VLM 专用 (如果与主模型不同)
VLM_MODEL_API_NAME=gpt-4o

# 图片传输
MYCODE_VLM_IMAGE_TRANSPORT=auto
```

### `.env.browser` (浏览器自动化)

```dotenv
# 基础 API 配置
BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
API_KEY=...
MODEL_NAME=Kimi2.6
MODEL_API_NAME=kimi-k2.6

# 浏览器配置
MYCODE_BROWSER_TIMEOUT=30  # 秒
MYCODE_BROWSER_HEADLESS=1  # 无头模式
```

### `.env.full-mm` (完整多模态)

```dotenv
BASE_URL=...
API_KEY=...
MODEL_NAME=Qwen3.5-397B-A17B
MODEL_API_NAME=xopqwen35397b

# 分阶段模型配置
PLANNING_MODEL_API_NAME=xopqwen35397b
EVIDENCE_MODEL_API_NAME=xopqwen35397b
CONTROLLER_MODEL_API_NAME=xopqwen35397b
VLM_MODEL_API_NAME=xopqwen35397b  # 需要支持图片

# 功能开关
MYCODE_VLM_IMAGE_TRANSPORT=auto
MYCODE_BROWSER_TIMEOUT=60
```

## 验证安装

### 1. 检查 Playwright

```bash
source .venv/bin/activate
python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
```

### 2. 测试 VLM

```bash
# 创建测试脚本
cat > test_vlm.py << 'EOF'
from mycode.evidence.tools.vlm_image_reader import try_analyze_image_with_vlm
from pathlib import Path

# 创建一个测试图片 (或指定现有图片)
image_path = Path("/tmp/test_image.png")
if not image_path.exists():
    # 创建一个简单的测试图片
    from PIL import Image
    img = Image.new('RGB', (100, 100), color='red')
    img.save(image_path)

result = try_analyze_image_with_vlm(
    image_path=str(image_path),
    image_format="png",
    issue_summary="Test chart rendering issue",
    repo="test/repo",
)

print("VLM Status:", result.get("vlm_status"))
print("Search Queries:", result.get("search_queries", [])[:5])
EOF

python test_vlm.py
```

### 3. 测试 Browser

```bash
cat > test_browser.py << 'EOF'
from mycode.evidence.tools.browser_reproduction_reader import read_browser_reproduction
from pathlib import Path

cache_dir = Path("/tmp/test_browser_cache")
cache_dir.mkdir(parents=True, exist_ok=True)

# 测试 CodeSandbox
result = read_browser_reproduction(
    url="https://codesandbox.io/s/react-chartjs-2-chart-js-issue-template-forked-3kw5p0?file=/src/App.tsx",
    cache_dir=str(cache_dir),
    allow_network=True,
    allow_browser=True,
    timeout=30,
)

print("Status:", result.get("status"))
print("Platform:", result.get("platform"))
print("Source Files:", len(result.get("source_files", [])))
print("Semantic Queries:", result.get("semantic_queries", [])[:5])
EOF

python test_browser.py
```

## 故障排查

### Playwright 安装失败

```bash
# 尝试使用 pipx
pipx install playwright
playwright install chromium

# 或手动下载
wget https://github.com/microsoft/playwright-python/releases/download/v1.40.0/playwright-1.40.0-py3-none-manylinux1_x86_64.whl
pip install playwright-1.40.0-py3-none-manylinux1_x86_64.whl
```

### VLM API 返回 400 错误

通常是图片传输方式问题：

```bash
# 尝试不同传输方式
MYCODE_VLM_IMAGE_TRANSPORT=url bash ...  # 直接 URL
MYCODE_VLM_IMAGE_TRANSPORT=data_uri bash ...  # Base64
MYCODE_VLM_IMAGE_TRANSPORT=auto bash ...  # 自动降级
```

### Browser 超时

```bash
# 增加超时时间
MYCODE_BROWSER_TIMEOUT=60 bash ...

# 使用无头模式减少资源消耗
MYCODE_BROWSER_HEADLESS=1 bash ...
```

## 性能建议

| 模式 | 每样本时间 | 适用场景 |
|------|-----------|---------|
| Structure Only | ~30 秒 | 快速基线测试 |
| Lightweight | ~2-3 分钟 | 全量 92 样本 |
| + Network | +30 秒/样本 | 有 URL 的样本 |
| + Browser | +1-2 分钟/样本 | Playground 复现 |
| + VLM | +10-20 秒/图片 | 多模态图片分析 |
| Full MM | ~5-10 分钟 | 案例分析/论文实验 |

## 输出说明

启用 VLM/Browser 后，输出目录会增加：

```
result/<run_id>/
├── tool_cache/
│   ├── browser_reproduction.json    # Browser 缓存
│   ├── vlm_analysis.json            # VLM 分析结果
│   └── source_files/                # 下载的源码
├── llm_events.jsonl                 # LLM 调用记录 (含 VLM)
├── agent_traces.jsonl               # Agent 轨迹 (含 Browser 操作)
└── localization_results.jsonl       # 完整结果
```

## 参考文档

- `src/mycode/evidence/tools/vlm_image_reader.py` - VLM 实现
- `src/mycode/evidence/tools/browser_reproduction_reader.py` - Browser 实现
- `src/mycode/evidence/tools/web_snapshot.py` - Web Snapshot 实现
- `newtest/README.md` - Clean15 测试入口文档
