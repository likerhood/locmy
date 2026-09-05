# mycode Clean15 全量测试入口

这里是 `mycode` 原型自己的全量测试脚本，不调用 LocAgent/CoSIL/GALA/GraphLocator 的 runner。

默认输入：

- SWE Clean15: `/home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl`，92 条。
- OmniGIRL Clean15: `/home/like/locCode/clean_subsets_new/omnigirl-full-candidates.clean15.v458.samples.jsonl`，458 条。

默认不开真实网络、浏览器、VLM、LLM，先跑确定性的 evidence parsing + 三层定位评估。
默认 `STRUCTURE_ONLY=1`，只使用 benchmark 的 `repo_structures`，避免全量测试时反复扫描超大的本地仓库。
默认 `LIGHTWEIGHT=1`，使用适合 92/458 全量扫描的快速检索模式：不会构建昂贵的深度仓库图，也不会跑多轮 flow；输出仍包含 file/module/function 的 `acc@1/3/5/8/10/12/13/15`、`MRR@15`、`MAP@15`。

## 先跑小样本冒烟

```bash
cd /home/like/locCode/alltry/mycode

MAX_SAMPLES=1 bash newtest/run_swe_clean15_full.sh
MAX_SAMPLES=1 bash newtest/run_omni_clean15_full.sh
```

## 跑 clean15 全量

```bash
cd /home/like/locCode/alltry/mycode

bash newtest/run_swe_clean15_full.sh
bash newtest/run_omni_clean15_full.sh
```

## 打开真实 LLM evidence agent / controller

如果 `.env.local` 已经配置 `BASE_URL`、`API_KEY`、`MODEL_NAME`、可选 `MODEL_API_NAME`：

```bash
USE_LLM=1 \
USE_LLM_PLANNING=1 \
USE_LLM_CONTROLLER=1 \
MAX_SAMPLES=1 \
bash newtest/run_swe_clean15_full.sh
```

也可以直接在命令行选择模型。`--model` 同时设置结果目录中的模型标签和发送给 API 的模型 ID：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --model xopqwen35397b \
  --print-model-config
```

当展示名称和 API ID 不同时分别指定：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --model-label Qwen3.5-397B-A17B \
  --model-api-name xopqwen35397b
```

可以维护多个模型配置文件，并用 `--env-file` 选择。显式命令行参数和已有环境变量优先于配置文件：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen \
  --print-model-config
```

VLM 图片载荷可以在命令行中选择。`data_uri` 使用下载并校验后的本地图片，
`url` 直接传递原始 HTTP(S) 地址，`auto` 优先使用 `data_uri`，并在服务端
返回 400、415 或 422 时使用原始 URL 重试一次：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.aliyun-kimi.local \
  --vlm-image-transport auto \
  --print-model-config
```

配置文件还可以分别覆盖不同阶段：

```dotenv
MODEL_NAME=hybrid-run
MODEL_API_NAME=general-model-id
PLANNING_MODEL_API_NAME=planning-model-id
EVIDENCE_MODEL_API_NAME=evidence-model-id
CONTROLLER_MODEL_API_NAME=code-model-id
VLM_MODEL_API_NAME=vision-model-id
```

## 跑 SWE Clean15 真实 LLM 动态 Agent 全量

这个入口专门用于你现在要跑的版本：默认使用真实 LLM、打开 evidence planning、LLM controller，并把结果写到 `alltry/mycode/result/`。为了能跑完整 92 条，默认定位阶段使用可控的 lightweight 模式；深度 concern/call/flow 多轮模式用于小批量案例分析。

```bash
cd /home/like/locCode/alltry/mycode

bash newtest/run_swe_clean15_agent_full.sh
```

建议先跑 1 条确认 API、trace、token 统计都正常：

```bash
cd /home/like/locCode/alltry/mycode

MAX_SAMPLES=1 RUN_ID=agent_smoke FORCE_RERUN=1 \
bash newtest/run_swe_clean15_agent_full.sh
```

如果要真正打开网页/下载图片/VLM 图片理解：

```bash
cd /home/like/locCode/alltry/mycode

FULL_MM=1 RUN_ID=agent_full_mm \
bash newtest/run_swe_clean15_agent_full.sh
```

默认输出目录形如：

```text
/home/like/locCode/alltry/mycode/result/swe_clean15_agent_<MODEL_NAME>_<RUN_ID>
```

这个脚本和 `run_swe_clean15_full.sh` 的区别是：

