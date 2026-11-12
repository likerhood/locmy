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
63 passed
```

覆盖：

- Kimi profile 三项开关与数值；
- 两个竞争 verified head 会阻止停止；
- 一个 verified owner 加导航候选可以完成对比；
- Kimi 丢弃无 grounded owner 证据的旧 seed；
- default 仍保留原 fallback 行为；
- Kimi 的 verified 精确函数超过语义 sibling；
- default 的函数顺序不变；
- 文档过滤、闭包 guard 和既有候选复核回归。

Python 编译和 `git diff --check` 同时通过。此阶段未调用 Kimi API，也未覆盖任何历史
Omni296 或 RQ4 结果。

## 5. 下一步在线验证

先用 v1 已跑过的 Tailwind 和 mypy 困难样本做配对复测，再运行 8--12 条独立金丝雀。
重点比较：

- Function Acc@1/5/15 与首次正确函数排名；
- closure 中非 gold 相关源码数量；
- `owner_contrast_complete` 与竞争 owner 数；
- partial fallback 删除的旧 seed；
- token、LLM 事件、总时长。

只有在函数 Top1/5 或 closure precision 改善且文件召回不下降时，才值得启动完整
Omni296-Kimi 重跑。v2 输出必须使用新目录，不能覆盖旧实验。
