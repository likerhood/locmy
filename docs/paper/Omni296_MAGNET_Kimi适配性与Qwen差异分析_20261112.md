# Omni296 上 MAGNET-Kimi 的适配性与 Qwen 差异分析

## 1. 核心结论

现有证据支持以下判断：**Kimi2.6 并非普遍不适合代码定位，但它对 MAGNET 在 Omni296 上采用的长链路、强结构化、细粒度收敛协议适配不足。** 问题主要不是“无法返回结果”，也不能继续简单归因于早期 HTTP 403，而是集中在以下三个环节：

1. **Top15 内的排序与证据压缩不足。** 292 条有逐例记录的样本都保留了 15 个文件，这是统一评测代码的固定 `TOP_K=15`，不能单独归因于 Kimi；真正的问题是正确文件在 Top15 内排名偏后，并且固定候选集的精确率较低。
2. **从文件到函数的证据传递损失严重。** 文件级还能通过扩大候选集获得较高召回，但模块和函数排序迅速恶化，说明模型能找到相关区域，却不稳定地识别真正需要修改的责任实体。
3. **控制器容易形成“内部自洽但外部错误”的闭环。** 部分轨迹被 MAGNET 的 closure 判为 complete，实际函数级仍未命中 gold，说明 Kimi 生成的因果说明可以满足当前协议的形式条件，却未必对应真实补丁机制。

因此，准确表述不是“Kimi 能力差”，而是：

> Kimi 在短链路、显式实体、单文件机制明确的任务上可以很好地驱动 MAGNET；但在 Omni296 的复杂跨文件问题中，它更容易宽泛探索、重复扩展和过早接受弱因果链，当前 MAGNET 提示词、结构化输出约束、停止策略和候选审查规则没有充分适配这种行为特征。

## 2. 数据来源与口径

本分析使用最终合并结果：

```text
omni296_addtest3_kimi26_merged_296_v1
```

合并清单记录如下：

| 项目 | 数值 |
|---|---:|
| 数据集 | `omni296-frozen` |
| 总样本数 | 296 |
| 原始运行覆盖 | 296 |
| 因 HTTP 403 定向重跑 | 111 |
| 保留原结果 | 185 |
| 用重跑结果替换 | 111 |
| 合并时调用 API | 否 |

合并规则是按 `instance_id` 替换 111 条受影响记录，并保持冻结数据集顺序。`source_mapping.csv` 为每条结果记录了 `original_185` 或 `rerun_403_111` 来源。因此，现有证据不支持“只是简单拼接导致重复、错位或漏行”的判断。

需要注意：296 条中有 285 条 `ok`、7 条 `partial`、4 条 failure。逐例指标 CSV 包含前 292 条可记录结果，4 条 failure 仍按预定全量分母策略计入聚合指标。

## 3. 最终指标揭示的主要问题

### 3.1 总体可用性并不差

| 指标 | 结果 |
|---|---:|
| Success / Partial / Failure | 285 / 7 / 4 |
| 最终失败率 | 1.35% |
| 文件预测为空率 | 1.35% |
| 平均预测文件数 | 14.80 |
| 平均每样本 LLM 事件数 | 36.97 |
| 平均每样本 token | 109,637 |
| 平均每样本耗时 | 661.12 秒 |
| 总 token | 32,452,580 |

这说明 Kimi 已经能够持续调用、生成结构化响应并完成大部分样本。若问题主要是 API 不可用或 JSON 完全无法解析，应该看到高 failure、高 empty 和低 token 消耗；实际情况正好相反：**调用充分、成本很高、结果也不为空，但排序精度不足。**

### 3.2 固定 Top15 评测不能直接等价为模型不收敛

292 条逐例记录的 `file_pred_count` 全部等于 15。代码审计确认，这是运行脚本与定位函数统一采用 `TOP_K=15` 的结果；聚合均值 14.80 则来自全量分母中的 4 条 failure。因此，“全部输出 15 个文件”是评测协议特征，不是 Kimi 独有行为，也不能据此证明 Kimi 拒绝停止。

