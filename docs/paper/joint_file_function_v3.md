# 文件与函数联合定位 v3：实现、验证与运行说明

## 1. 版本与授权边界

基线为 `magnet-kimi-compat-v3` 的 `fa574b260fc9fed4518a587912379acd4741d621`。开发与发布分支为 `magnet-kimi-compat-v3-fileloc-v1`；分支名字沿用，当前算法版本为联合定位 v3。

用户已明确调整发布要求：不限制步数/token成本；文件或函数有改善即可提交实验分支，不再把“两类全部截点同时非退化”作为阻止提交的条件；所有退化仍必须披露。仅推独立分支，不合并主分支、不强推、不自动启动服务器全量任务。

本次先发布通过代码回归的**实验实现检查点**，新模型测试继续运行，结束后追加结果提交。不能把“代码已推送”解释为“新模型八例已经验证完成”。旧严格 gate 原样保留，新实验发布判断单列，不改写旧实验结论。

## 2. 为什么文件改善不必然带来函数改善

文件与函数不是同一个排序：文件有 precision、head、closure；函数经历实体检索、源码/流核验和跨轮合并。正确文件前移只能减少找错文件的风险，不能保证其中正确函数被生成、被读取、被核验或最终保留。大文件中的无关函数、嵌套同名函数和多文件修改责任仍需要单独解决。

已有轨迹例子：Calypso21648 文件 2→1，但函数覆盖 4/8→1/8；第二轮曾发现另外两个正确函数，最后因“只出现一轮、缺少实现窗口”没有保留。这不是文件排名能自动修复的交接问题。

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

## 4. 已完成与进行中的验证必须区分

相关单元和回归：134 passed、1 skipped，`git diff --check` 通过。新增测试覆盖真实源码读取、独立证据门槛、错误行范围、预算耗尽、单轮发现尾部保留、最终文件顺序保持，以及新发布口径披露另一粒度退化。

固定旧轨迹回放只用于确定性末端策略检查，不能冒充更高预算的新模型成绩。旧 v2.1 八例有文件 A1 提升但 A8/保护例退化，仍保留在项目根 `allresultwork/015_KimiV3文件与函数联合优化_实现与验证记录_20261121.md`。

新鲜 MiMo-v2.6-pro 固定八例原版/改版配对正在独立目录运行，4轮、每轮最多12个 ReAct步骤、Top15、每任务900秒异常保护、函数复读上限48。两臂相同最大搜索预算，不能与此前3轮/8步结果直接作无控制的算法增益比较。

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

模型小范围完成后将逐例首个正确名次、A1/A3/A5/A8/A15、SL15/REC15、阶段变化、账本接受/拒绝理由整理回项目根报告和本文件。代码可实验发布不等于两类指标必然同时改善，更不等于全量优于 CoSIL/LocAgent。

服务器可按相同模型、相同预算、原版与改版独立目录运行 SWE92 和 Omni296；不要使用默认 Omni458 替代296。此时仅提供命令，由服务器人员启动，不从本机启动全量任务。对于 Calypso22026 全轮候选均未命中的情况，预算增加只能提供机会，不能代替必要的召回来源诊断。
