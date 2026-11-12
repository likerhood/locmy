# MAGNET Kimi 兼容层 v3：多通道种子保留

## 1. 分支与目标

- 分支：`magnet-kimi-compat-v3`
- 基础：`magnet-kimi-compat-v2` / `5623b4c`
- 工作树：`/data2/like/locmy-kimi-compat`

v2 已加强 owner 对比、snippet/entity 对齐和 partial closure 过滤，但最终 Tailwind
复跑暴露出一个更上游的问题：正确文件 `src/corePlugins.js` 在 fast seed 中排第 3，拥有
5 个检索通道支持，却因为最终 seed retention 固定只保留前 2 个而在第一轮后掉出 Top15。

v3 只修复这一条候选保留路径，不改变数据集、gold、Top-K、搜索工具、owner 校验和 closure
规则，也不修改 Qwen、MiMo 或 default profile。

## 2. 实施内容

Kimi profile 新增：

```text
final_seed_retention_limit = 3
seed_retention_min_axes = 3
```

含义如下：

1. Kimi 最多保留 3 个 fast seed，而不是固定保留 2 个。
2. 候选至少需要 3 个独立支持通道才能占用保留位。
3. planner 明确标记为 persistent 的 seed 可以绕过通道数量门槛。
4. 缺少多通道证据的 seed 会记录为 `insufficient_multi_channel_support`，不会因为初始排名
   靠前而被无条件锁定。
5. 保留诊断记录通道数量、通道名称和 persistent 状态。

default、Qwen 和 MiMo 的默认值仍为保留 2 个、最低通道数 0，因此原行为不变。环境变量
`MYCODE_FINAL_SEED_PREFIX` 和 `MYCODE_FINAL_SEED_MIN_AXES` 仍可显式覆盖 profile。

## 3. 验证

当前系统 Python 缺少 pytest，因此本轮直接执行了全部 7 项不依赖 pytest 的 seed-frontier
回归函数，结果为 `7 passed`。同时验证：

- Kimi profile 为 `3 / 3`；
- Qwen profile 仍为 `2 / 0`；
- 第 3 个五通道 seed 被保留；
- 单通道弱 seed 被拒绝；
- persistent seed 仍可保留；
- Python 编译和 `git diff --check` 通过。

## 4. Tailwind 在线配对结果

使用同一份 Kimi2.6 套餐、同一数据、同一 Top15 和两轮深度搜索配置运行：

```text
result/kimi_compat_v3_seed_retention_tailwind_20261112
```

| 指标 | v2 最终复跑 | v3 | 变化 |
| --- | ---: | ---: | ---: |
| File Acc@1 | 1.00 | 1.00 | 0 |
| File Recall@15 | 0.50 | 1.00 | +0.50 |
| File Strict Acc@15 | 0.00 | 1.00 | +1.00 |
| File MAP@15 | 0.5000 | 0.8333 | +0.3333 |
| Function Acc@1 | 0.00 | 1.00 | +1.00 |
| Function Recall@15 | 0.25 | 0.75 | +0.50 |
| Function MRR | 0.0667 | 1.0000 | +0.9333 |
| 时间 | 106.9s | 122.5s | +15.6s |
| Token | 23,933 | 23,956 | +23 |

v3 最终保留：

```text
src/lib/setupContextUtils.js
src/lib/generateRules.js
src/corePlugins.js
```

其中 `corePlugins.js` 有 5 个支持通道，最终保持在文件第 3 名；两个 gold 文件均进入
Top15。函数级 Top1 也首次命中 gold。token 基本不变，说明提升来自确定性的候选保留，
不是增加 API 请求或输出预算。

修改闭包仍为空，表明 v3 没有绕过 v2 的 owner 和 mechanism guard。该样本同时改善召回，
又保持了下游修复上下文的精度保护。

## 5. 结论与限制

本次结果验证了具体因果假设：固定 Top2 seed retention 会丢失有强多通道证据的正确第 3
候选；Kimi 专用的 `Top3 + 最少 3 通道` 策略能够恢复该候选，而且没有增加明显 token 成本。

但这仍是一条样本的一次在线运行，不能直接推断 Omni296 总体提升。下一步应在 8--12 条
独立样本上检查：弱 fast seed 是否被正确拒绝、第三保留位是否提高完整召回，以及是否会把
错误但多通道相关的文件稳定锁入 Top15。至少重复运行关键样本 2 次，才能估计 Kimi 采样波动。
