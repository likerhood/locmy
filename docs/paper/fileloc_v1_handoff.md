# 文件前列定位实验分支：服务器交接

> 最新实施为联合定位v3：见 [joint_file_function_v3.md](joint_file_function_v3.md)。已撤掉末端文件RRF、增加真实函数源码账本；134项相关测试通过、1项跳过，更高预算的新鲜八例配对进行中。用户已授权独立实验分支发布，旧严格门槛保留作对照；下文的“未推送”等描述均为旧迭代历史，当前推送状态以最新v3说明和提交记录为准。

## 当前开发状态：文件与函数联合优化 v2.1

以下 v1 数值和服务器示例保留作历史，不能代表当前联合版本已验收。当前新增 `MYCODE_JOINT_EVIDENCE_V2=1`（默认关闭）：查询聚焦源码窗口、跨轮上下文去重合并，以及分别针对文件/函数的跨轮融合。文件继承机制核验和 grounded 反证约束，函数不继承按文件质量选中的检查点额外权重。没有按任务 ID 或 gold 调整运行排序。

相关回归 129 passed、1 skipped。联合 v2 第一轮四例有 Babel 文件和 marked 函数退化，未通过；v2.1 固定八例、16次正式配对已经全部有效完成，文件 A1 25%→62.5%、函数 A15 25%→37.5%，但文件 A8 75%→62.5%、marked1435文件1→10，仍未通过门槛。未提交、未推送，不启动服务器全量运行。`scripts/fileloc_pair_smoke.py` 当前改版臂启用上述联合开关，原版臂显式关闭；运行目录保存源码副本、哈希和冻结输入。

全案例离线消融显示：去掉最后的文件 RRF、保留 closure 输出，可将 p5.js3709 从11恢复3、marked1435从10恢复1，其余六例不变，函数输出不变。这是后续优先修正方向，尚未实施，不能当成新的在线八例成绩。Calypso21648 的函数覆盖 4/8→1/8、Calypso22026 的文件召回缺失也仍须解决。

完整记录：项目根 `allresultwork/015_KimiV3文件与函数联合优化_实现与验证记录_20261121.md`。四例不能代替原定八例门槛，不将回放成绩当作在线实验结果。

## 以下为 v1 历史记录

基线：`magnet-kimi-compat-v3`，提交`fa574b260fc9fed4518a587912379acd4741d621`。
开发分支：`magnet-kimi-compat-v3-fileloc-v1`。当前为实验实现，未经完整SWE92/Omni296验证；只有小范围门槛通过后才推送。

2026-11-21本地结论：**未通过，不提交/推送，不启用默认策略。** 相关回归121 passed、1 skipped；MiMo2.6pro八例配对File A3为62.50→75.00，但File A15为87.50→75.00，Function A15为37.50→25.00。Babel15349由11→1，不能抵消其余退化和覆盖损失。两个实验开关默认关闭。原始16任务中的一个媒体失败对在独立目录重跑，所有原始记录均保留。

后续优先审目标片段与外层核验交接、种子恢复压低正确文件、以及Chart10806后续轮次命中但检查点回滚后丢失；不要将下方服务器命令理解为本版已获准全量运行。

## 改动

1. `MYCODE_READ_EXPLICIT_PATHS=1`：ReadCode优先读取动作明确提出且当前索引存在的相对路径，保持预算与scope边界。
2. `MYCODE_FILE_FRONT_RERANK=verified_frontier`：复用precision已有责任门槛和质量分，在当前前15内稳定插入强证据候选，不增加候选或LLM评分调用。
3. 读取日志包含请求、拒绝、提交、返回及真正带片段的路径；原有阶段快照继续记录融合、head与closure。

默认关闭改动：`MYCODE_READ_EXPLICIT_PATHS=0`、`MYCODE_FILE_FRONT_RERANK=head_only`。本分支关闭开关可作为配对原版（仅观测日志不同）。不修改Sketch、scope、队列和停止策略。

## 小范围运行

使用已有Python环境（依赖见pyproject.toml/requirements.lock），不自动安装或更新包。凭据文件保留在Git外。

```bash
python -m pytest tests/test_file_frontier_v1.py tests/test_candidate_review_rerank.py tests/test_react_interprocedural_agent.py tests/test_tool_execution_layer.py -q
python scripts/fileloc_pair_smoke.py --workspace /path/to/locCode --env-file /path/to/.env.mimo.local --model-api-name mimo-v2.6-pro --run-dir /path/to/new_smoke_run
python scripts/score_fileloc_pairs.py --workspace /path/to/locCode --run-dir /path/to/new_smoke_run
```

运行器先检查API可用性；模型退休或返回空响应时不继续。原版和改版使用同一模型。输入删除修复patch、test_patch、gold文件和测试标签；评分只在运行结束后加载冻结gold。任务级status=ok不等于API成功，评分器拒绝无模型token的降级结果。记录token不代表完整账单。

## 服务器全量运行（本机不启动）

先将`TASK_ROOT`替换成服务器实际项目根路径，设置`ENV_FILE`为服务器模型配置；执行同一组命令时保持模型、配置和预算一致。两个不同RUN_ID目录分别运行baseline和improved，不复用输出目录。

```bash
export TASK_ROOT=/path/to/locCode
export ENV_FILE=/path/to/.env.kimi.local
export SAMPLES="$TASK_ROOT/clean_subsets_new/swebench_multimodal-full-dev.clean15.samples.jsonl"
export OUTPUT_DIR="$TASK_ROOT/result/fileloc_v1_swe92_improved_RUN_ID"
export MYCODE_READ_EXPLICIT_PATHS=1
export MYCODE_FILE_FRONT_RERANK=verified_frontier
export MYCODE_PHASE_LOG=1
test ! -e "$OUTPUT_DIR" && bash newtest/run_swe_clean15_agent_full.sh --env-file "$ENV_FILE"
```

Omni296必须指定296子集，不能直接使用默认458条：

```bash
export SAMPLES="$TASK_ROOT/collected_results/omin296/data/samples296.jsonl"
export OUTPUT_DIR="$TASK_ROOT/result/fileloc_v1_omni296_improved_RUN_ID"
test ! -e "$OUTPUT_DIR" && bash newtest/run_omni_clean15_agent_full.sh --env-file "$ENV_FILE"
```

原版对照将两个开关改为`0`和`head_only`，使用新的baseline_RUN_ID输出路径。不要把MiMo结果和Kimi结果直接解释为版本效应。完整新运行仍须用冻结gold重新评分，原生指标不可直接拼接旧评分表。

## 验收

8例开发诊断：至少一负例排名提升、A1或A3净增、三个保护例保持A1、A1–8/A15以及函数指标不下降；门槛未通过不推送为已验证分支。全量服务器结果是后续必要验证，不以案例成绩宣称总体优于baseline。

根目录主文档：`allresultwork/014_KimiV3文件前列定位_诊断改进与服务器交接_20261121.md`，原始运行目录及结果入口在其中维护。
