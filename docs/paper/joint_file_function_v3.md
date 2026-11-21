# 文件与函数联合定位 v3：实现、验证与运行说明

> 最终验证已于2026-11-22完成：固定八例的16个最终有效配对结果，模型为MiMo-v2.6-pro，两臂同为4轮/12步上限。文件A1为12.5%→62.5%，函数A15为25%→37.5%；134项相关回归通过、1项跳过。实验发布口径通过，旧严格保护条件未全部满足。代码检查点已于前一晚23:53推送；本文件与轻量指标为追加结果提交。详见第7节，所有局部退化均保留。

## 1. 版本与授权边界

基线为 `magnet-kimi-compat-v3` 的 `fa574b260fc9fed4518a587912379acd4741d621`。开发与发布分支为 `magnet-kimi-compat-v3-fileloc-v1`；分支名字沿用，当前算法版本为联合定位 v3。

用户已明确调整发布要求：不限制步数/token成本；文件或函数有改善即可提交实验分支，不再把“两类全部截点同时非退化”作为阻止提交的条件；所有退化仍必须披露。仅推独立分支，不合并主分支、不强推、不自动启动服务器全量任务。

本次先在模型测试进行时发布通过代码回归的**实验实现检查点**，现已完成测试并追加结果记录。不能把检查点当时的“代码已推送”解释为当时已完成模型验证。旧严格 gate 原样保留，新实验发布判断单列，不改写旧实验结论。

检查点提交为 `92443318ccfcf16502cefba4faa453ebc293a2ef`，北京时间2026-11-21 23:53已推送到上述独立分支。冻结模型运行在提交前启动，但实现哈希绑定相同源码；提交动作没有改变运行中的实现。

## 2. 为什么文件改善不必然带来函数改善

文件与函数不是同一个排序：文件有 precision、head、closure；函数经历实体检索、源码/流核验和跨轮合并。正确文件前移只能减少找错文件的风险，不能保证其中正确函数被生成、被读取、被核验或最终保留。大文件中的无关函数、嵌套同名函数和多文件修改责任仍需要单独解决。

旧v2.1轨迹例子：Calypso21648 文件 2→1，但函数覆盖 4/8→1/8；第二轮曾发现另外两个正确函数，最后因“只出现一轮、缺少实现窗口”没有保留。这不是文件排名能自动修复的交接问题。新v3的独立配对和阶段消融见第7节，不能混用两轮实验数字。

## 3. 本版已实施

### 3.1 文件：保留有效排序，撤掉有害末端全局融合

继续使用显式请求路径实际读取、查询聚焦源码窗口、precision/head/closure。删除运行路径中的最终全局文件 RRF 调用，因此不会再把 p5.js3709 的正确文件由 closure 第3硬降至11，也不会把 marked1435 的正确文件由第1硬降至10。

文件排序不从函数数量或函数名机械加分，也不在 generated-output demotion 后再从跨轮池引入新文件。日志中保留 `preserve_precision_closure_order_v3` 的前后顺序，便于证明这一不变量。

### 3.2 函数：真实实现复读与跨轮证据账本

对每轮前15函数的并集按最佳历史名次确定有界读取顺序。每条记录必须匹配当前索引中的完整实体 ID、起止行；从真实文件读取对应实现，不信任未经匹配的缓存文本，不读取 gold 或补丁。

账本记录：实际源码范围、最多两个实现窗口、issue/body 匹配词、已有程序流支持、已有精确实体 review、预算耗尽/索引不匹配/证据不足等原因。

一次性发现可保留的门槛为：真实源码窗口、至少两个具体 issue/body 语义词，以及程序流支持或已有精确 patch-target 实体 review。它是可检验的启发式，不是正确性证明；普通词法命中不能单独过门槛。

函数仍独立进行等权跨轮排序，不继承文件检查点额外一票。最多4个高证据瞬时发现可保留在尾部，不能通过保留池强行改写前三名；不无条件合并所有轮函数。不同文件的同名函数以完整实体 ID 区分。

### 3.3 默认关闭与可单独关闭的部分

```bash
# 原版配对臂
export MYCODE_READ_EXPLICIT_PATHS=0
export MYCODE_FILE_FRONT_RERANK=head_only
export MYCODE_JOINT_EVIDENCE_V2=0
export MYCODE_FUNCTION_SOURCE_LEDGER=0

# v3改版臂
export MYCODE_READ_EXPLICIT_PATHS=1
export MYCODE_FILE_FRONT_RERANK=verified_frontier
export MYCODE_JOINT_EVIDENCE_V2=1
export MYCODE_FUNCTION_SOURCE_LEDGER=1
export MYCODE_FUNCTION_REREAD_LIMIT=48
```

