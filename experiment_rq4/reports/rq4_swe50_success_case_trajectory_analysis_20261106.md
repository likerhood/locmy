# RQ4 SWE50 成功案例与轨迹分析

## 1. 分析口径

本文分析最终成功集合中的 8 条样本。`Selected` 指在线流程最终选择的补丁；`Oracle` 指实验结束后对候选逐个进行官方评测，只用于诊断，不能回填在线解决率。

分析证据包括：细粒度定位结果、修复计划、候选数量、最终补丁、选择票数、补丁应用状态、FAIL_TO_PASS 与 PASS_TO_PASS 官方测试，以及三条追加实验的候选级 Oracle 结果。

## 2. 案例总览

| 样本 | 问题本质 | 成功方法数 | 主要决定因素 |
|---|---|---:|---|
| `wp-calypso-33752` | React 混合子节点中的 widow prevention | 3/5 | 输出数据结构是否保持不变 |
| `Chart.js-9027` | 非有限小数位参数进入 `Intl.NumberFormat` | 3/5 | 是否从异常栈定位到格式化入口 |
| `Chart.js-8567` | 布局尺寸更新后才判断变化，导致 refit 永远不触发 | 1/5 | 精确定位到 `updateDims`，以及候选选择 |
| `Chart.js-9766` | stacked line 的 min/max 未按累计栈值计算 | 4/5 | 栈范围建模与候选选择 |
| `marked-1825` | GFM 关闭时仍无条件识别 task list | 5/5 | 根因明确，局部条件修复容易生成 |
| `marked-684` | 不同类型相邻列表被合并 | 1/5 | 旧版正则边界复杂，定位和选择都困难 |
| `marked-1435` | link href 不支持平衡括号 | 3/5 | 修改语法规则比事后修补解析结果可靠 |
| `p5.js-5555` | WebGL `alpha` 默认值错误 | 3/5 | 是否修改真正的运行时默认值 |

## 3. Case 1：`wp-calypso-33752`

### 问题与正确修复方向

`translate()` 在传入 `components = { strong: <strong /> }` 后返回混合的字符串与 React 元素，而原有 `preventWidows()` 只处理字符串。正确方向是对翻译结果中的文本片段插入不换行空格，同时保留 `<strong>` 和组件返回结构。

### 轨迹对比

| 方法 | 定位与补丁 | 候选 | 结果 |
|---|---|---:|---|
| MAGNET | 精确定位 `getSupportText`；仅修改 `index.jsx`，对文本片段做 widow prevention | 9/10 有效，选择票 1 | 通过 1 个 F2P、14 个 P2P |
| LocAgent | 定位组件及公共 `preventWidows`；扩展公共函数以处理数组 | 10/10 有效，选择票 1 | 通过 |
| CoSIL | 定位正确，但用 `React.Children.map` 包装返回值 | 8/10 有效，选择票 1 | F2P 失败 |
| GALA | 定位正确；在组件内处理字符串片段 | 8/10 有效，选择票 1 | 通过 |
| GraphLocator | 定位到另一个 post-normalizer widow 工具，未修改实际组件 | 3/10 有效，7 个空补丁 | F2P 失败 |

CoSIL 并非没有插入 NBSP，而是把原测试期望的字符串 children 变成了长度为 1 的数组：断言显示实际值为 `[Array(1)]`，预期是字符串。它修复了表面文本，却破坏了组件输出契约。GraphLocator 则属于文件级定位偏移，补丁逻辑再复杂也没有触及实际渲染入口。

### Case 结论

该样本证明“定位到相关概念”不够，必须定位到实际数据流，并保持返回类型等接口契约。MAGNET 的优势是修复范围小且与定位计划一致；LocAgent 的修复更通用，但修改了共享工具，影响面更大。

## 4. Case 2：`Chart.js-9027`

### 问题与正确修复方向

布局异常会让 tick precision 变成 `NaN`，最终作为 `minimumFractionDigits` 或 `maximumFractionDigits` 传入 `Intl.NumberFormat`，触发 `RangeError`。直接防线是过滤非有限小数位参数；更宽的修复还可以阻止 NaN 尺寸和范围向下游传播。

### 轨迹对比

