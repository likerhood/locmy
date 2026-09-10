# addtest3: File 前排定位与覆盖率修复

## 分支与实验边界

- 基于 addtest2 的 `29dc302` 创建独立 addtest3 worktree；不修改原实验目录。
- 分析使用服务器 addtest2 Qwen 的 92 条 compact agent traces。该实验记录的运行版本为 `8ae8054`，不是本次基线分支头；分支头还包含后续修复。
- 本文中的回放结果不是 addtest3 在线成绩，不验证新的模型输出、浏览器访问或源码下载行为。
- gold 仅用于离线计分，不进入新增定位规则、提示词或种子选择。

## 失败诊断

主要问题并非单纯缺少搜索轮数。原最终 File Acc@1 为 28.26%，但 FastSeed 首位命中 45/92（48.91%）；26 个正确初始首位在后续丢失，同时动态流程救回了 7 个错误首位。

| 案例 | 观察到的失败 | 本次处理及边界 |
|---|---|---|
| marked-2627 | rules/Tokenizer/Lexer 初始候选有效，最终 bundle 抢占首位 | 有限保留源码种子；根据生成声明和对应源码识别顶层 lib 产物 |
| marked-1825 | Tokenizer 初始首位被 lib/marked.js 替代 | 不把 lib 一律视为源码，也不整体禁止真实 lib 实现 |
| p5.js-5555 | RendererGL 初始首位有效，最佳轮却选择 setting.js | 种子在最佳轮之后重新参与排序，不让单轮选择永久消除入口先验 |
| Chart.js-10806 | 正确 arc 文件仍在前排附近，legend 获得较强保护 | 去掉默认固定前六保护；仍保留现有证据锁和 HeadSelector。错误机制判断不保证被本次完全解决 |
| wp-calypso-26335 | Accordion 种子本身错误，actions 靠后且 reducer 缺失 | 种子策略不能解决缺失根机制；保留跨文件检索和闭包，需新实验继续审计 |

已有结构数据并非这次差距的主要已证实原因：两次实验多数实体规模一致，addtest2 有 89 个样本获得 checkout。三个下载失败样本需要可靠降级，但不能解释大范围首位退化。

## 实现

1. **结构数据降级**：已有 canonical structure 时，checkout 下载失败不再一起丢弃结构路径和 base_commit。显式记录 `source_error`，不伪装成本地 checkout 可用。没有结构也没有源码的失败仍按原错误路径处理。
2. **有限种子保留**：默认最多两个 FastSeed 源码候选重新进入最终候选前缀。要求索引中有源码，拒绝不合适路径角色、已识别产物和有 grounded 反证的候选。缺少 flow 本身不等于反证。后续跨轮强候选和 HeadSelector 仍可纠正它们，种子不是机制证明。
3. **生成物识别**：顶层 `lib/*.js/cjs/mjs` 同时具有生成声明和对应 `src/*.js` 时，最终稳定降到其他候选之后，不删除候选。明确要求修改构建产物时跳过此降级。`client/lib` 或没有生成证据的普通 lib 不被一刀切。
4. **责任验证**：英文提示词明确源码是修复前版本，应识别错误操作或缺少检查的插入点，而不是要求修复已经存在。compact review 中合法的已提供实体 ID 不再因截断片段缺少声明而失效；未知 ID、quote/flow 的原有校验继续保留。
5. **预算**：wrapper 默认最大动态轮数从 14 改为 12，review plateau override 默认轮次从 12 改为 6；这不是第六轮无条件停止，原 plateau 条件仍需满足。外部显式环境值继续覆盖默认值。call/dataflow、图搜索与修改闭包没有删除。
6. **诊断**：增加 seed_retention 阶段与保留/拒绝原因；阶段评估同时计算 File Acc@1–8、Acc@15、MRR、SL 和 Recall。

新增默认值：`MYCODE_FINAL_SEED_PREFIX=2`；`MYCODE_CROSS_ROUND_PROTECTED_PREFIX=0`。原有证据锁仍有效。保留种子可能挤出 Top-15 尾部，因此深层覆盖率必须实测，而不是认为保留候选池就保证指标不降。

## 冻结排序回放

逐行读取 `agent_traces.jsonl`，比较“原最终顺序”与“无条件前置两个原始种子再去重截取 15”。回放不执行新代码的源码角色/反证筛选，也不执行之后的 HeadSelector，因此只是排序损失诊断。

| 回放策略 | Acc@1 | Acc@3 | Acc@4 | Acc@6 | Acc@8 | Acc@15 | MRR | SL@15 | Recall@15 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| addtest2 冻结最终排序 | 28.26 | 56.52 | 60.87 | 67.39 | 71.74 | 84.78 | 44.17 | 42.39 | 60.21 |
| 前置两个原始种子 | 48.91 | 69.57 | 76.09 | 78.26 | 79.35 | 84.78 | 60.33 | 44.57 | 61.78 |

这里说明存在可恢复的排序信息，不说明新版本必然获得这些提升。SL@15 改善只有两个样本，表明多文件找全仍是独立问题。Function/Module 不可由文件回放推导。

复现命令（只生成离线报告，不调用 API）：

```bash
python scripts/replay_seed_prefix.py /path/to/addtest2/agent_traces.jsonl \
  --output result/addtest3_offline/qwen_seed_prefix.json
```

## 新实验

在独立 addtest3 目录、该目录对应的已安装虚拟环境中执行，模型配置必须是有效且明确指定的本地文件：

```bash
RUN_ID=swe_clean15_addtest3_v1 RESUME=0 \
  bash scripts/run_swe_clean15_addtest.sh --env-file /absolute/path/to/.env.local
```

不要在仍运行旧实验的目录中切换分支，也不要复用旧 RUN_ID。不预先 source 旧分支 runtime profile，以免覆盖新默认值；启动日志核对实际模型、动态轮数和源码能力。

新结果出来后，先对齐 92 个 instance ID 和成功数，再检查 Acc@1–8、SL/Recall@15、Function/Module@15、平均耗时与 token。未完成全量新实验前，不宣称满足性能验收阈值。冻结此次规则后评估，避免按单个 gold 案例继续调参。

## 本地验证

- 排除三个真实 API 测试文件后：169 passed, 1 skipped。随后补充种子边界测试，针对本次涉及模块再运行：59 passed（两批测试存在重叠，不应相加）。
- 完整测试首次尝试中，四个真实 API 用例因未配置模型凭据失败；未通过加载密钥启动付费调用。另一个新增用例的无源码哨兵路径断言已修正并通过上述回归。
- wrapper 通过 `bash -n`，补丁通过 `git diff --check`。
- 新在线 92 样本、耗时和 Function/Module 回归尚未验证。