`MYCODE_JOINT_EVIDENCE_V2` 沿用原实验开关名，当前开启后为 v3 实现；默认0。函数账本可设0单独消融。源码复读默认上限24，本次测试显式设48。未改 Sketch、scope、停止判定逻辑；提高的是可配置最大预算，不要求每个任务用完。

## 4. 验证协议与历史实验区分

相关单元和回归：134 passed、1 skipped，`git diff --check` 通过。新增测试覆盖真实源码读取、独立证据门槛、错误行范围、预算耗尽、单轮发现尾部保留、最终文件顺序保持，以及新发布口径披露另一粒度退化。

固定旧轨迹回放只用于确定性末端策略检查，不能冒充更高预算的新模型成绩。旧 v2.1 八例有文件 A1 提升但 A8/保护例退化，仍保留在项目根 `allresultwork/015_KimiV3文件与函数联合优化_实现与验证记录_20261121.md`。

新鲜 MiMo-v2.6-pro 固定八例原版/改版配对在独立目录运行并已完成：4轮、每轮最多12个 ReAct步骤、Top15、每任务900秒异常保护、函数复读上限48。两臂相同最大搜索预算，不能与此前3轮/8步结果直接作无控制的算法增益比较。

Chart.js一臂曾发生媒体HTTP400，原始整对保留，并在同代码、模型、输入、预算的新目录重跑整对；最终仅使用重跑的该对，不对有效差成绩择优重试。

八例固定为 p5.js3709、Calypso21648、Calypso22026、Chart.js10806、Babel15349、marked1262、marked1435、p5.js3068。无全量本地运行。完整源码副本、哈希、冻结任务输入和所有负例均保存；输入删除 gold、patch、test_patch 和测试标签，结束后才评分。

## 5. 复现小范围测试

从本仓库目录执行；凭据文件不提交，替换路径为机器实际位置。输出目录必须全新，不能覆盖旧结果。

```bash
python scripts/fileloc_pair_smoke.py \
  --workspace /path/to/locCode --env-file /path/to/.env.mimo.local \
  --model-api-name mimo-v2.6-pro --workers 2 \
  --dynamic-rounds 4 --max-react-steps 12 --sample-timeout-seconds 900 \
  --function-reread-limit 48 --run-dir /path/to/new_joint_v3_run
python scripts/score_fileloc_pairs.py \
  --workspace /path/to/locCode --run-dir /path/to/new_joint_v3_run \
  --release-policy any-gain-experimental
```

模型或媒体接口失效的输出不计有效成绩；异常重跑必须同代码/模型/输入/预算，整对替换且保留原始记录，不能对有效差成绩择优重试。原版和改版的输出、cache 分开；源码在批内发生改变则拒绝继续。

## 6. 后续全量验证安排

模型小范围结果、逐例首个正确名次、A1/A3/A5/A8/A15、SL15/REC15、阶段变化及账本副作用已整理至第7节和项目根报告。代码可实验发布不等于两类指标必然同时改善，更不等于全量优于 CoSIL/LocAgent。

服务器可按相同模型、相同预算、原版与改版独立目录运行 SWE92 和 Omni296；不要使用默认 Omni458 替代296。此时仅提供命令，由服务器人员启动，不从本机启动全量任务。对于 Calypso22026 全轮候选均未命中的情况，预算增加只能提供机会，不能代替必要的召回来源诊断。

### 服务器命令示例（本机没有执行）

在服务器检出本分支后，从仓库目录执行。依赖沿用可运行 v3 基线的 Python 环境；需要已有对应任务的 repo_structures、SWE92/Omni296输入及冻结gold。模型凭据通过 `ENV_FILE` 装载，不把凭据文件提交到Git。示例复现MiMo组；若使用Kimi，需要两臂同时改为Kimi模型和profile，另建结果组。

