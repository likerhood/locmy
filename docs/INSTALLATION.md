# MAGNET 安装、运行与能力说明

本文档是独立克隆 `mycode` 后的标准入口。API 密钥只放在本地 `.env.*.local`，不要提交到 Git。

## 1. 固定环境

推荐使用项目内 `.venv`：

```bash
cd /path/to/mycode
bash scripts/bootstrap_environment.sh
```

该脚本依据 `requirements.lock` 安装固定版本的 Python 依赖，并下载与 Playwright 版本匹配的 Chromium。首次安装还需要一次系统库安装：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
.venv/bin/python scripts/runtime_preflight.py --require-browser
```

也可以使用 Conda：

```bash
conda env create -f environment.yml
conda activate magnet
python -m playwright install chromium
sudo python -m playwright install-deps chromium
python scripts/runtime_preflight.py --require-browser
```

当前机器已安装 Python 包和 Chromium，但 Chromium 启动仍缺少 `libnspr4.so` 等 Linux 系统库；在执行上述 `install-deps` 前，`ALLOW_BROWSER=1` 只表示允许调用，真实浏览器会降级为静态网页或 URL 证据。

## 2. 模型配置

从模板创建本地配置并填写真实值：

```bash
cp .env.example .env.provider.local
```

最少需要：

```ini
BASE_URL=https://provider.example/v1
API_KEY=replace-me
MODEL_NAME=model-label
MODEL_API_NAME=model-id-sent-to-api
VLM_MODEL_API_NAME=vision-capable-model-id
MYCODE_VLM_IMAGE_TRANSPORT=auto
```

`auto` 会优先使用已下载并校验过的图片；提供方拒绝 data URI 时回退到 HTTP URL。图片无法下载、格式伪装或 VLM 返回 400/500 时，样本不会整体失败，而是保留文本、URL 和仓库结构通道继续定位。

## 3. 标准运行

运行 SWE Clean15 的 92 个样本：

```bash
bash scripts/run_swe_clean15_addtest.sh --env-file .env.provider.local
```

该脚本封装了论文实验使用的高质量搜索预算。默认生成带时间戳的新 `RUN_ID`。中断后续跑时必须显式复用原 ID：

```bash
RUN_ID=swe_clean15_addtest_20260908_210000 RESUME=1 \
  bash scripts/run_swe_clean15_addtest.sh --env-file .env.provider.local
```

等价的手动配置方式为：

```bash
set -a
source configs/runtime/full_mm.env
set +a

DEEP_AGENT=1 RUN_ID=swe_clean15_fullmm_v1 RESUME=1 \
  bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.provider.local
```

启动脚本会自动优先使用 `<repository>/.venv/bin/python`，并在结果目录写入 `runtime_capabilities.json`，记录 Python、依赖、浏览器实际可用性、Git commit、未提交改动哈希和关键实验参数。若论文实验要求浏览器必须真实执行，增加：

```bash
MYCODE_BROWSER_REQUIRED=1
```

这样预检失败会在处理样本前终止，避免把“允许浏览器”和“浏览器确实运行”混为一组实验。

## 4. 独立仓库与缓存

首次遇到没有现成结构文件的 `repo + base_commit` 时，MAGNET 会：

1. 在 `.mycode_cache/repos` 创建或复用 Git mirror；
2. 为指定 `base_commit` 创建 checkout；
3. 生成仓库结构索引并写入 `.mycode_cache/repo_structures`；
4. 将 checkout 交给 GitHub URL 本地解析、浏览器证据映射和源码读取。

再次运行相同提交时直接复用缓存。若外层工作区已有 LocAgent、MM-IR、CoSIL、GraphLocator 或 GALA 结构，仍优先兼容并复用；只有缺失资产才自动下载。`result/`、`.mycode_cache/`、`.env.*` 和工具缓存均已被 `.gitignore` 排除。

## 5. 图与数据流能力边界

当前实现包含：

- 文件、类、函数级异构图，以及 import、call、inheritance、render、state、style 和 config 等有向边；
- incoming/outgoing 双向扩展和受预算约束的多跳图搜索；
- Python、JavaScript/TypeScript、Java 的跨函数参数传播、返回值、状态读写及跨文件 import/call 追踪；
- 语句级局部 def-use 静态切片、调用边界摘要和可选运行时轨迹。

这些分析已经接入深度模式，`MYCODE_FLOW_MODE=auto` 与 `MYCODE_DEEP_FLOW_AUTO=1` 会在候选冲突或证据不足时触发。它们是面向多语言仓库的容错启发式分析，不等同于 CodeQL/SCIP 的编译器级完备数据流；动态轨迹也依赖项目能够运行并触发目标路径。因此论文中应表述为“跨过程流证据”或“best-effort interprocedural flow validation”，不宜声称完整程序切片。

## 6. 快速验证

```bash
.venv/bin/python -m pytest -q \
  tests/test_url_browser_seed_pipeline.py \
  tests/test_standalone_repository_assets.py \
  tests/test_typed_graph_navigation_policy.py \
  tests/test_call_dataflow_dynamic_agent.py \
  tests/test_static_slice_runtime_trace.py
```

浏览器单独检查：

```bash
.venv/bin/python scripts/runtime_preflight.py --require-browser --json
```
