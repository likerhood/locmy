# MAGNET 消融实验设计与实施规划

> 实施更新：核心开关与 Qwen 统一启动器现已实现，实际运行方式和精确干预边界见 [Qwen 消融运行说明](../ADDTEST32_QWEN_ABLATION_GUIDE.md)。下文保留初始研究设计；其中全流程固定策略、严格候选内 flow、配对统计汇总仍是后续目标，不代表本版已实现。no_graph 当前还会移除闭包使用的关系图邻接；fixed_react 只固定 ReAct 局部策略。尚未运行在线效果实验。

## 1. 基线与本次工作边界

- 新分支：`addtest32`，从 `addtest3` 的提交 `9cef95a2282ef74cffcd8f3e5b379db6460a3aa6` 创建。
- 独立工作目录：`/home/like/locCode/alltry/mycode-addtest32`。不切换、修改其他 worktree，不复制旧结果作为新实验。
- 本次交付是代码审计与实验规划；尚未实现下文新增的统一消融接口，尚未运行付费模型实验，不能声称已经验证模块收益。
- 不混入 addtest4/addtest31 的修改。先回答 addtest3 为什么有效，再研究新版本优化。
- Full 必须在消融实现后的同一提交重新运行；历史 addtest3 结果仅用于复现核对，不能直接替代配对对照。

## 2. 要回答的研究问题

| 问题 | 需要验证的机制 | 主要观察 |
| --- | --- | --- |
| 多模态证据有用吗？ | 图像提供文本没有的符号、现象和界面信息 | File Acc@3/8、图像增量线索、误导率 |
| 图导航有用吗？ | 从入口找到实现、调用者、被调用者和相关子系统 | 候选召回、首次读到正确源码的轮次、Acc@15 |
| flow 有用吗？ | 从“相关代码”辨别“承担故障机制的代码” | File MRR/Acc@1/3、Function REC、错误提升次数 |
| agent 的动态性有用吗？ | 利用新证据调整查询、工具和停止时机 | 同预算质量、实际调用量和耗时 |
| 修改闭包有用吗？ | 在根文件之外补全必要的修改位置 | 固定 K 的 SL/REC、最终 patch set 的完整性与大小 |

不能把多轮搜索、代码图、数据流、最终排序的收益全部归给“agent”。也不能把关联性日志直接写成因果结论。

## 3. 代码核对：现有开关并不等于完整消融

代码定位均相对本 worktree：

| 代码入口 | 已核实的行为 | 实验影响 |
| --- | --- | --- |
| `src/mycode/agent/pipeline.py`：`run_localization_pipeline` | 先准备仓库，再构造证据和索引 | 所有组必须保持源码 checkout 与索引一致 |
| `src/mycode/dynamic_retrieval/search_agent.py`：`_run_flow_backends_layered` | 基础 program flow、parameter closure 总会执行；deep 门控另外控制 statement/slice/interprocedural 等 | `FLOW_MODE=light` 不是 no-flow |
| `src/mycode/dynamic_retrieval/tools.py`：`TraceFlowTool.run` | ReAct 工具还有独立的 flow 入口；light 模式仍可能执行 statement flow | 只关主循环 deep flow 会漏掉另一条通路 |
| `src/mycode/dynamic_retrieval/search_agent.py`：图扩展及 scope 设置 | 多类查询执行图扩展；`MYCODE_DEEP_GRAPH_SCOPE=0` 表示全仓库建图 | 禁止用 scope=0 表示关闭图，且它可能增加内存 |
| `src/mycode/dynamic_retrieval/react_agent.py` | SearchAnchor、ReadCode、NavigateCode、TraceFlow 动态调度 | 关 LLM 不等于只关动态调度，会同时改变其他能力 |
| `search_agent.py`：最终排序段 | seed 保留、跨轮融合、HeadSelector、闭包以及最终实体处理依次执行 | 关闭某一排序阶段不意味着其后没有再次重排 |
| `search_agent.py`：`_build_modification_closure` | closure 轮数参数最小为 1 | 设 `MYCODE_CLOSURE_MAX_ROUNDS=0` 不能关闭闭包 |
| `scripts/run_swe_clean15_addtest.sh` | 默认 12 个 dynamic rounds、6 个 tool rounds、10 个 ReAct steps、Top-K=15 | 不沿用历史命令中的 14 轮作为“相同默认预算” |