MAGNET 已经将宽召回排名和修复集合分开：前者固定保留 Top15 用于 Acc@k 等公平比较，后者由 `modification_closure` 生成。Kimi 最终修复集合平均只有 1.65 个文件，说明框架确实执行了稀疏化。应当批评的是 Top15 内部排序质量和 closure 的校准，而不是固定候选数量本身。

| File 集合指标 @15 | 结果 |
|---|---:|
| Recall | 63.73% |
| Precision | 7.25% |
| F1 | 12.39% |
| Strict coverage | 51.35% |

文件召回达到 63.73%，但固定 Top15 的精确率只有 7.25%。这不是“完全找不到”，而是正确文件在宽召回列表中与大量相关但非修改目标的文件混在一起。当下游修复只接收 closure 选出的少量文件时，真正风险在于 closure 是否从 Top15 中选对，而不是直接把全部 15 个文件交给修复器。

1. 修复模型收到大量无关代码，真正责任文件被稀释；
2. 多文件候选引入不必要的补丁自由度，增加空补丁和应用失败；
3. 排名靠后的正确文件即使进入 Top15，也不一定进入实际修复上下文。

### 3.3 粒度越细，性能下降越明显

| 粒度 | Acc@1 | Acc@5 | Acc@15 | MRR@15 | MAP@15 | Set Precision @15 | Set F1 @15 |
|---|---:|---:|---:|---:|---:|---:|---:|
| File | 41.55% | 60.14% | 76.35% | 50.01% | 39.00% | 7.25% | 12.39% |
| Module | 17.57% | 40.20% | 65.20% | 29.26% | 21.41% | 6.15% | 10.49% |
| Function | 7.09% | 17.57% | 29.73% | 12.14% | 7.84% | 2.88% | 4.54% |

从 File Acc@15 的 76.35% 到 Function Acc@15 的 29.73%，下降 46.62 个百分点；Function Acc@1 只有 7.09%。这说明 Kimi 在 MAGNET 中的主要瓶颈不是仓库级检索，而是：

- 判断哪个文件真正承担根因；
- 将问题描述映射到具体函数或类；
- 区分调用入口、导航文件、公共基类和真实修改点；
- 对相似符号进行反事实排除；
- 将跨文件调用链压缩成少数可修改实体。

## 4. 111 条重跑说明了什么

原始日志中可观察到 560 次嵌套 `error` 和 560 次 `fallback`。111 条定向重跑后，这两类状态均归零，111 条全部完成为 `ok`。这证明新的 API 配置确实清除了主要基础设施故障。

但是，修复 API 后的指标变化并不是全面提升：

| 指标 | 原始 296 汇总 | 合并后 296 | 变化 |
|---|---:|---:|---:|
| File Acc@1 | 39.53% | 41.55% | +2.02 pp |
| File Acc@15 | 76.35% | 76.35% | 0.00 pp |
| File MRR@15 | 48.53% | 50.01% | +1.48 pp |
| Module Acc@15 | 61.49% | 65.20% | +3.71 pp |
| Module MRR@15 | 20.80% | 29.26% | +8.46 pp |
| Function Acc@15 | 35.81% | 29.73% | -6.08 pp |
| Function MRR@15 | 15.27% | 12.14% | -3.13 pp |
| Final patch-set complete rate | 11.23% | 14.74% | +3.51 pp |
| Final patch-set F1 | 29.54% | 32.07% | +2.53 pp |

这个结果应这样解释：

1. **重跑是必要的。** 它消除了 403/fallback，并明显改善模块排序和最终补丁文件集合。
2. **重跑不是充分条件。** 同一批 Kimi 新响应在函数层面反而更差，说明模型采样波动和细粒度决策不稳定。
3. **不能把最终差距继续归咎于 403。** API 故障解释了原始运行为什么需要重跑，却解释不了重跑后函数级仍低，也解释不了为什么所有样本都输出 15 个文件。
4. **合并后指标变化并非“不合理”。** 替换 111 条完整轨迹会同时改变文件、模块、函数和 closure；新轨迹可能在文件/模块上更好，在函数上更差。它不是简单地把失败改成成功，因此聚合结果不会单调上升。