| 方法 | 定位与补丁 | 候选 | 结果 |
|---|---|---:|---|
| MAGNET | 由异常栈精确定位 `helpers.intl.js`；增加 7 行参数净化 | 10/10 有效 | 通过 |
| LocAgent | 同时修正 tick precision、Intl 参数和 linear range | 10/10 有效 | 通过 |
| GALA | 净化 Intl 参数，并对 layout 尺寸增加 0 fallback | 8/10 有效 | 通过 |
| CoSIL | 细粒度定位失败，没有进入候选生成 | 0 | 无补丁 |
| GraphLocator | 细粒度定位失败，没有进入候选生成 | 0 | 无补丁 |

### Case 结论

这是异常栈信息非常强的案例。MAGNET 的最小补丁直接在异常边界建立防线，成本最低；LocAgent 和 GALA 的补丁更宽，但也通过官方测试。CoSIL 与 GraphLocator 的失败发生在修复模型之前，属于纯定位覆盖问题。

## 5. Case 3：`Chart.js-8567`

### 问题与精确根因

真正缺陷位于 `src/core/core.layouts.js::updateDims`：代码先把 `chartArea.w/h` 更新成 `newWidth/newHeight`，再比较两者是否变化，因此比较恒为 false。布局系统无法报告尺寸变化，`fitBoxes` 不会重新适配垂直 scale，最终出现 y 轴标签重叠。

### Selected 与 Oracle 轨迹

| 方法 | 定位/Selected 方向 | 有效候选 | Selected | Oracle 成功 |
|---|---|---:|:---:|---:|
| MAGNET | 主要修 label sampling 与 tick density | 9 | × | 3/9（0、6、9） |
| LocAgent | 同时改 label、tick、layout 多处启发式 | 10 | × | 0/10 |
| CoSIL | 修 tick limit 与 sample | 10 | × | 0/10 |
| GALA | 修 tick 生成和 layout 尺寸 | 10 | × | 4/10（3、4、5、8） |
| GraphLocator | 精确定位 `updateDims` 的先赋值后比较 | 10 | ✓ | **10/10** |

GraphLocator 的 Selected 只改 `core.layouts.js`，先保存 `widthChanged/heightChanged`，再更新 chart area，并返回保存的变化标志。补丁仅增加 5 行、删除 2 行，选择票数为 3；10 个候选只有 6 个唯一补丁，说明多个候选收敛到了相同根因。

MAGNET 和 GALA 的候选池中分别有 3、4 个官方可解补丁，但 Selected 选择票均只有 1，最终选中了错误候选。这是当前数据中最清楚的“选择器损失”。LocAgent 与 CoSIL 的 10 个候选全部失败，说明它们不仅选择错误，候选生成方向本身也没有覆盖精确根因。

### Case 结论

该案例是 GraphLocator 的代表性优势：图结构把尺寸更新与 `fitBoxes` 的控制依赖联系起来，定位到了局部但关键的状态变化错误。它同时说明 MAGNET 的主要改进点并非增加更多候选，而是利用修复计划、改动范围和代码因果关系重排候选。

## 6. Case 4：`Chart.js-9766`

### 问题与正确修复方向

stacked line 的 y 轴范围不能从单个 dataset 的原始值或隐含零基线推导，必须按 index 和 stack 累加可见数据集，再用累计值确定 min/max。否则即使 `beginAtZero: false`，最低 tick 仍会回到 0。

### Selected 与 Oracle 轨迹

| 方法 | 有效候选 | Selected | Oracle 成功 | 轨迹解释 |
|---|---:|:---:|---:|---|
| MAGNET | 7 | ✓ | **7/7** | 在 scale 层实现累计栈范围，候选生成非常稳定 |
| LocAgent | 7 | × | 2/7（4、5） | 已生成正确补丁，但选择了候选 8 |
| CoSIL | 10 | ✓ | 8/10 | 大多数候选正确 |
| GALA | 6 | ✓ | 2/6 | Selected 恰好选中成功候选 4 |
| GraphLocator | 5 | ✓ | 4/5 | 有效候选较少，但正确率高 |

