# MAGNET Kimi 兼容层 v2：编辑责任归属修复

## 1. 分支与边界

- 分支：`magnet-kimi-compat-v2`
- 基础：`magnet-kimi-compat-v1` / `a5b5d32`
- 工作树：`/data2/like/locmy-kimi-compat`

本分支只修改 MAGNET 的候选裁决、修改闭包和函数排序。它不包含
`graphlocator-protocol-guard-v1` 的工具执行保护，也不改变数据集、gold、Top-K、
检索工具或 Qwen/MiMo/default 的既有协议。

## 2. v1 后仍存在的问题

v1 已把 Tailwind 困难样本的 token 从约 228,896 降到约 23.5K，并阻止 mypy
样本的形式闭环假阳性。但轨迹仍显示三个后置问题：

1. Kimi 能召回正确文件，却会把调用者、消费者或相邻实现与真正编辑 owner
   一起保留，导致 closure 中出现 `offsets.js` 之类的相关但错误文件。
2. partial fallback 会无条件保留已有 seed；一个高排名但没有机制证据的旧选择
   可能绕过后续验证。
3. reviewer 已经指出精确函数时，原有加成仍可能小于最高 225 分的语义加成，
   因而正确函数仍停留在 Function Top10 之后。

因此 v2 的目标不是扩大搜索，而是让已获得的证据形成更明确的编辑责任判断。

## 3. 实施内容

### 3.1 唯一主 owner 对比

Kimi profile 开启 `require_owner_contrast`。候选 judge 必须在存在单一 owner 时只保留
一个 verified primary head；调用者、消费者、导航文件和相关实现只能标为 plausible、
navigation 或 reject。互补的多文件改动可以保留为 supporting target，但不能成为竞争主头。

验证结果新增：

- `verified_primary_heads`
- `ambiguous_verified_competitors`
- `owner_contrast_complete`

Kimi 只有在 owner 对比完成后才能 `stop_ready=true`。零个或多个 verified primary
head 都会继续搜索或复核。

在线 mypy 金丝雀进一步发现，旧校验允许 snippet 和 entity 只要来自同一文件就组合成
verified mechanism，即使二者相隔数千行。Kimi profile 因此增加
`require_entity_snippet_alignment`：snippet 必须包含实体名，或与实体源码行范围重叠。
不同代码区域的证据不能再拼接成 verified owner。

复跑又发现 class/module 的源码范围过宽，类内任意方法都可能与 class entity 行范围重叠。
最终规则仅允许 function/method 使用行范围重叠；class/module 必须在 snippet 中直接出现名称，
避免宽泛容器实体冒充精确编辑位置。

### 3.2 严格 partial fallback

Kimi profile 开启 `strict_partial_owner_evidence`。旧 seed 不再因为“之前被选中过”而自动
进入下游修复上下文。候选至少需要满足以下一项：

- 已验证错误机制；
- grounded patch/supporting target，且 quote 或 entity 得到代码佐证；
- grounded patch/supporting target，且存在任务相关直接 flow。

仅有高排名、通用检索证据或 provisional 证据的候选会被删除。诊断中记录
`rejected_seed_paths`。严格过滤可以产生空的 partial closure；这比把一个自信但错误的文件
交给补丁生成器更可解释。

### 3.3 已验证函数 owner 加成

Kimi profile 为 grounded、mechanism-verified 的精确实体提供 240 分上限内的 owner
加成。该值略高于现有单实体最高 225 分语义加成，确保 reviewer 已用代码、实体和 flow
验证的函数不会继续被同文件中的关键词密集 sibling 压住。

加成只作用于精确路径和实体名匹配；supporting target 只获得一半权重。默认、Qwen 和
MiMo 的该参数为 0，因此原排序保持不变。

## 4. 离线验证

聚焦测试结果：

```text
65 passed
```

覆盖：

- Kimi profile 三项开关与数值；
- 两个竞争 verified head 会阻止停止；
- 一个 verified owner 加导航候选可以完成对比；
- 同文件但不同代码区域的 snippet/entity 不能形成 verified mechanism；
- 宽泛 class 范围不能替代精确函数 owner 证据；
- Kimi 丢弃无 grounded owner 证据的旧 seed；
- default 仍保留原 fallback 行为；
- Kimi 的 verified 精确函数超过语义 sibling；
- default 的函数顺序不变；
- 文档过滤、闭包 guard 和既有候选复核回归。

Python 编译和 `git diff --check` 同时通过。离线测试未调用 Kimi API，也未覆盖任何历史
Omni296 或 RQ4 结果。

## 5. 两条在线金丝雀

使用 `/data2/like/locmy/.env.kimi.local` 中的 `ws-4tl5i0ym93z201nz` Kimi2.6
套餐运行，不覆盖 v1 结果。