已有局部开关：`MYCODE_PRECISION_RERANK=0`、`MYCODE_BEST_ROUND_CHECKPOINT=0`、`MYCODE_CROSS_ROUND_FRONTIER=0`。
它们适合对应阶段的消融，不能分别解释成“无排序”“无记忆”“无 agent”。
`USE_VLM=0` 也不能自动保证没有 OCR、图片缓存或截图衍生线索，需要沿证据入口清除。

## 4. 主消融：先做六组

| ID | 组名 | 唯一主要干预 | 保留什么 | 不允许的替代操作 |
| --- | --- | --- | --- | --- |
| A0 | Full | 无 | addtest3 全部方法 | 用旧运行结果顶替 |
| A1 | w/o Visual Evidence | 不读取图像内容，不产生或消费 VLM/OCR/截图视觉证据 | issue 文本、链接锚文本、URL 文本访问、源码 | 直接关网络，连仓库获取一起取消 |
| A2 | w/o Graph Navigation | 不用关系图扩展候选、产生导航查询或为排名加分 | 文本/实体搜索、源码读取、候选内 flow 验证 | 关闭源码索引，或把 scope 设为 0 |
| A3 | w/o Flow Evidence | 关闭各 flow 后端及其派生查询、分数、证明与缓存消费 | 图导航、源码读取、quote/entity 验证 | 仅把 deep 改成 light |
| A4 | Fixed Search Policy | 固定工具次序与查询更新规则，不按证据自适应选下一动作 | 同一个模型、证据、工具、reviewer、预算上限 | 将 USE_LLM 设为 0 或 dynamic rounds 设为 1 |
| A5 | w/o Modification Closure | 不扩展修改闭包、不执行闭包重排，不用闭包扩展最终实体 | 闭包前排名、相同的输出粒度 | 只禁用 closure rerank，却继续消费 closure 证据 |

先把 A0/A2/A3/A5 实现和验证，再加入 A1/A4。六组是实验设计，不是目前已经可直接运行的六条命令。

### 4.1 如何解释 no-flow 的依赖问题

Full 的机制验证、证据锁定和闭包可能依赖 direct flow。删除 flow 后这些能力减少，是整个 flow 证据通路的总效应，不是单个后端的独立收益。

主组 A3 不伪造 `mechanism_verified=true`，也不把缺少被禁用的 flow 标成候选“已被反证”。必须记录 `unavailable_by_ablation` 与真正反证的区别，并禁止 agent 持续请求一个不可用工具。

如 A3 大幅下降，再做诊断组：保留 flow 候选发现，但禁止 flow 分数和验证信号进入排序。它测试“flow 参与排序的贡献”，不是完整 no-flow；不能取效果更大的那个冒充主消融。

### 4.2 如何解释 no-graph

图既可能用于搜索导航，又可能是跨过程 flow 的底层结构。主组 A2 只关闭导航作用，允许 flow 在已有候选内使用必要的局部结构，但该结构不得额外扩张候选。

因此论文应称 **w/o Graph Navigation**，不能称“完全没有图”。若无法隔离底层共享结构，先修接口，再跑该组；不要让名称掩盖实际仍在用的能力。

## 5. 专门验证图与 flow 是否互补

在以上边界实现并通过审计后，增加一组 A6：同时关闭图导航和 flow 证据。

| 组合 | 图导航 G | flow 证据 F | 对应 |
| --- | --- | --- | --- |
| G1F1 | 开 | 开 | A0 |
| G0F1 | 关 | 开，仅验证现有候选 | A2 |
| G1F0 | 开 | 关 | A3 |
| G0F0 | 关 | 关 | A6 |

