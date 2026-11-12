# MAGNET Kimi 兼容层 v1：实施与验证

## 1. 分支选择

- 新分支：`magnet-kimi-compat-v1`
- 基础分支：`addtest3`
- 基础提交：`9c2c602 Fix Kimi structured response parsing`
- 独立工作树：`/data2/like/locmy-kimi-compat`

选择 `addtest3` 是因为 Omni296-Kimi 的定位实验和 Kimi 结构化响应修复均来自这条历史。`rq4-eval-recovery` 面向下游补丁生成与官方评测恢复，`paper-rq4-analysis` 面向论文分析；从这两条分支开发会把无关的 RQ4 变化混入定位兼容实验。

## 2. 问题判断

现有结果不支持“Kimi 无法完成 MAGNET 定位”这一结论。Kimi 在 SWE92 上与 Qwen 接近，在 Omni296 上也能广泛召回相关文件。主要差距出现在召回之后：

1. 候选压缩不稳定，相关文件容易同时保留。
2. 对函数职责、因果机制和真正修改所有权的区分不足。
3. 缺少排除其他候选的反证，过早停止或停止依据不充分。
4. 搜索轮次和上下文继续增长，但新增证据有限，token 成本很高。

因此 v1 不修改数据集、gold、Top15、检索工具或评价定义，只为不同模型提供相同任务下的协议适配。

## 3. v1 改动

### 3.1 集中式模型协议

`src/mycode/utils/model_profile.py` 定义 `default`、`qwen`、`mimo`、`kimi` 四种协议。显式的 `MYCODE_MODEL_PROFILE` 优先；没有显式设置时，根据 API 模型名推断。未知的显式协议直接报错，避免静默使用错误配置。

Kimi v1 默认值：

| 参数 | 值 | 目的 |
| --- | ---: | --- |
| 候选复核数 | 4 | 降低同时比较过多候选造成的职责混淆 |
| 复核输出预算 | 1400 | 给反证和因果链留下空间 |
| 最终修改集上限 | 3 | 强制形成紧凑修改闭包 |
| 闭包轮数 | 2 | 抑制低收益搜索延长 |
| 每轮扩展数 | 1 | 只按缺失义务逐个补证据 |
| 证据平台期 | 1 | 新证据停滞时更早进入复核 |
| issue 文本上限 | 2400 | 减少长问题描述对判断的稀释 |

所有默认值都可以用原有环境变量显式覆盖。

### 3.2 候选反证

Kimi 必须完整复核送入 judge 的每个候选。未选候选必须给出具体反证，例如：只有调用关系但不拥有错误逻辑、只是导航入口、缺少直接数据流，或属于生成产物。缺少候选或缺少反证时：

- `review_coverage` 和 `counterevidence_complete` 会记录到轨迹；
- `stop_ready=false`；
- 搜索不会把不完整的复核误判为可以停止。

### 3.3 已验证机制闭包

Kimi 只有在最终修改集中至少存在一个“已读、证据落地、机制已验证”的候选时，闭包才能标为 `complete`。仅靠关键词、图邻接或形式上的 flow 覆盖不足以完成闭包。缺失时记录 `verified_root_patch_mechanism`，后续扩展仍可通过找到一个合格候选满足该义务。

### 3.4 可追溯配置

Omni/SWE 启动脚本会在加载模型环境后解析协议，并显示协议名但不显示 API key。结果摘要记录协议名及解析后的候选数、修改集大小和闭包轮数，便于后续确认一条结果究竟使用了什么配置。

## 4. 公平性边界

本分支不改变：

- 数据集样本与 gold 标注；
- Top1/5/15 和函数级指标定义；
- 可用检索工具与仓库内容；
- Qwen、MiMo 和 default 的既有默认参数；
- 官方评测的通过标准。

兼容层改变的是模型如何压缩和验证同一批证据，不是降低 Kimi 的评分标准。

## 5. 离线验证

聚焦回归测试：

```text
75 passed
```

覆盖内容包括：协议推断、显式覆盖、非法协议拒绝、启动器配置、候选完整复核、未选候选反证、无机制时拒绝闭包，以及有落地机制时允许正常完成闭包。

Python 编译、Shell 语法、`git diff --check` 和凭据模式扫描均通过。此前完整测试运行结果为 `175 passed, 1 skipped, 19 failed`；19 个失败来自本机缺少 clean15 数据路径、Pillow 和真实 API 凭据，不是本次相关测试回归。

### 5.1 第一条在线困难样本金丝雀

使用新的 Kimi2.6 套餐运行历史困难样本 `tailwindlabs__tailwindcss-10276`。旧轨迹记录约 228,896 token、262.5 秒，File Top1 命中但 Function Top15 未命中，closure 为 incomplete。

v1 首次运行结果：

| 项目 | 旧轨迹 | v1 首次运行 |
| --- | ---: | ---: |
| Token | 228,896 | 23,870 |
| 耗时 | 262.5 秒 | 156.7 秒 |
| LLM 事件 | 未统一记录 | 12 |
| File Acc@1 | 命中 | 命中 |
| File Recall@15 | 未按本表口径记录 | 0.50 |
| Function 首次命中 | Top15 未命中 | Rank 11 |
| Function Recall@15 | 未按本表口径记录 | 0.25 |
| Closure | incomplete | partial |
| Verified mechanism | false | false |