Tailwind 首次 v2：File Acc@1 和 Recall@15 均保持 1.0；Function Recall@15 从
0.25 提升到 0.75，MRR 从 0.0714 提升到 0.1667；耗时从 157.0 秒降到
144.4 秒。严格 fallback 删除了没有机制证据的 `offsets.js`，但同时得到空 closure，
说明精度保护生效而 owner 证据仍不足。

mypy 首次 v2：耗时从 485.2 秒降到 282.2 秒，token 从 35,318 降到 26,516，
但 File Recall@15 从 0.5 降为 0。轨迹显示 Kimi 把 `checker.py` 第 909 行附近的
snippet 与约第 6233 行的 `lookup_typeinfo` entity 拼接为 verified mechanism，随后唯一
owner 和 closure promotion 放大了该误判。这个负结果直接促成上述 snippet/entity 对齐
校验。

对齐修复后复跑 mypy：`checker.py + lookup_typeinfo` 的错位证据已被正确降级，第一轮
`continue_search=true`，不再提前停止。但 Kimi 随后使用覆盖整个类的
`ExpressionChecker` entity 和类内无关片段，把 `checkexpr.py` 误判为 verified owner；
File Recall@15 仍为 0，且由于本地深层 flow 被执行两次，总耗时增至 695.7 秒。
该结果促成 class/module 精确名称限制。它说明 v2 已能逐层阻止虚假证据闭环，但本样本的
真正 gold `build.py`、`messages.py` 仍未进入候选前 15，根因已经转为上游召回不足，不能
再靠 owner rerank 修复。

## 6. 最终提交在线复跑

在最终提交 `c622cca` 上，继续使用同一份
`/data2/like/locmy/.env.kimi.local`、同一 Kimi2.6 套餐和同一 Tailwind 样本进行一次
独立复跑。结果写入：

```text
result/kimi_compat_v2_final_tailwind_20261112
```

本次请求全部成功，没有出现 401、429、超时或额度拒绝。耗时 106.9 秒，消耗
23,933 token。File Acc@1 保持 1.0，但 File Recall@15 从此前 v2 的 1.0 降至
0.5；Function Recall@15 从 0.75 降至 0.25，Function MRR 从 0.1667 降至
0.0667。修改闭包仍为空，没有把未经验证的相关文件交给下游修复。

阶段快照给出了关键因果证据：第二个 gold 文件 `src/corePlugins.js` 在 fast seed 中仍为
第 3，但在第一轮动态搜索后已经掉出 Top15；之后的 cross-round frontier、head selector
和 modification closure 都没有再看到该文件。最终提交新增的 class/module owner guard
只参与 reviewer 证据校验，并没有在本次 Tailwind 轨迹中直接删除该 gold。因此这次召回下降
不能归因于最后一道 owner guard，而是 Kimi 规划和动态检索存在独立运行间波动，且当前
seed retention 没有稳定保护高价值初始候选。

三次 Tailwind 配对结果如下：

| 版本 | File Recall@15 | File MAP@15 | Function Recall@15 | Function MRR | 时间 | Token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 | 1.00 | 0.8333 | 0.25 | 0.0714 | 157.0s | 23,548 |
| v2（最终 guard 前） | 1.00 | 0.6250 | 0.75 | 0.1667 | 144.4s | 24,005 |
| v2（最终提交） | 0.50 | 0.5000 | 0.25 | 0.0667 | 106.9s | 23,933 |

所以当前能支持的结论是：v2 的证据对齐和严格 fallback 确实阻止了已观察到的虚假 owner
闭环，并在一次 Tailwind 运行中显著改善了函数排序；但单次正向结果不能证明稳定的总体定位
提升。最终复跑显示上游召回波动足以抵消后置精度收益。现在不应启动 Omni296 全量重跑，
也不应宣称 Kimi 已被完全适配。

## 7. 下一步在线验证

先用 v1 已跑过的 Tailwind 和 mypy 困难样本做配对复测，再运行 8--12 条独立金丝雀。
重点比较：

- Function Acc@1/5/15 与首次正确函数排名；
- closure 中非 gold 相关源码数量；
- `owner_contrast_complete` 与竞争 owner 数；
- partial fallback 删除的旧 seed；
- token、LLM 事件、总时长。

只有在函数 Top1/5 或 closure precision 改善且文件召回不下降时，才值得启动完整
Omni296-Kimi 重跑。v2 输出必须使用新目录，不能覆盖旧实验。

下一版应优先修复两个与 owner guard 正交的问题：

1. 对 fast seed 中有多通道支持的候选设置最小保留配额，避免正确架构文件在第一轮直接掉出
   Top15；保留必须依赖确定性证据，不能无条件锁定所有初始候选。
2. 对缺失全局符号、初始化和注册链问题增加 architecture query expansion，使 mypy 的
   `build.py`、`messages.py` 能进入候选池。后置 owner rerank 无法恢复从未进入 Top15 的文件。

随后至少运行 8--12 条独立金丝雀，并对同一样本重复 2--3 次，分别报告均值、标准差和最差
结果。只有这样才能把代码改进与 Kimi 的采样波动区分开。
