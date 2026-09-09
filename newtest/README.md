# mycode Clean15 全量测试入口

这里是 `mycode` 原型自己的全量测试脚本，不调用 LocAgent/CoSIL/GALA/GraphLocator 的 runner。

默认输入：

- SWE Clean15: `/home/like/locCode/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl`，92 条。
- OmniGIRL Clean15: `/home/like/locCode/clean_subsets_new/omnigirl-full-candidates.clean15.v458.samples.jsonl`，458 条。

默认关闭 evidence 阶段的网页网络、浏览器、VLM 和 LLM，先跑确定性的 evidence parsing
以及三层定位评估。仓库资产下载由独立的 `MYCODE_AUTO_FETCH_REPOS` 控制，默认仅在本地
结构与 checkout 都缺失时启用。
默认 `STRUCTURE_ONLY=1`，优先复用 benchmark 已有的 `repo_structures`。如果把
`mycode` 独立移出当前多项目目录且结构文件缺失，runner 会按样本中的 `repo` 和
`base_commit` 首次下载精确版本、生成结构快照并写入 `.mycode_cache/`；后续运行直接复用。
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

真实 LLM 启动器必须通过 `--env-file` 显式选择包含 `BASE_URL`、`API_KEY`
和 `MODEL_API_NAME` 的配置文件。没有选择、文件不存在或必需字段为空时，启动器会在
处理第一个样本前退出，不再自动回退到 `offline`：

```bash
USE_LLM=1 \
USE_LLM_PLANNING=1 \
USE_LLM_CONTROLLER=1 \
MAX_SAMPLES=1 \
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local
```

也可以直接在命令行选择模型。`--model` 同时设置结果目录中的模型标签和发送给 API 的模型 ID：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local \
  --model xopqwen35397b \
  --print-model-config
```

当展示名称和 API ID 不同时分别指定：

```bash
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local \
  --model-label Qwen3.5-397B-A17B \
  --model-api-name xopqwen35397b
```

可以维护多个模型配置文件，并用 `--env-file` 选择。所选文件会替换终端中遗留的
provider/model 变量，显式 `--model` 参数拥有最终优先级：

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

bash newtest/run_swe_clean15_agent_full.sh --env-file .env.qwen.local
```

建议先跑 1 条确认 API、trace、token 统计都正常：

```bash
cd /home/like/locCode/alltry/mycode

MAX_SAMPLES=1 RUN_ID=agent_smoke FORCE_RERUN=1 \
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local
```

如果要真正打开网页/下载图片/VLM 图片理解：

```bash
cd /home/like/locCode/alltry/mycode

FULL_MM=1 RUN_ID=agent_full_mm \
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local
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
bash newtest/run_swe_clean15_agent_full.sh \
  --env-file .env.qwen.local
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

## 独立仓库模式

仓库资产的解析顺序为：外部已有 `repo_structures`、`mycode` 自有结构缓存、目标
`base_commit` 的现有 checkout、首次下载。这个顺序保证当前 LocAgent/MM-IR 等目录仍
可直接复用，不会因为启用独立模式而重新下载。

```dotenv
MYCODE_AUTO_FETCH_REPOS=1
MYCODE_REPO_CACHE_DIR=.mycode_cache
MYCODE_REPO_REMOTE_TEMPLATE=https://github.com/{repo}.git
```

首次运行需要系统已安装 `git` 且能够访问仓库远端。下载使用样本的 `base_commit`，
不会读取 `patch`、`files` 等 gold 字段来生成结构。缓存包含共享 Git 对象、按 commit
隔离的 detached checkout，以及按数据集/样本保存的结构 JSON。若需要严格离线运行或
检查资产完整性，可设置 `MYCODE_AUTO_FETCH_REPOS=0`，此时缺失资产会产生
`missing_repo_index`，不会悄悄定位到错误 commit。

也可直接控制 runner：

```bash
# 默认行为：缺失时自动获取
bash newtest/run_swe_clean15_full.sh

# 完全禁止仓库下载
MYCODE_AUTO_FETCH_REPOS=0 bash newtest/run_swe_clean15_full.sh
```

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

深度定位还会在 `localization.rank_stage_snapshots` 中记录 FastSeed、每轮检索、
最佳轮、跨轮融合、HeadSelector 和修改闭包后的前 15 名。默认排序采用
`10 recall -> 6 responsibility -> 3 active seeds`，最多锁定两个已有源码机制证据的
候选；最终 HeadSelector 只裁决 Top-1，其余候选保持原相对顺序。

运行完成后可生成阶段级 File Acc/MRR 报告：

```bash
python scripts/evaluate_rank_stages.py result/<run-directory>
```

报告写入 `rank_stage_metrics.md` 和 `rank_stage_metrics.json`。工具优先读取体积较小的
`agent_traces.jsonl`，用于定位究竟是哪一阶段改善或损害了 Acc@1-6。

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
