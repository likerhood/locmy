# addtest32：Qwen 消融实验运行说明

## 实现范围与命名

本实现从 addtest3 出发，支持下列独立组；不是 addtest31/addtest4 的融合版。

| --arm | 实际干预 | 解释边界 |
| --- | --- | --- |
| full | 保留方法能力 | 新实现上的 Full 对照，需要重新跑 |
| no_visual | 不生成图片 inspections、不执行图像读取/VLM、不截图 | 保留 issue 文本与图片引用、网页 DOM/文本、浏览器交互，不是关闭浏览器 |
| no_graph | 不构建 TypedRepositoryGraph 边，不执行 NavigateCode | 同时移除闭包使用的该图邻接关系；独立 flow 后端仍可读取源码构建自身局部关系 |
| no_flow | 禁用七类 flow 后端及两个 flow 调度入口，不生成确定性 flow obligation | 保留关系图和 issue 文本中的状态/调用概念；不等于删除模型自身的因果推理 |
| no_graph_flow | 同时执行上面两种干预 | 用于结构导航/flow 组合对照 |
| no_closure | 闭包返回禁用状态，不扩展、不提升闭包候选 | 最终 patch set 不适用；空集合指标不能作为该组 patch-set 效果报告 |
| fixed_react | 固定 ReAct 工具循环与本次调用的初始查询，不调用 ReAct planner | 仍保留外层动态搜索、证据规划、reviewer、外层停止逻辑；不能称为“无动态 agent” |
| no_head | 关闭 HeadSelector | 后续闭包可能仍改变排名 |
| no_checkpoint | 关闭最佳轮选择，使用末轮 | 仍保留跨轮候选融合 |

相较最初规划，本版 no_graph 的边界包含闭包邻接，flow 后端并非严格限制为候选内验证；因此组合实验测量上述两个实际通路的总效应，不将其解释为完全独立的两套图算法。fixed_react 是可复现的局部策略消融，不是原规划的全流程固定搜索策略。
所有提示词新增内容为英文。Full 不改变既有 ReAct 提示词文本。

## 服务器准备

推送后，在原仓库执行一次，创建独立目录。不要在仍运行实验的目录切换分支。

```bash
cd /data2/like/locmy
git fetch origin
git worktree add --track -b addtest32 /data2/like/locmy-addtest32 origin/addtest32
cd /data2/like/locmy-addtest32
```

如果该目录/分支已经存在，进入该 worktree 后执行 `git pull --ff-only`，不要重复 worktree add。
可以沿用原 Python 环境，启动器会把新 worktree 的 `src` 放在 PYTHONPATH 最前面；不会修改旧环境的 editable install。
若报缺依赖，再准备独立环境。环境文件不会提交到 Git，应保留服务器原来的 Qwen 配置文件。

```bash
export PYTHON_BIN=/data2/like/locmy/.venv/bin/python
export QWEN_ENV=/data2/like/locmy/.env.local
export SAMPLES=/data2/like/locmy/data/swebench_multimodal-full-dev.clean15.samples.jsonl
```

QWEN_ENV 可以改为实际使用的阿里云 Qwen 环境文件，但所有组必须使用同一 provider/model/thinking 配置。启动器拒绝非 Qwen 的主模型和阶段模型；API 密钥不会写入 manifest。

## 先检查，再用两条样本冒烟

dry-run 只校验配置、输入数量、重复 ID 和输出目录，不调用模型、不启动浏览器、不证明 API 连通。

```bash
bash scripts/run_ablation_qwen.sh \
  --dataset swe --arm full --tag check \
  --env-file "$QWEN_ENV" --dry-run

MAX_SAMPLES=2 bash scripts/run_ablation_qwen.sh \
  --dataset swe --arm full --tag smoke_v1 \
  --env-file "$QWEN_ENV"
```

建议每组都先冒烟，下面循环串行执行，不会并行建图。MAX_SAMPLES 只作为命令前缀，不污染正式运行环境。

```bash
for arm in full no_visual no_graph no_flow no_graph_flow no_closure fixed_react; do
  MAX_SAMPLES=2 bash scripts/run_ablation_qwen.sh \
    --dataset swe --arm "$arm" --tag smoke_all_v1 \
    --env-file "$QWEN_ENV" || break
done
```