## 5. “Kimi 适配性不高”应如何理解

### 5.1 不是普遍能力不足

在 SWE Clean15-92 的同一 MAGNET 框架中，Kimi 与 Qwen 的差距并不大：

| MAGNET 指标 | Qwen3.5 | Kimi2.6 | Kimi - Qwen |
|---|---:|---:|---:|
| File Acc@15 | 85.87% | 82.61% | -3.26 pp |
| Module Acc@15 | 76.09% | 77.17% | +1.08 pp |
| Function Acc@15 | 43.48% | 42.39% | -1.09 pp |
| File strict/SL | 48.91% | 46.74% | -2.17 pp |
| Function strict/SL | 14.13% | 15.22% | +1.09 pp |

这组数据排除了“Kimi 天生无法运行 MAGNET”的强结论。它在较小、任务结构更统一的 SWE 子集上能够接近 Qwen，模块 Acc@15 甚至略高。

### 5.2 适配问题主要在长链控制和复杂数据分布

Omni 包含多语言、多项目、URL/图像证据、不同构建体系以及复杂跨文件结构。Kimi 在现有 MAGNET 控制器中的行为更接近：

1. 先生成宽泛问题概念；
2. 为多个相关文件分别构造合理解释；
3. 对“可能有关”与“必须修改”的区分不够稳定；
4. 继续消耗搜索轮次补充证据；
5. 最终用满候选预算，但根因函数仍未被锁定。

这种行为在召回导向任务中未必差，但与 MAGNET 的目标存在偏差。MAGNET 需要控制器做的是“搜索后收敛”，而不是“搜索后罗列”。

### 5.3 其他框架上的模型差异也支持“协议敏感性”

OmniClean-458 的旧基线结果显示，Kimi 并非在所有框架上都一致落后：CoSIL 和 GALA 的若干文件级结果与 Qwen 接近，部分指标还略高；但在更依赖多轮代理、层级决策或图导航的 LocAgent、GraphLocator 中，差距明显扩大。例如：

| 框架 | 指标 | Qwen | Kimi | 差距 |
|---|---|---:|---:|---:|
| LocAgent | File Acc@15 | 89.52% | 72.93% | -16.59 pp |
| LocAgent | Function Acc@15 | 24.02% | 8.95% | -15.07 pp |
| GraphLocator | File Acc@15 | 55.68% | 51.09% | -4.59 pp |
| GraphLocator | Function Acc@15 | 12.01% | 7.21% | -4.80 pp |

这些结果不是 MAGNET-Qwen 的直接替代对照，但它们说明一个一致趋势：**Kimi 对不同代理协议的敏感度较高，尤其容易在细粒度结构化决策中损失性能。**

## 6. 代表性轨迹分析

### 6.1 成功案例：`dateutil__dateutil-763`

该样本定位非整数分钟时区偏移的处理逻辑，问题实体和调用机制相对明确。

| 项目 | 结果 |
|---|---:|
| Token | 26,066 |
| 耗时 | 109.8 秒 |
| Top1 文件 | `dateutil/tz/tz.py` |
| File / Module / Function Top1 | 全部命中 |
| Closure | complete |
| Mechanism verified | true |
| Adaptive lock | 已锁定 `dateutil/tz/tz.py` |

轨迹中，Kimi 能把 `tzfile`、`utcoffset`、seconds offset 和 `timedelta` 映射到同一文件，并验证根因机制。候选质量分数高，存在责任候选、根因候选和可引用源码，最终较早停止。

这说明当任务具备“显式领域实体 + 单一实现文件 + 可验证数据流”时，Kimi 与 MAGNET 的配合是有效的，并且成本显著低于总体均值。

### 6.2 过度探索案例：`tailwindlabs__tailwindcss-10276`