五种方法的定位计划都识别到 stacked min/max；差异主要来自实现细节与选择。MAGNET 的 7 个有效候选全部通过官方测试，是它最强的候选生成案例。LocAgent 并不是定位或生成完全失败，而是 7 个候选中已有 2 个可解补丁，最终选择器没有选中。

### Case 结论

该案例证明相同修复协议下，定位信息能够显著改变候选池质量；也证明 `Resolved@1` 会同时混合“能否生成正确补丁”和“能否选中正确补丁”两个能力。

## 7. Case 5：`marked-1825`

### 问题与正确修复方向

task list 是 GFM 扩展，但 `Tokenizer.list` 无条件识别 `[ ]`/`[x]`。正确修复是在 GFM 关闭时保留文本，不生成 task/checked token。

### 轨迹对比

| 方法 | 修复方式 | 候选特征 | 结果 |
|---|---|---|---|
| MAGNET | 在 task 检测条件增加 `this.options.gfm` | 10 个唯一有效候选 | 通过 |
| LocAgent | GFM 关闭时预先转义 checkbox 标记 | 4/10 有效 | 通过 |
| CoSIL | Renderer 在非 GFM 时输出普通 `[ ]`/`[x]` | 7/10 有效 | 通过 |
| GALA | 与 MAGNET 同类的一行条件修复 | 10/10 有效，4 个唯一，票数 7 | 通过 |
| GraphLocator | 与 GALA 完全相同的 Selected 补丁 | 10/10 有效，3 个唯一，票数 8 | 通过 |

GALA 与 GraphLocator 的最终补丁哈希完全一致，说明不同定位方法在根因清晰时能够收敛到同一最小修复。CoSIL 从渲染层修复，LocAgent从输入预处理修复，虽然都通过测试，但层次不如 tokenizer 条件修复自然。

### Case 结论

这是五方法共同成功的唯一案例。它说明当问题描述包含明确配置开关、错误行为和局部语义时，定位差异对最终结果的影响会显著缩小。

## 8. Case 6：`marked-684`

### 问题与正确修复方向

旧版 Marked 的 block list/item 正则会吞掉分隔换行，使无序列表后的有序列表继续进入同一个 list token。修复必须正确识别列表类型切换和空行边界，同时保持 loose list 等旧行为。

### 轨迹对比

| 方法 | 轨迹 | 结果 |
|---|---|---|
| LocAgent | 在 `Lexer.prototype.token` 中识别不同列表类型的相邻边界并插入额外分隔 | Selected 通过 |
| MAGNET 主批次 | 修改 `block.item`，但未完整处理 list token 的终止语义 | Selected 失败 |
| CoSIL | 细粒度定位失败 | 无补丁 |
| GALA | 简化 `block.list` 终止条件，破坏/遗漏边界 | Selected 失败 |
| GraphLocator | 修改 list 与 item 正则，但仍未满足 spec | Selected 失败 |

MAGNET 后续 MiMo-v2.6-pro 定向重跑得到 3 个有效候选。原 Selected 官方运行未解决；修正 Node 运行时后，候选 1、5 通过，候选 9 失败。按既定口径，这些是后验 Oracle 证据，不能回填 MAGNET 的 Resolved@1。

### Case 结论

LocAgent 的补丁不是最优雅的语法层修复，但它对该版本行为具有针对性并通过 spec。MAGNET 定向重跑已经具备生成正确补丁的能力，主要损失来自选择与评测环境敏感性。这一案例适合用于说明“Selected 失败不等于候选池完全失败”。

## 9. Case 7：`marked-1435`

### 问题与正确修复方向

Markdown link 的 href 中包含平衡括号时，旧正则会在第一个 `)` 提前结束，导致 URL 只有转义 `(` 后才能解析。正确方向是在 link grammar 中支持有限深度的平衡括号。

### 轨迹对比

| 方法 | 修复方式 | 结果 |
|---|---|---|
| MAGNET | 直接替换 inline link grammar，允许嵌套括号 | 通过 |
| CoSIL | 同样在 link grammar 支持平衡括号 | 通过 |
| GALA | 扩展 `href` 子模式支持嵌套括号 | 通过 |
| LocAgent | 修改 link match 后的括号扫描逻辑 | `Marked Links example 16` 失败 |
| GraphLocator | 细粒度定位失败 | 无补丁 |