前两条只验证可运行性，不能据此判断效果。论文诊断抽样应另按仓库分层，不把顺序前两条称为代表性样本。

## SWE 正式消融

```bash
unset MAX_SAMPLES INSTANCE_ID
for arm in full no_visual no_graph no_flow no_graph_flow no_closure fixed_react; do
  bash scripts/run_ablation_qwen.sh \
    --dataset swe --arm "$arm" --tag qwen_r1 \
    --env-file "$QWEN_ENV" || break
done
```

如需要排序策略诊断，再单独跑 `no_head`、`no_checkpoint`；不要根据结果只保留有利组。
默认最大动态轮数 12、证据工具轮数 6、ReAct 步数 10、Top-K 15。通过原环境变量可覆盖，但所有组必须一致，并核对 manifest 与 runtime_capabilities。

## Omni 正式消融

与 SWE 只改变数据集选择和样本文件：

```bash
export SAMPLES=/data2/like/locmy/data/omnigirl-full-candidates.clean15.v458.samples.jsonl
unset MAX_SAMPLES INSTANCE_ID
for arm in full no_visual no_graph no_flow no_graph_flow no_closure fixed_react; do
  bash scripts/run_ablation_qwen.sh \
    --dataset omni --arm "$arm" --tag qwen_r1 \
    --env-file "$QWEN_ENV" || break
done
```

7 组全量为 3206 次样本运行，不是 3206 次 LLM 调用，成本较高。先确认 SWE 与 Omni 冒烟结果，再启动全量。
输入必须是完整 92/458 条，部分运行用 MAX_SAMPLES/INSTANCE_ID；SHA256 记录完整输入文件，selection 记录筛选条件。

## 结果在哪里

固定目录结构，不依赖继承的 OUTPUT_DIR/RUN_ID/CACHE_DIR：

```text
mycode-addtest32/result/ablation_addtest32/
  swe或omni/
    实际MODEL_API_NAME经过路径字符清洗/
      qwen_r1/
        full/
        no_visual/
        no_graph/
        no_flow/
        no_graph_flow/
        no_closure/
        fixed_react/
```

例如 MODEL_API_NAME=xopqwen35397b，则 SWE Full 位于：
`/data2/like/locmy-addtest32/result/ablation_addtest32/swe/xopqwen35397b/qwen_r1/full/`。

每组主要文件：

- `ablation_manifest.json`：分组、代码提交、数据 SHA256、模型名、Python、选择条件和请求预算。不含 API_KEY。
- `runtime_capabilities.json`：实际环境与现有运行配置，源码/浏览器环境检查。
- `metrics_summary.md`、`metrics_3level.md`：汇总结果；先检查分母与成功/失败数，不只看 Acc。
- `per_instance_metrics.csv`、`per_instance_metrics_3level.csv`：配对分析首选的小文件。
- `phase_events.jsonl`：每样本开头的 ablation 事件及阶段信息。
- `agent_traces.jsonl`、`localization_results.jsonl`：详细轨迹，可能很大，按行按 ID 分析。
- `run.log`：终端日志；`launcher_status.json`：下游启动器退出码，不等于所有样本都成功。

为了避免污染，已有输出目录一律拒绝复用，启动器强制 RESUME=0。失败后使用新的 tag，例如 `qwen_r2`；本版不提供断点续跑保证。
如果 profile 自定义覆盖了预算或其他运行变量，以 runtime_capabilities 和日志为准；建议模型配置文件只放 provider/model 参数，不混入实验策略开关。

## 怎样比较

按相同实例 ID 配对，主要比较 File Acc@1/3/8、MRR@15，并检查 Acc@15、Function/Module、SL@15、REC@15与成本。no_closure 的 patch set 标为不适用，仍可比较 ranked-list SL/REC。
每组应保留失败样本分母。只在同一冻结输入和 gold 口径下比较，历史 Omni baseline 的 458 条不一定与当前 458 条相同。
测试通过只能说明开关与程序行为符合测试，并不证明模块有效、更不证明超过 baseline。本次没有启动付费 API 全量实验。