相对旧轨迹，token 减少约 89.6%，耗时减少约 40.3%。正确主文件 `src/lib/setupContextUtils.js` 仍保持 Top1，函数从 Top15 完全未命中改善到 Rank 11 弱命中；但第二个 gold 文件 `src/corePlugins.js` 仍未召回，严格文件和函数指标仍为 0。因此当前证据支持“成本和停止校准明显改善、细粒度定位有有限改善”，不支持“该困难样本已解决”。

该运行还发现 partial fallback 曾把 `CHANGELOG.md` 加入修复集合。后续修复将顶层 README、CHANGELOG、CONTRIBUTING、LICENSE 和常见 Markdown/reStructuredText 文档识别为 `documentation`，并在非文档任务中禁止进入 modification closure。该限制不删除 Top15 评测候选，只清理下游修复上下文；专门回归测试已覆盖此案例。

同一样本在 documentation 修复后复测：

| 项目 | v1 首次运行 | documentation 修复后 |
| --- | ---: | ---: |
| Token | 23,870 | 23,548 |
| 耗时 | 156.7 秒 | 157.0 秒 |
| LLM 事件 | 12 | 11 |
| File Acc@1 | 1.00 | 1.00 |
| File Recall@15 | 0.50 | 1.00 |
| File Strict Acc@15 | 0.00 | 1.00 |
| Function Acc@15 | 1.00 | 1.00 |
| Function Recall@15 | 0.25 | 0.25 |
| Function MRR | 0.0909 | 0.0714 |
| Closure 文件 | `CHANGELOG.md`, `setupContextUtils.js` | `offsets.js`, `setupContextUtils.js` |

复测中两个 gold 文件 `src/lib/setupContextUtils.js` 和 `src/corePlugins.js` 分别位于 Rank 1 和 Rank 3，说明文件完整召回可以在相同低 token 预算下实现。`CHANGELOG.md` 已退出 closure，验证了过滤修复；但 `offsets.js` 仍不是 gold，说明下一层问题是相关源码之间的职责选择，而不再只是文档噪声。两次函数弱命中的具体排名存在波动，因此不能用单次最优结果代表总体水平。

### 5.2 第二条形式闭环假阳性金丝雀

`python__mypy-15407` 的旧轨迹把 closure 标为 complete，但 Function Top15 未命中。v1 运行结果如下：

| 项目 | v1 |
| --- | ---: |
| Token | 35,318 |
| 耗时 | 485.2 秒 |
| LLM 事件 | 10 |
| File Acc@1 | 0.00 |
| File Recall@15 | 0.50 |
| Function Acc@15 | 1.00 |
| Function Recall@15 | 0.3333 |
| Function MRR | 0.1111 |
| Closure | partial |
| Profile guard | failed as intended |
| Missing obligations | `root_patch_mechanism`, `verified_root_patch_mechanism` |

新 guard 没有重演旧轨迹的 false complete，并把缺失机制显式保留下来。函数层由旧记录的 Top15 未命中改善为弱命中，但两个 gold 文件仍只召回一个，Top1 也不是 gold，因此该样本同样不能视为定位成功。约 8 分钟耗时主要来自 mypy 大仓库上的本地 statement flow 和 static slice，而不是 API 重试或限流。

## 6. 在线验证计划

### 阶段 A：12 条独立金丝雀

从 Omni458 中选择不属于冻结 Omni296 的 12 条样本，覆盖不同语言、仓库规模和问题类型。使用相同 Kimi API，固定随机性和预算，对 `addtest3` 与 `magnet-kimi-compat-v1` 做配对比较。

主要指标：

- File Acc@1/5/15；
- Function Acc@1/5/15；
- 完整候选复核率；
- 反证完整率；
- 已验证机制闭包率；
- 平均搜索轮数、LLM 事件数、输入/输出 token；
- 单样本总时长和失败类型。

通过条件建议为：File Acc@1 和 Function Acc@1 不下降，函数级 Top5 或 Top15 有提升，同时平均 token 或 LLM 事件数明显下降。12 条只用于判断协议是否工作，不用于论文主结果。

### 阶段 B：失败案例审查

逐条检查以下四类轨迹：

1. 原版错、v1 对：确认提升来自职责压缩和机制证据。
2. 原版对、v1 错：检查候选上限或早停是否过强。
3. 两版都错：区分召回缺失与 judge 选择失败。
4. 两版都对：比较成本是否下降。

### 阶段 C：完整重跑

只有阶段 A 没有明显回归后，才新建独立输出目录完整重跑。不得覆盖原 Omni296-Kimi 结果。最终报告采用配对样本比较，并单独报告 API、解析、召回和选择失败数量。

## 7. 当前结论

v1 已完成离线实现、回归验证、Tailwind 一条重复运行和 mypy 一条在线困难金丝雀。当前证据支持三点：第一，候选/轮次压缩可以显著降低 Tailwind 案例的 token；第二，verified mechanism guard 能阻止旧式 false complete；第三，两类旧 Function Top15 失败都变成了较低排名弱命中。与此同时，多文件完整召回、函数 Top1/5 和 partial fallback 的真实职责选择仍不稳定。两条困难样本不足以推断 Omni296 总体指标，下一项有证据价值的工作仍是完成分层的 12 条独立金丝雀，而不是覆盖旧结果或直接全量重跑。