LocAgent 虽然理解了问题，但在正则已经截断匹配结果之后再调整 `findClosingBracket`，无法可靠恢复完整 href，属于“修复位置晚于信息丢失点”。三个成功方法都在语法规则入口解决问题。

### Case 结论

此案例说明调用链上的位置先后很关键：应在数据第一次被错误截断的位置修复，而不是在后处理阶段尝试补救。

## 10. Case 8：`p5.js-5555`

### 10.1 问题契约与正确修复点

该缺陷不是普通的文档默认值不一致，而是 WebGL context 的运行时属性错误。`p5.RendererGL.prototype._setAttributeDefaults` 将 `alpha` 默认设为 `true`，导致 drawing buffer 带有 alpha 通道；绘制半透明图形时，浏览器会将结果与网页背景合成，而不是像 2D renderer 那样与 canvas 背景合成。

一个完整修复必须同时满足以下行为约束：

1. 默认 WebGL canvas 使用 `alpha: false`。
2. 用户显式调用 `setAttributes('alpha', true)` 时仍能获得透明 canvas。
3. 主 canvas、`p5.Graphics(..., WEBGL)` 与 context reset 路径使用一致的默认行为。
4. 不能只修改 JSDoc；官方目标测试观察的是实际像素合成行为。

因此最直接的根因修复是在 `_setAttributeDefaults` 的 defaults 对象中执行：

```diff
- alpha: true,
+ alpha: false,
```

### 10.2 细粒度定位与修复计划

本案例适合控制“文件级定位是否正确”这个变量，因为五种方法都把 `src/webgl/p5.RendererGL.js` 列为唯一预期修改文件，并且都在修复计划中明确指出运行时默认值应由 `true` 变为 `false`。

| 方法 | 细粒度定位 | 计划中的根因判断 | 定位特点 |
|---|---|---|---|
| MAGNET | `_setAttributeDefaults`、`_initialize` | defaults 中的 `alpha: true` 造成错误合成 | 直接覆盖“默认值定义 → context 创建”链路 |
| LocAgent | `_setAttributeDefaults`、`setAttributes`、公共 setter | 默认 alpha 开启，应改为关闭并保留显式 override | 根因正确，但范围稍宽 |
| CoSIL | `setAttributes` | 明确要求运行时 `alpha: true → false` | 文件和语义正确，具体位置没有直接指向 defaults 行 |
| GALA | `setAttributes`、setup 相关代码 | 明确要求默认值变为 false，并更新文档 | 同时强调运行时和文档，范围较宽 |
| GraphLocator | `_initContext`、`_resetContext`、constructor、`setAttributes` | context attributes 应默认 `alpha: false` | 调用链覆盖完整，但定位较分散 |

这意味着 CoSIL/GALA 的失败不能简单归因为“没有找到文件”或“没有理解 issue”。它们的计划已经写出了正确运行时状态迁移，错误发生在计划向候选补丁的转换阶段。

### 10.3 十候选生成轨迹

| 方法 | Generated | Empty | 编辑应用失败 | 唯一补丁 | RuntimePatch@10 | 候选阶段主要现象 |
|---|---:|---:|---:|---:|---:|---|
| MAGNET | 10 | 0 | 0 | 1 | **10/10** | 10 次均直接替换运行时默认值，且补丁完全相同 |
| LocAgent | 5 | 0 | 5 | 5 | **5/10** | 生成的 5 个补丁都修改运行时代码，但另外 5 次无法应用 |
| CoSIL | 4 | 5 | 1 | 4 | **0/10** | 4 个有效补丁全部只修改注释/JSDoc |
| GALA | 9 | 0 | 1 | 3 | **0/10** | 9 个有效补丁全部只修改文档默认值 |
| GraphLocator | 6 | 1 | 3 | 6 | **6/10** | 6 个有效补丁均影响运行时，但生成稳定性和文本共识较弱 |

这里的 `RuntimePatch@10` 只判断候选是否实际修改运行时代码，不等同于候选已经通过官方测试。它比单独报告 `Valid@10` 更能解释本案例：GALA 的 `Valid@10=9/10` 看似很好，但 `RuntimePatch@10=0/10`，所以这些“有效”补丁只是在语法和应用层面有效，并没有修复行为。