| 项目 | 结果 |
|---|---:|
| Token | 228,896 |
| 耗时 | 262.5 秒 |
| File Top1 / Top15 | 命中 / 命中 |
| Function Top1 / Top15 | 未命中 / 未命中 |
| Closure | incomplete |
| 缺失条件 | `root_patch_mechanism` |
| Adaptive lock | 空 |

Kimi 围绕 `setupContextUtils.js`、`generateRules.js`、`corePlugins.js`、`expandTailwindAtRules.js` 和 `resolveConfig.js` 持续扩展。它识别到样式、selector、plugin、configuration 等相关概念，却没有验证根补丁机制。最终文件范围相关，但函数候选被 `wrapped`、`parseStyles`、`ignoreNot`、`extractClasses` 等多个语义相似实体分散。

这是典型的“相关性定位成功、责任定位失败”：模型知道问题在哪一片区域，却无法用反证排除邻近实现，因此消耗超过 22 万 token 后仍未命中函数。

### 6.3 API 修复有效案例：`statsmodels__statsmodels-8669`

该样本在原始轨迹与重跑轨迹中都存在，可以直接观察基础设施恢复的价值。

| 阶段 | 耗时 | Token 记录 | Closure | 机制验证 |
|---|---:|---:|---|---|
| 原始 | 621.3 秒 | 缺失 | partial | false |
| 403 重跑 | 124.4 秒 | 43,183 | complete | true |

重跑后，Kimi 正确锁定 `statsmodels/discrete/discrete_model.py::fit`，File、Module、Function Top1 全部命中。这个案例证明：早期 403 的确破坏过控制器链路，定向重跑不是多余操作。

同时它也说明，API 恢复后 Kimi 在实体明确的样本上可以明显改善；问题在于这种改善没有稳定扩展到全部复杂样本。

### 6.4 形式闭环但真实定位不足：`python__mypy-15407`

该样本的重跑轨迹把 closure 判为 complete，并选择 `mypy/checker.py` 作为主要文件，但函数排序仍被 `nodes.py::deserialize`、`nodes.py::fullname`、`typeanal.py::__init__` 等宽泛实体占据，Function Top15 未命中。

这暴露出当前 closure 判定的一个漏洞：只要模型能够为某个文件构造覆盖 concern、effect 和 flow 的说明，就可能满足协议；但这些说明没有被“目标函数是否能解释最小复现”“删除该函数后因果链是否仍成立”等反事实检查约束。

因此，`closure_complete` 不能直接等价为定位正确。最终 42 条 closure complete 中仍存在文件、模块或函数未命中的样本，这也是后续优化应优先处理的地方。

## 7. 根因优先级

### 高置信根因

1. **Top15 内部排序和搜索停止不够校准。** 固定输出 15 个文件来自统一评测协议；Kimi 的真实问题是高成本搜索没有稳定提高 Top1/Top5 与函数排序。
2. **细粒度责任判定不足。** Function Acc@1、MRR 和 Precision 极低，文件相关性没有稳定传递到函数责任。
3. **根因机制验证约束偏软。** 轨迹允许在 `mechanism_verified=false` 或缺少 `root_patch_mechanism` 时继续保留大量候选。
4. **多轮成本没有换来同比收益。** 平均 10.96 万 token、37 次事件，仍只有 7.09% Function Acc@1，说明当前额外轮次主要增加候选广度，而非排序质量。

### 中等置信根因

1. **提示词更符合 Qwen 的结构化决策习惯。** 当前 JSON schema、verdict 和因果链模板可能使 Kimi倾向生成完整解释，而不是作出激进排除。
2. **反证权重不足。** Kimi 很容易为相关文件构造正向理由；若没有强制给出可证伪条件，候选分数会持续累积。
3. **跨语言统一提示造成分布偏差。** mypy、Babel、Webpack、Statsmodels 的职责结构差异很大，同一套 entity/flow 模板未必都适合。

### 已基本排除的主因