对越大越好的指标 M，计算交互项：`I = M(G1F1) - M(G1F0) - M(G0F1) + M(G0F0)`。
I 为正只能说明该指标尺度上存在正交互迹象，还需要配对置信区间；接近零可能表示收益可加，不能一概说不互补。
这研究的是“导航与验证的配合”，不是证明项目实现了完整、精确的静态数据流分析。

通俗例子：issue 说按钮提交了旧值。搜索先找到 Button，图导航找到 handler/store，flow 再检查 handler 捕获的参数和 store 写入值。图可能帮助找到 store，flow 可能将真正写错值的文件排到按钮之前。若两者都没有，则只能依赖文本与源码阅读。
这个例子用于说明机制，不是已经观测到的实验结果。

## 6. 第二层诊断：不要全部塞进主表

| 诊断 | 对照定义 | 用途 |
| --- | --- | --- |
| HeadSelector | 开 vs `MYCODE_PRECISION_RERANK=0` | 前排提升是否来自选头；同时观察后续闭包覆盖其决定 |
| Best checkpoint | 开 vs `MYCODE_BEST_ROUND_CHECKPOINT=0` | 最佳轮选择与末轮输出的区别 |
| Cross-round fusion | 开 vs `MYCODE_CROSS_ROUND_FRONTIER=0` | 融合收益；明确这不是清空全部跨轮记忆 |
| Seed retention | 保持 seed 发现，仅关最终 seed 前缀保护 | 区分“种子找得好”与“保护种子是否合理” |
| Deep flow | auto vs light，并记录两个 flow 入口的实际后端 | 深分析是否值得成本，不能标成 no-flow |
| External URL | 保留 issue 中链接与仓库获取，只关 URL 外部内容提取 | 外部上下文是否带来增益或污染 |
| Rounds | 上限 2/4/8/12，其他设置不变 | 收益是否饱和；轮数不是调用数 |
| Early stop | 同为 12 轮上限，开/关自适应早停 | 早停的独立成本收益，需要覆盖全部停止入口 |

已有 `rank_stage_snapshots` 可先做无 API 成本的阶段分析，但只能描述已有轨迹中某次重排的净变化。删除组件会改变后续查询和读取，所以阶段回放不能替代端到端消融。
固定策略 A4 和关闭早停也是两种不同实验：前者改变怎么搜，后者改变什么时候停。

## 7. 先冻结数据与运行条件

1. SWE 使用当前 Clean15 92 个 ID；Omni 使用当前 v458 的 458 个 ID。保存样本文件 SHA256、ID 清单和逐样本 gold/commit 指纹。
2. 历史盘点发现，部分 Qwen/Kimi Omni baseline 虽然也是 458 条，但与当前 458 只有 283 个共同 ID。必须重新核对清单；不能以数量相同认定同一测试集。历史 baseline 比较需要重新对齐输入和 gold，消融则在固定当前集合内配对。
3. 源码 base commit、可读文件数、实体覆盖率、结构索引版本一致；不能一个组用真实 checkout，另一个组仅有结构占位数据。
4. 同一模型 API 名、endpoint、thinking、temperature、输出预算和 retry 策略；仅同一个展示名称不够。清单不保存密钥。
5. 优先 Qwen 做完整 SWE 主消融，MiMo 做跨模型验证；Omni 先分仓库诊断，冻结实现后再全量。Kimi 可后续补充，不因结果不利而隐去。
6. 每组独立 RUN_ID、结果目录及模型/工具证据缓存，初跑 `RESUME=0`。只共享只读源码 checkout 和经过版本校验的原始索引；禁止共用会写入候选/证据的缓存。
7. 不要求共享现有 worktree 的可编辑安装。新环境或显式验证 import 路径，保证加载 `mycode-addtest32/src`，不是旧分支代码。
8. 初始化失败、超时、模型错误必须记录；主指标按冻结的全部样本计算，不能只报告成功子集。无适用函数/module gold 时另列有效分母与判定规则。