五种方法的候选池可分成三类：

1. **根因级运行时修改**：MAGNET 直接修改 defaults，10 个候选全部收敛到同一补丁。
2. **等价但间接的运行时修改**：LocAgent 在 constructor 中增加默认覆盖；GraphLocator 通过后置重复 key 让 `alpha: false` 生效。
3. **计划正确、执行退化为文档修改**：CoSIL 与 GALA 的候选没有落实计划中明确写出的运行时状态迁移。

### 10.4 Selected 补丁与代码质量

| 方法 | Selected | 票数 | 改动行数 | 运行时效果 | 代码质量判断 |
|---|---:|---:|---:|---|---|
| MAGNET | candidate 0 | **10** | 2 | 直接将默认值改为 false | 最小、直接、与计划完全一致 |
| LocAgent | candidate 3 | 1 | 8 | constructor 中未显式传 alpha 时强制 false | 行为正确，但绕过 defaults 定义，注释和逻辑更重 |
| CoSIL | candidate 9 | 1 | 4 | 无；只新增 alpha JSDoc | 不满足行为修复要求 |
| GALA | candidate 0 | **7** | 2 | 无；只把文档 true 改为 false | 高共识但语义错误 |
| GraphLocator | candidate 3 | 1 | 3 | 在 `alpha: true` 后追加 `alpha: false`，后一个 key 生效 | 测试可通过，但重复对象 key 可读性和维护性较差 |

MAGNET 的选择票数为 10，不只是“10 个候选都能应用”，而是 10 个候选的标准化补丁哈希完全相同。这是文本共识、语义共识和官方行为结果三者一致的少见案例。

GALA 则提供了反例：7 票选中了同一个文档补丁，说明当前哈希投票只能测量候选是否相似，不能判断它们是否改变了目标行为。**高票数并不自动等于高修复可信度。**

### 10.5 计划—补丁一致性

为避免把主观印象包装成正式 benchmark，本节只使用一个透明的案例研究量表。四项分别是：根因是否正确、预期文件是否正确、计划是否明确要求运行时迁移、Selected 是否真正执行运行时迁移。

| 方法 | 根因正确 | 文件正确 | 计划要求运行时修改 | Selected 修改运行时 | 计划—补丁一致性 |
|---|:---:|:---:|:---:|:---:|---|
| MAGNET | ✓ | ✓ | ✓ | ✓ | 完整；且实现点与计划精确一致 |
| LocAgent | ✓ | ✓ | ✓ | ✓ | 完整；实现语义一致但位置更间接 |
| CoSIL | ✓ | ✓ | ✓ | × | 计划到补丁发生语义丢失 |
| GALA | ✓ | ✓ | ✓ | × | 计划到补丁发生语义丢失 |
| GraphLocator | ✓ | ✓ | ✓ | ✓ | 行为一致，但实现含重复 key |

因此，本案例最有价值的归因不是“MAGNET 定位正确、其他方法定位错误”，而是：**在定位和计划均基本正确的条件下，MAGNET 对修复计划的执行忠实度最高。**

### 10.6 官方评测结果

| 方法 | Patch applied | F2P | P2P | Resolved@1 | 失败定位 |
|---|:---:|---:|---:|:---:|---|
| MAGNET | ✓ | **13/13** | **2421/2421** | ✓ | 无 |
| LocAgent | ✓ | **13/13** | **2421/2421** | ✓ | 无 |
| CoSIL | ✓ | 12/13 | **2421/2421** | × | 透明 GL canvas 目标测试失败 |
| GALA | ✓ | 12/13 | **2421/2421** | × | 透明 GL canvas 目标测试失败 |
| GraphLocator | ✓ | **13/13** | **2421/2421** | ✓ | 无 |

CoSIL 与 GALA 的 `2421/2421 P2P` 表明它们没有引入已有测试回归，但这不是修复成功：唯一失败的 F2P 正是验证该 issue 的目标测试 `p5.RendererGL:GL Renderer clear():transparent GL graphics with GL canvas`。换言之，它们保持了旧行为，却没有让错误行为发生变化。