1. **大面积 API 不可用。** 重跑后 111/111 为 ok。
2. **结果普遍为空。** 最终 empty 仅 1.35%。
3. **简单合并错位。** 清单、来源映射、替换数量和冻结顺序相互一致。
4. **Kimi 完全不适合 MAGNET。** SWE92 上 Kimi 与 Qwen 的 MAGNET 指标接近。

## 8. 建议的适配改进

### 8.1 第一优先级：保持双输出，但加强修复集合校准

保留 Top15 宽召回列表用于与 Qwen 和 baseline 公平评测，不修改 Acc@k 的候选预算。同时让模型为修复阶段明确输出 `must_edit`、`supporting_context`、`rejected` 三组：

- `must_edit` 默认最多 3 个；
- `supporting_context` 最多 5 个，不进入修复上下文；
- 其余候选必须进入 `rejected` 并给出反证；
- 只有未覆盖必需因果角色时才允许扩大预算。

三组标签只影响 `final_patch_set` 和修复上下文，不删除 Top15 评测结果。重点观察 File Precision@3/5、Final Patch Set F1、Valid Repair Context Rate，而不只是 Acc@15。

### 8.2 第二优先级：函数责任的反事实审查

对每个 Top 函数强制回答：

1. 它直接读取或修改了哪个错误状态？
2. 最小复现是否必然经过它？
3. 若只修改该函数，预期行为如何改变？
4. 邻近候选为何不能独立解释故障？

缺少其中两项时，函数不得进入 Top5。这样可以抑制 `deserialize`、`fullname`、通用 `__init__` 等高词频但低责任实体。

### 8.3 第三优先级：校准 closure

将 closure 从“模型声称覆盖”改为“证据可验证覆盖”：

- `root_patch_mechanism` 必须绑定具体源码行和具体状态变化；
- `mechanism_verified=false` 时不得判 complete；
- 没有 adaptive lock 时，complete 降级为 partial；
- closure complete 后再运行一次独立 critic，只允许 critic 看问题、Top3 文件和证据，不看原控制器解释。

### 8.4 第四优先级：面向 Kimi 的协议简化

当前每样本平均约 37 次 LLM 事件，提示上下文不断膨胀。建议把控制协议压缩为三阶段：

1. `seed`: 只找 5 个文件；
2. `verify`: 对 Top3 做源码与调用链验证；
3. `commit`: 输出最多 3 个文件和 5 个函数。

中间轮次不再反复重述完整 issue sketch。只有验证失败时才回到 seed，一次最多扩展 3 个新候选。

### 8.5 代码层应增加“模型协议兼容层”，而不是复制一套 Kimi-MAGNET

当前代码已经有一部分 provider 兼容能力：`llm_client.py` 支持切换 `max_tokens`/`max_completion_tokens`、thinking 和 JSON response format，也支持 planning/evidence/controller 分阶段模型。但这些配置主要解决“请求能否成功”，还没有表达“某个模型更适合怎样的控制协议”。

建议增加显式的 `MYCODE_MODEL_PROFILE=default|qwen|kimi|mimo`，并通过一个集中式 profile 对象控制以下项目：

| 层次 | 通用核心 | Kimi profile 可调整项 |
|---|---|---|
| API 层 | 重试、usage、错误处理 | token 字段、thinking、超时、JSON mode |
| 控制器层 | 工具集合和安全约束 | 每轮最多决策数、查询长度、是否要求单一主目标 |
| Candidate review | 路径白名单、源码引用校验 | 输出 schema、反证数量、`must_edit` 上限 |
| 搜索停止 | 证据门槛和最大轮数 | 无新增机制时更早停止，限制重复扩展 |
| Closure | required obligations 和确定性验证 | critic 阈值、complete 校准，不改变 gold 定义 |
| Entity ranking | 实体必须来自索引 | Kimi 输出的精确实体只作有界加分，反证可扣分 |

推荐的代码边界是：