```bash
export LOC_CODE_ROOT=/path/to/locCode
export ENV_FILE=/path/to/.env.mimo.local
export LOAD_ENV_FILE=1
export MODEL_API_NAME=mimo-v2.6-pro
export MODEL_NAME=mimo-v2.6-pro
export PLANNING_MODEL_API_NAME=mimo-v2.6-pro
export EVIDENCE_MODEL_API_NAME=mimo-v2.6-pro
export CONTROLLER_MODEL_API_NAME=mimo-v2.6-pro
export VLM_MODEL_API_NAME=mimo-v2.6-pro
export MYCODE_MODEL_PROFILE=mimo
export MYCODE_FAST_SEED_PLANNER=1
export MYCODE_FAST_SEED_LLM=1
export MYCODE_LLM_CANDIDATE_REVIEW=1
export MYCODE_LLM_RETRIES=1
export MYCODE_FUNCTION_REREAD_LIMIT=48
export MYCODE_PHASE_LOG=1
# 改版；原版需将前三个1设0，并将verified_frontier改成head_only。
export MYCODE_READ_EXPLICIT_PATHS=1
export MYCODE_JOINT_EVIDENCE_V2=1
export MYCODE_FUNCTION_SOURCE_LEDGER=1
export MYCODE_FILE_FRONT_RERANK=verified_frontier

# RUN_ID替换为新的唯一标识；目录已存在则不运行。
test ! -e "$LOC_CODE_ROOT/result/joint_v3_swe92_improved_RUN_ID" && \
python newtest/run_clean15_dataset.py \
  --samples "$LOC_CODE_ROOT/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl" \
  --dataset swebench_multimodal-full-dev \
  --output-dir "$LOC_CODE_ROOT/result/joint_v3_swe92_improved_RUN_ID" \
  --top-k 15 --dynamic-rounds 4 --max-react-steps 12 --max-tool-rounds 1 \
  --structure-only --no-auto-fetch-repos --allow-network --download-images \
  --use-llm --use-llm-planning --use-llm-controller --use-vlm \
  --sample-timeout-seconds 900 --sample-heartbeat-interval 30

test ! -e "$LOC_CODE_ROOT/result/joint_v3_omni296_improved_RUN_ID" && \
python newtest/run_clean15_dataset.py \
  --samples "$LOC_CODE_ROOT/collected_results/omin296/data/samples296.jsonl" \
  --dataset omnigirl-full-candidates \
  --output-dir "$LOC_CODE_ROOT/result/joint_v3_omni296_improved_RUN_ID" \
  --top-k 15 --dynamic-rounds 4 --max-react-steps 12 --max-tool-rounds 1 \
  --structure-only --no-auto-fetch-repos --allow-network --download-images \
  --use-llm --use-llm-planning --use-llm-controller --use-vlm \
  --sample-timeout-seconds 900 --sample-heartbeat-interval 30
```

原版必须先设置上述原版开关，并将输出目录中的 `improved` 改成 `baseline`、使用同一 RUN_ID 组，不复用cache。两套数据均需使用各自冻结统一 gold 做最终重评分；运行器自身原始gold汇总不能与不同口径旧表直接拼接。

## 7. 完整模型配对结果

轻量可机读结果：[joint_file_function_v3_scores.json](joint_file_function_v3_scores.json)。其中 `code_commit` 为 `92443318ccfcf16502cefba4faa453ebc293a2ef`，导出时逐文件核验Git提交中的实现哈希与冻结运行一致。后续结果/文档提交不改变该次推理实现。原版是本分支关闭实验开关的MAGNET对照路径，不是新跑的CoSIL或LocAgent。

| 任务 | 文件首个正确名次 原版→改版 | 函数首个正确名次 原版→改版 | 函数Top15覆盖 原版→改版 |
| --- | --- | --- | --- |
| p5.js3709 | 2→3 | gold为空 | 不适用 |
| Calypso21648 | 3→1 | 5→2 | 5/8→2/8 |
| Calypso22026 | 均未命中 | 均未命中 | 0/6→0/6 |
| Chart.js10806 | 未命中→1 | 未命中→1 | 0/4→3/4 |
| Babel15349 | 12→1 | gold为空 | 不适用 |
| marked1262 | 1→1 | 2→1 | 3/3→3/3 |
| marked1435 | 2→1 | gold为空 | 不适用 |
| p5.js3068 | 2→3 | gold为空 | 不适用 |

“未命中”指Top15未命中，非名次第0。函数有4例gold为空，不应解读为定位失败。

| 粒度 | 指标 | 原版% | 改版% |
| --- | --- | ---: | ---: |
| 文件 | A1 | 12.50 | 62.50 |
| 文件 | A3/A5/A8 | 62.50 | 87.50 |
| 文件 | A15 | 75.00 | 87.50 |
| 文件 | SL15 | 50.00 | 62.50 |
| 文件 | REC15 | 65.18 | 75.89 |
| 函数 | A1 | 0.00 | 25.00 |
| 函数 | A3 | 12.50 | 37.50 |
| 函数 | A5/A8/A15 | 25.00 | 37.50 |
| 函数 | SL15 | 12.50 | 12.50 |
| 函数 | REC15 | 20.31 | 25.00 |