### 10.7 Token 与时间成本

`prediction.jsonl` 中的 total token 已经包含细粒度定位和 10 次候选调用，因此不能再次叠加 fine-localization token。下表中的候选阶段 token 使用 `total - fine localization` 计算；耗时是修复 attempt 的生成耗时，不包含后续官方 Docker 评测时间。

| 方法 | 定位 Token | 候选阶段 Token | 总 Token | 候选阶段 Token/有效候选 | Token/运行时候选 | 生成耗时 |
|---|---:|---:|---:|---:|---:|---:|
| MAGNET | 72,058 | 47,422 | 119,480 | **4,742** | **4,742** | **77.0 s** |
| LocAgent | 5,806 | 128,400 | 134,206 | 25,680 | 25,680 | 1,218.3 s |
| CoSIL | 12,195 | 82,065 | 94,260 | 20,516 | N/A | 677.1 s |
| GALA | 7,620 | 74,875 | **82,495** | 8,319 | N/A | 545.1 s |
| GraphLocator | 7,720 | 98,705 | 106,425 | 16,451 | 16,451 | 742.1 s |

成本数据需要结合产出语义解释：

- MAGNET 的定位输入异常大，定位阶段消耗 72,058 token，其中 prompt 为 71,274 token，原因是 top-15 上下文较宽；这是它仍可优化的明显成本。
- 但 MAGNET 的候选阶段最稳定，每个可运行候选约 4.7K token，10 次调用没有空补丁或应用失败。
- LocAgent 总 token 最高，5 次应用失败仍消耗了大量推理 token；候选阶段每个有效补丁约为 MAGNET 的 5.4 倍。
- GALA 总成本最低，却把 9 个有效候选全部花在文档修改上，说明“便宜”和“候选共识高”都不能替代行为有效性。
- CoSIL/GALA 没有运行时候选，所以 `Token/运行时候选` 不应写成 0，而应记为 N/A；它们在这一案例中的行为产出率为零。

### 10.8 失败归因与可用指标

该案例可将修复链路分成五个阶段：

| 阶段 | MAGNET | LocAgent | CoSIL | GALA | GraphLocator |
|---|---|---|---|---|---|
| 文件/符号定位 | 成功 | 成功 | 成功 | 成功 | 成功 |
| 根因计划 | 成功 | 成功 | 成功 | 成功 | 成功 |
| 计划转运行时补丁 | **10/10** | 5/10 | **0/10** | **0/10** | 6/10 |
| 在线选择 | 成功且强共识 | 成功但弱共识 | 选中无行为补丁 | 高共识选中无行为补丁 | 成功但弱共识 |
| 官方行为验证 | Resolved | Resolved | Failed | Failed | Resolved |

建议在 RQ4 汇总中，除 `Valid@10`、`Apply@10` 和 `Resolved@1` 外，增加以下诊断指标：

1. `RuntimePatch@10`：10 次尝试中实际修改运行时代码的候选数。
2. `Plan-Patch Alignment`：计划要求的文件、状态迁移和边界条件是否在 Selected 中落实；应报告分项，不宜只给不透明总分。
3. `SemanticConsensus@10`：按行为语义聚类后的最大候选簇，而不只按 patch hash 投票。
4. `Token/RuntimeCandidate`：候选阶段 token 除以运行时候选数，用于区分“生成便宜”和“生成有行为价值”。
5. `Oracle@10`：后验逐候选官方评测，用于区分候选池不足与选择器错误；不能用于回填在线 `Resolved@1`。

### 10.9 Case 结论

`p5.js-5555` 是 MAGNET 最有解释力的成功案例，但论点应准确表述为：在五种方法均找到正确文件、均写出正确根因计划的条件下，MAGNET 唯一实现了 `Valid@10=10/10`、`RuntimePatch@10=10/10`、单一补丁共识 `10/10` 与 `Resolved@1=1` 的同时成立。