- 默认 `USE_LLM=1`、`USE_LLM_PLANNING=1`、`USE_LLM_CONTROLLER=1`。
- 默认 `LIGHTWEIGHT=1`，适合全量 92 条真实 LLM 跑通并记录 trace/token。
- 如果设置 `DEEP_AGENT=1`，则默认 `LIGHTWEIGHT=0`，会走更重的 concern 横向搜索、call/used-by 纵向导航、flow 验证和重排。
- 默认 `STRUCTURE_ONLY=1`，先用 `repo_structures` 控制速度；需要扫描真实仓库源码时再显式设 `STRUCTURE_ONLY=0`。
- 默认 `RESUME=1`，中断后重跑会跳过已经成功写入的样本。
- 默认 `MYCODE_LLM_RETRIES=4`、`MYCODE_LLM_RETRY_DELAYS=10,30,60,100`，减少 API 瞬时失败导致全量中断。

深度案例跑法：

```bash
cd /home/like/locCode/alltry/mycode

DEEP_AGENT=1 INSTANCE_ID=Automattic__wp-calypso-21409 RUN_ID=deep_21409 \
bash newtest/run_swe_clean15_agent_full.sh
```

## 打开深度动态 Agent

深度模式会构建 typed repository graph，并执行 concern 横向搜索、call/used-by 纵向导航、flow 验证、读代码和重排。它适合论文案例分析，不建议直接默认跑全量。

```bash
LIGHTWEIGHT=0 \
USE_LLM=1 \
USE_LLM_PLANNING=1 \
USE_LLM_CONTROLLER=1 \
DYNAMIC_ROUNDS=3 \
INSTANCE_ID=chartjs__Chart.js-10301 \
bash newtest/run_swe_clean15_full.sh
```

常用开关：

- `MAX_SAMPLES=10`: 只跑前 10 条。
- `INSTANCE_ID=chartjs__Chart.js-10301`: 只跑指定样本。
- `OUTPUT_DIR=/path/to/out`: 指定输出目录，配合 `RESUME=1` 断点续跑。
- `FORCE_RERUN=1`: 删除旧输出后重跑。
- `STRUCTURE_ONLY=0`: 同时扫描已 checkout 的真实仓库源码，信息更完整但会明显变慢。
- `LIGHTWEIGHT=0`: 打开深度 concern/call/flow 动态 Agent。
- `ALLOW_NETWORK=1`: 允许 URL/docs/playground 抓取。
- `ALLOW_BROWSER=1`: 允许浏览器工具。
- `DOWNLOAD_IMAGES=1 USE_VLM=1`: 下载图片并调用 VLM 分析。

每个输出目录包含：

- `localization_results.jsonl`: 每条样本完整 evidence、动态定位、flow trace、三层评估。
- `agent_traces.jsonl`: 精简后的 ReAct/搜索/flow 轨迹，适合人工看失败案例。
- `llm_events.jsonl`: 每个样本抽取出的 LLM planning、understanding、controller、ReAct 输出。
- `token_usage.jsonl`: 每个样本显式记录到的 LLM token 用量。
- `progress.jsonl`: 每个样本开始/结束/失败的进度记录，适合长跑时监控。
- `per_instance_metrics.csv`: 每条样本一行指标。
- `metrics_summary.json`: 整体指标。
- `metrics_summary.md`: 可读 summary。
- `failures.jsonl`: 异常样本。
- `run.log`: shell 层运行日志。

## 重评估已有结果

如果已经有 `localization_results.jsonl`，只想按最新三层评估口径重新输出表格，不需要重新跑定位：

```bash
cd /home/like/locCode/alltry/mycode

python3 newtest/eval_existing_results.py \
  --result-dir result/swe_clean15_20260828_230138 \
  --dataset swebench_multimodal-full-dev-clean15 \
  --model-name mycode-structure-lightweight \
  --sync-result-summary \
  --write-augmented
```

输出会写到 `<result-dir>/eval_latest/`：

- `metrics_3level.md`: 宽松 Acc/Recall/MRR/MAP、严格 Acc、Set Metrics @All/@8/@10/@15。
- `metrics_3level.json`: 机器可读汇总。
- `per_instance_metrics_3level.csv`: 每个样本的三层指标。
- `metric_definitions.md`: 指标口径说明。
- `augmented_predictions.jsonl`: 可选，写入补齐新指标后的完整结果。

加上 `--sync-result-summary` 后，还会把最新报告同步到结果目录根部的
`metrics_3level.*`、`metrics_summary.*` 和 `per_instance_metrics_3level.csv`，
适合旧实验完成后升级报告格式，不会重新运行定位或调用 LLM。