固定分母N=8；函数gold非空N=4时，函数A15为50%→75%，平均召回40.625%→50%。这8例是用于开发的固定选择案例，不是独立抽样测试集，不能据此宣称全量超过baseline、统计显著或Kimi模型同样受益。

旧严格gate仅`protection_A1`未满足：p5.js3068改版第3而非第1。实际本轮对照也是第2，不能说“把当前原版A1毁掉”。按用户新授权，实验发布允许任一粒度改善并披露退化：本次两类聚合指标均有收益，`eligible_for_experimental_push=true`；这不代表每例非退化或可以自动合并主分支。

## 8. 阶段证据、负例与下一步

### 8.1 已证实的有效部分与边界

文件末端不再全局RRF，joint前后顺序相同，避免旧版本对正确文件的硬降级和重新引入bundle。Chart及Babel在当前新鲜配对中分别由未命中/第12提升到第1，marked1262函数由第2到第1；这些是联合版本的现象，不是每个组件独立贡献的证明。

两例p5均有文件第2→第3的局部退化。p5.js3709改版各轮已是第3，最终没有再被末端RRF压到11；p5.js3068第一轮第1、后续第2，选中后又被seed保留推到第3。后者应审检查点与种子恢复，不应继续归罪于已删除的文件RRF。

### 8.2 函数账本不能被包装成已验证的收益来源

全八例统一固定轨迹消融见 [joint_file_function_v3_stage_ablation.json](joint_file_function_v3_stage_ablation.json)，脚本为 `scripts/analyze_joint_stage_ablation.py`。这是确定性末端消融，不是新模型运行。

| 改版轨迹上的函数末端策略 | A1% | A3% | A15% | REC15% |
| --- | ---: | ---: | ---: | ---: |
| 仅保留选中轮函数 | 37.50 | 37.50 | 37.50 | 28.125 |
| 跨轮融合但关闭源码账本 | 25.00 | 37.50 | 37.50 | 26.5625 |
| 本次在线策略：跨轮融合+账本 | 25.00 | 37.50 | 37.50 | 25.00 |

直接证据：Calypso21648选中轮函数首个正确位置为1、覆盖4/8；无账本融合为3/8，在线融合+账本只剩2/8。其账本29条真实读取中20条被启发式标为strong，但尾部保留的三个新增函数均非gold。Chart选中轮已覆盖3/4，在线仍3/4，账本插入的另一函数也非gold。因此**当前证据不支持“账本带来本轮函数收益”的结论**；源码/流关联与修改责任之间的门槛仍过宽。

### 8.3 下一轮规划，按优先级而非一味加步数

1. **函数末端先减法。** 对已有高质量选中轮函数采用保持头部与责任覆盖的策略；将跨轮函数保留改为有明确状态/效果/修改责任证据的有限补充，先消融全局RRF和低区分度保留池。仅有两个issue词与潜在flow不能视为责任确认。
2. **停止条件补实体核验。** Calypso21648第三轮因`high_confidence_source_candidate`停止，文件置信0.942、`stable_top1=true`，但`review_stop_ready=false`且program轴为false。文件准备好不等于实体集合准备好。下一实验可对“文件已确认而实体/责任仍有缺口”增加独立实体读取/核验预算，而非重复全局搜索。当前尚未实现，也不能保证硬跑第四轮一定提升。
3. **检查点与种子校准。** 针对p5.js3068保留第一轮与后续轮的责任证据差异，约束无核验种子压过已有源码责任证据；不要按gold锁第一名。
4. **召回问题单列。** Calypso22026两臂均未命中，需要检查正确文件是否进入初始检索、工具scope及导航边，不用末端排序或再读同一批候选替代召回。
5. **验证顺序。** 先对八例完整保存轨迹做统一消融，再冻结新配置进行小范围模型配对，最后由服务器做SWE92/Omni296全量。不能从多个有效运行中择优形成“最好表”。

本次两臂实际搜索轮数总计32→28，主ReAct记录步数90→84，改版额外实际复读186个实体；主ReAct记录不包含所有后续动作。这些数字不能直接推导全链路token成本或效率优势，token记录也不代表完整账单。少执行轮次可能包含过早停止，不能单独视为优势。