LocAgent 与 GraphLocator 也成功解决问题，说明 MAGNET 并非唯一能生成正确行为补丁的方法；MAGNET 的优势在于补丁更直接、候选更稳定、选择共识更强。CoSIL/GALA 的失败则揭示了一个独立于定位质量的瓶颈：模型可以在计划中正确解释根因，却在编辑阶段退化成只修改文档。该现象支持在后续实验中单独报告计划—补丁一致性和运行时候选率，而不能只依赖 `Valid@K`。

## 11. 跨案例归因

### 11.1 成功分布

- 五方法共同成功：1 条，`marked-1825`。
- 四方法成功：1 条，`Chart.js-9766`。
- 三方法成功：4 条，`wp-calypso-33752`、`Chart.js-9027`、`marked-1435`、`p5.js-5555`。
- 单方法独有成功：2 条，GraphLocator 的 `Chart.js-8567` 与 LocAgent 的 `marked-684`。

### 11.2 失败阶段分类

1. **定位阶段失败**：`Chart.js-9027` 的 CoSIL/GraphLocator、`marked-684` 的 CoSIL、`marked-1435` 的 GraphLocator没有进入候选生成。
2. **定位相关但根因不精确**：`Chart.js-8567` 的 LocAgent/CoSIL；它们围绕 tick 密度修补，但未找到 `updateDims` 的状态更新错误。
3. **修复层次错误**：`p5.js-5555` 的 CoSIL/GALA只改文档；`marked-1435` 的 LocAgent在 href 已被截断后再补救。
4. **接口契约破坏**：`wp-calypso-33752` 的 CoSIL改变 children 类型，文本效果接近正确但官方断言失败。
5. **候选选择失败**：`Chart.js-8567` 的 MAGNET/GALA、`Chart.js-9766` 的 LocAgent、定向 `marked-684` 的 MAGNET均存在可解 Oracle 候选但 Selected 未选中。

## 12. 对 MAGNET 的结论与改进方向

MAGNET 的 6 条成功覆盖了 UI 文本、异常防御、stacked scale、GFM tokenizer、link grammar 与 WebGL 默认值，说明它不是只对某一种错误有效。它在 `p5.js-5555` 上表现出极强共识，在 `Chart.js-9766` 上表现出极高候选正确率。

但 MAGNET 的 6 条成功中，除 `p5.js-5555` 的选择票为 10 外，其余 Selected 的票数均为 1。成功往往依赖单个候选，而不是稳定共识。`Chart.js-8567` 和 `marked-684` 又直接暴露了后验可解候选未被选中的问题，因此下一步优先级应是选择器，而不是盲目增加 K。

建议的选择特征：

1. 优先选择改动文件与 `repair_plan.expected_files` 完全一致的候选。
2. 对超出定位计划的额外文件、额外公共 API 修改和大补丁增加惩罚。
3. 检查补丁是否真正修改运行时代码；仅注释/文档修改应在行为缺陷任务中降权。
4. 检查修复点是否位于信息首次丢失或状态首次错误的位置，而不是只在下游增加 fallback。
5. 将同语义候选聚类，按语义簇投票，而不是只按补丁文本哈希投票。
6. 对可快速执行的局部测试、lint、导入检查和补丁契约检查进行在线预筛，但官方测试结果只能保留作后验 Oracle，不能用于在线选择。

## 13. 可用于论文的主要论点

1. **定位信息会改变候选池质量**：`Chart.js-8567` 中 GraphLocator 为 10/10 Oracle 成功，而 LocAgent/CoSIL 为 0/10。
2. **Resolved@1 同时受生成器和选择器影响**：MAGNET 在 `Chart.js-8567` 有 3 个成功候选，LocAgent 在 `Chart.js-9766` 有 2 个成功候选，但最终均记为失败。
3. **最小且根因对齐的补丁更稳定**：MAGNET 在 `p5.js-5555` 的 10 个候选全部收敛到同一行修改，并通过 2434 项相关与回归测试。
4. **不同定位方法具有互补性**：GraphLocator 独有解决 `Chart.js-8567`，LocAgent 独有解决 `marked-684`，五方法成功并集达到 8 条，高于任一单方法的 6 条。
5. **MAGNET 当前优势主要是覆盖，而非全面稳定性**：它取得最高 Resolved@1，但多个成功案例选择票仅为 1，空补丁和编辑应用失败仍占较大比例。