## 8. 预算公平性与执行规模

主分析使用相同预算上限：12 dynamic rounds、6 tool rounds、10 ReAct steps、Top-K=15，源码读取与上下文截断上限也固定；实际预算以导出的 resolved config 为准。
移除组件节省下来的预算不自动补给其他组件，这衡量部署中的整体影响。若 Full 更准但更贵，增加等 token 上限或成本-准确率曲线，不能只凭相同轮数声称等成本。

按以下顺序执行，单进程串行：

1. 无 API 单元测试与接口审计，证明禁用能力不会从其他路径或缓存泄漏。
2. 小规模冒烟：SWE 8 条、Omni 12 条，仅验证流程、字段和错误处理，不做论文结论。
3. 诊断：SWE 24 条、Omni 40 条。按仓库/语言/输入模态分层，用固定种子抽样；必须包含原本表现好的仓库，不能只选已知失败案例。
4. 冻结代码和参数，跑 SWE 92 × 7 组 = 644 个样本运行/模型（包含组合 A6），不是 644 次 LLM 调用。
5. Omni 若跑全 7 组则为 458 × 7 = 3206 个样本运行/模型。先估算成本；预算有限可先验证 A0/A2/A3/A6，但不可宣称所有消融都在 Omni 得到验证。
6. 对主结论尽可能做 3 次配对重复；模型不可控随机性需承认。单次运行只能给样本不确定性，不能涵盖模型重复运行波动。

交替组别执行并保留时间戳，减少服务时段漂移；不要 A0 跑完数天后才跑所有对照。
已用于迭代的样本不能重新宣称完全未见测试集。论文披露开发使用情况，额外留出的仓库/样本用于确认泛化。

## 9. 指标与统计口径

- **Acc@K**：前 K 至少命中一个 gold；主要前排看 File Acc@1/3/8，同时报告 Acc@15。它不表示全部修改位置都找齐。
- **MRR@15**：第一个命中位置的倒数，15 名内没有命中记 0；适合观察整体前排质量。
- **REC@K**：前 K 覆盖的 gold 数占该样本 gold 总数比例，再按样本汇总。
- **SL@K**：该样本所有 gold 都包含在前 K 才算成功；多文件任务比 Acc 更严格。gold 数超过 K 时理论上不能 SL@K 命中，需报告这类样本数。
- **Function/Module**：使用冻结的实体归一化、适用性和 gold 定义，报告各自分母。
- **Patch set**：单独评估最终建议修改集合的 SL/REC/PRE、集合大小与空集率，不能把当前 ranked-list 的 Set@All 当成 patch-set 指标。
- **效率**：LLM 调用、输入/输出 token、工具调用、读取字符、图展开边数、flow 后端/有效证据数、轮数、耗时和峰值 RSS；成功样本与包含失败的总成本都报告。

主检验预先指定 File MRR@15；Acc@1/3/8 和 SL/REC 为辅助解释，避免事后挑最有利指标。
对每个样本保存 Full-minus-ablation 差值，采用配对 bootstrap 给差值置信区间；二值 Acc/SL 可补 McNemar 检验。多组主比较采用 Holm 校正，报告效果量而不是只报显著性。
仓库内样本有关联，补充分仓库宏平均与留一仓库敏感性分析；SWE 仓库数量少，不把普通样本 bootstrap 解释为跨仓库泛化保证。

## 10. 失败案例怎么分析才有价值

对每个 gold，只在运行结束后离线追踪：

`是否进入召回池 -> 是否通过图扩展出现 -> 是否实际读取 -> 是否有受支持实体/quote -> 是否有任务相关 flow -> 是否被排到前 8 -> 是否被末端重排压下去`。