1. 新增 `model_profile.py`，只负责解析 profile 和提供不可变配置；
2. `llm_client.py` 只处理 provider 请求差异，不放定位策略；
3. `controller.py` 和 `candidate_reviewer.py` 接收 profile，构造不同紧凑度的 schema；
4. `search_agent.py` 保持统一检索、图搜索、源码校验和评价逻辑，只读取 profile 给出的预算与阈值；
5. 所有模型仍使用同一个 gold、Top15、Acc@k 和 closure 定义，避免为 Kimi 降低正确标准。

Kimi profile 的第一版可以采用以下保守设置：

```text
controller_decisions_per_round = 2
candidate_review_limit = 4
must_edit_limit = 3
supporting_context_limit = 3
required_counterevidence_per_rejected_candidate = 1
no_new_verified_mechanism_rounds = 1
independent_critic_on_complete = true
exact_entity_required_for_function_boost = true
```

这里的“兼容性”是让模型更稳定地表达同一个决策任务，不是给 Kimi 更宽松的命中标准，也不是对 Kimi 的分数单独加权。

### 8.6 小规模验证设计

不要直接重跑 296 条。先选 12 条金丝雀：

- 3 条当前低成本成功案例；
- 3 条文件命中但函数失败案例；
- 3 条 closure complete 但 gold 未命中案例；
- 3 条原 403 重跑后指标下降案例。

统一比较旧协议与新协议：

| 指标 | 目标 |
|---|---|
| Top15 候选数 | 固定为 15，保持评测口径 |
| Final patch set 平均文件数 | 维持约 1-3 个，不靠扩大集合提分 |
| File Recall@5 | 不明显下降 |
| File Precision@5 | 显著提高 |
| Function Acc@5 | 提高 |
| Closure false-positive rate | 降低 |
| 平均 token | 至少降低 30% |

只有当函数指标和 precision 同时提升，才能说明适配真正改善；单独提高 Acc@15 可能只是再次扩大候选集合。

## 9. 关于 Qwen 对照的证据边界

当前已经找到的 Qwen Omni296 归档主要属于 LocAgent、CoSIL、GALA 等 baseline。仓库审计记录还指出，曾存在较新的 `omni_clean15_qwen_server_v1` 聚合指标和逐例 CSV，但现有可访问材料中尚未确认到与本次 Kimi 完全同代码、同 296 条顺序、同 MAGNET 配置的完整 Qwen agent trace。

因此，目前可以严谨地说：

- Kimi 在 MAGNET-Omni296 中存在明确的收敛和细粒度适配问题；
- SWE92 的同框架结果表明 Kimi 不是普遍不兼容；
- Omni 其他框架结果显示 Kimi 对代理协议较敏感；
- **尚不能仅凭现存材料精确计算 MAGNET-Qwen 与 MAGNET-Kimi 在 Omni296 上的逐例胜负、token 差和轨迹差。**

若后续找到 Qwen 的同口径 `per_instance_metrics_3level.csv`、`agent_traces.jsonl` 和 `token_usage.jsonl`，应进一步做配对分析：Qwen 成功/Kimi 失败、Kimi 成功/Qwen 失败、两者均命中但排名不同、两者均失败四组，并报告 bootstrap 置信区间。这样才能把“模型适配差异”从当前的高可信诊断提升为严格的配对实验证据。

## 10. 最终判断

**是的，Kimi 对当前 MAGNET-Omni 协议的适配性确实不够高，但不是简单的模型能力不足。** 最准确的归因是：

> 当前 MAGNET 能让 Kimi 广泛收集相关证据，却没有充分约束它完成候选压缩、根因验证和函数级责任排序。API 403 曾经造成明显干扰，但重跑后的主要瓶颈已经转变为协议适配和控制策略问题。

论文中不宜写成“Kimi 比 Qwen 差所以结果低”，更合适的表述是：

> MAGNET 对基础模型的结构化控制特性较为敏感。Kimi 在统一、显式的 SWE 任务上可获得与 Qwen 接近的性能，但在异构 Omni 场景中更倾向于以高候选覆盖换取召回，导致文件精度和函数级责任识别下降。该现象表明，面向不同基础模型的停止校准、反证式候选筛选和细粒度机制验证是提升框架稳健性的关键。