| 现象 | 优先解释 | 下一步 |
| --- | --- | --- |
| gold 从未进入候选 | 导航/召回缺口 | 查 package 定位、索引覆盖、搜索词，不先加精排强度 |
| gold 进入但未读源码 | 读取分配不足 | 检查读取预算与选择顺序 |
| gold 已读且有证据但排后 | 分数融合或保护错误 | 查 head/checkpoint/seed/closure 阶段变化 |
| flow 很多但重复、泛化 | 证据质量不足 | 按端点和源码签名去重，不能用数量表示进展 |
| 根文件命中但 SL 低 | 责任链不完整或 gold 粒度问题 | 看必要支撑文件及实体传播，不盲目扩大集合 |

每组选择至少一个 Full 赢、一个 Full 输、一个双方失败的配对案例，列出相同 sample ID、源码片段、工具动作与排名变化。
历史 Chart.js、marked、p5.js 以及 Babel/mypy/webpack 案例只用于提出假设；不能据此给特定仓库或 gold 文件硬编码优先级。

## 11. addtest32 后续实现清单

下列接口是待实现方案，当前不要把这些名称当作已有可运行参数：

1. 增加统一 `AblationConfig`，明确 visual、graph_navigation、flow_evidence、adaptive_policy、modification_closure 的启停；默认 Full 完全保持 addtest3 行为。
2. 在主循环、ReAct 工具、证据入口、reviewer 输入、候选融合和最终实体输出统一消费配置，禁止仅修改提示词。
3. 可用工具列表与英语提示词同步变化；禁用工具不进入候选动作，不靠反复失败消耗预算。
4. 结果保存 `ablation_id`、resolved config、源码/data/gold 指纹、能力禁用原因、实际后端计数、预算与缓存命名空间。
5. no-flow 同时处理 flow obligation 的调度需求；不得无限等待无法完成的 flow，也不得将禁用当作“任务已验证”。
6. no-closure 明确定义 patch set 输出规则；排名输出与 Full 相同长度限制，另行评价自适应集合，避免集合数量差异造成假提升。
7. 先实现配置单测与 spy 测试：禁用图导航时无导航扩展，禁用 flow 时各入口调用数均为 0，禁用视觉时无图片证据消费，禁用闭包时无闭包扩展/重排。
8. 加默认 Full 的确定性回归 fixture、缓存隔离测试、入口脚本覆盖测试；Full 与原实现的相同 fixture 输出应一致。
9. 再实现矩阵启动器：遇到缺模型、缺样本、源码不可读或配置未生效立即失败，不允许静默离线运行。
10. 新增独立评估器，流式读取逐样本结果，只生成小型 paired CSV 和汇总表，不一次读入大型轨迹文件。

## 12. 内存与运行安全

- 并发默认 1，不并行建图，不用整个仓库无界 scope。
- 不复制大型历史结果到新 worktree，不扫描或输出 `.env` 密钥。
- 分析只优先读取 summary JSON、逐样本 CSV、小型 phase 记录；需要轨迹时逐行读取并按 ID 筛选，处理完一个样本立即释放。
- 正式启动前检查空闲内存和磁盘。分组进程间释放缓存；记录峰值 RSS，设置可控进程内存上限时要将触发记为运行失败，而不是丢掉样本。
- 不以增加 swap 或同时跑多组来掩盖内存问题；本次仅文档和小规模代码读取，没有启动实验。

## 13. 给老师/论文的简洁讲法

我们不是只证明“完整系统比删掉一个东西好”，而是区分五种能力：多模态提供线索，图导航扩大相关实现的覆盖，flow 检验责任机制，动态策略决定下一步该读什么，修改闭包补全共同修改位置。

主消融验证各通路的整体贡献，图与 flow 的组合实验验证两者能否配合，预算曲线检查收益是否只是多花了调用成本。结果必须允许出现负收益：若某组件增加成本却无稳定提升，就收缩适用范围或删除，不预设 Full 一定最好，也不承诺消融后超过 baseline。
