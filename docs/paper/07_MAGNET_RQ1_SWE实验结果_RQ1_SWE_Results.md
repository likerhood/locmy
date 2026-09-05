# MAGNET 实验结果初稿：RQ1（SWE）

> 本文档整理 SWE 数据集上的 RQ1 主结果。主表使用 Qwen3.5-397B-A17B；Kimi2.6 结果作为跨模型稳健性证据，放入 RQ3 或补充材料。

## RQ1：MAGNET 能否提高多粒度代码定位性能？

我们首先研究 MAGNET 在多粒度代码定位任务上的总体有效性。实验使用 SWE-bench Multimodal Clean15 数据集，其中原始 102 个实例经 Clean15 规则筛选后保留 92 个。为控制基础模型差异，CoSIL、GALA、GraphLocator、LocAgent 和 MAGNET 均使用 Qwen3.5-397B-A17B。参考 LocAgent 的多粒度结果组织方式 \cite{locagent}，表 1 在文件、模块和函数三个粒度统一报告 Acc@10 和 Acc@15。这六项指标均在固定候选预算下计算，可避免不同方法输出集合大小不同带来的混杂。Strict Acc@\(k\) 与集合级 SL、Recall、Precision、F1 在补充材料中作为一个完整指标族报告，不在主表中单独抽取有利列。

Acc@\(k\) 采用宽松命中定义：只要前 \(k\) 个候选中包含至少一个真实修改位置，该实例即记为命中。主表选用宽松口径，是因为它直接衡量排名列表能否为后续分析提供至少一个正确入口；全部真实位置的覆盖能力则由补充材料中的 Strict Acc、SL 和 Recall 衡量。所有指标均先按实例计算再进行宏平均；超时、解析失败和空预测均保留在分母中。

<p align="center"><strong>表 1. SWE 数据集上的多粒度代码定位结果（%）。所有方法使用 Qwen3.5-397B-A17B，N=92。Acc@k 采用宽松命中定义。粗体和下划线分别表示每列最优和次优结果，所有指标均为越高越好。</strong></p>

<div style="overflow-x: auto;">
<table>
  <thead>
    <tr>
      <th rowspan="2">方法</th>
      <th colspan="2">File (%)</th>
      <th colspan="2">Module (%)</th>
      <th colspan="2">Function (%)</th>
    </tr>
    <tr>
      <th>Acc@10</th><th>Acc@15</th>
      <th>Acc@10</th><th>Acc@15</th>
      <th>Acc@10</th><th>Acc@15</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>CoSIL</td>
      <td><u>70.65</u></td><td>70.65</td>
      <td>52.17</td><td>52.17</td>
      <td>25.00</td><td>27.17</td>
    </tr>
    <tr>
      <td>GALA</td>
      <td><strong>80.43</strong></td><td><u>80.43</u></td>
      <td><strong>65.22</strong></td><td><u>65.22</u></td>
      <td><u>33.70</u></td><td>36.96</td>
    </tr>
    <tr>
      <td>GraphLocator</td>
      <td>48.91</td><td>50.00</td>
      <td>39.13</td><td>39.13</td>
      <td>16.30</td><td>20.65</td>
    </tr>
    <tr>
      <td>LocAgent</td>
      <td><strong>80.43</strong></td><td><u>80.43</u></td>
      <td>46.74</td><td>60.87</td>
      <td>32.61</td><td><u>38.04</u></td>
    </tr>
    <tr>
      <td><strong>MAGNET</strong></td>
      <td><strong>80.43</strong></td><td><strong>85.87</strong></td>
      <td><u>61.96</u></td><td><strong>76.09</strong></td>
      <td><strong>41.30</strong></td><td><strong>43.48</strong></td>
    </tr>
  </tbody>
</table>
</div>

表 1 显示，MAGNET 在 6 项主指标中取得 4 项单独最优和 1 项并列最优，即 83.3% 的主表指标达到最优或并列最优；若只计单独最优，该比例为 66.7%。文件级结果最为均衡：MAGNET 的 Acc@10 为 80.43%，与 GALA 和 LocAgent 并列最优；当候选预算扩大到 15 时，Acc@15 达到 85.87%，较最强 baseline 提高 5.44 个百分点。

在模块级定位上，MAGNET 呈现出较明显的深候选优势。其 Acc@10 为 61.96%，低于 GALA 的 65.22%，但 Acc@15 达到 76.09%，较最佳 baseline 提高 10.87 个百分点，说明部分正确模块仍位于候选列表的第 11 至 15 位。这一结果表明 MAGNET 提高了相关模块的深度召回，但模块级候选前移仍有改进空间。

函数级结果进一步验证了 MAGNET 的细粒度搜索能力。MAGNET 的 Acc@10 和 Acc@15 分别为 41.30% 和 43.48%，较对应的最强 baseline 分别提高 7.60 和 5.44 个百分点。因此，MAGNET 在文件修改范围的整体覆盖之外，也能够将至少一个关键细粒度实体送入有限候选范围。补充材料同时表明，MAGNET 的模块和函数级完整集合覆盖仍落后 LocAgent，因而这一结果支持“细粒度命中改善”，但不支持“完整恢复全部细粒度修改职责”。

总体而言，MAGNET 的主要优势集中在文件级 Acc@15、模块级 Acc@15 以及函数级 Acc@10/15，说明证据理解、多通道搜索和流感知验证有助于在较深候选预算中保留真实修改实体。MAGNET 未在模块级 Acc@10 上超过 GALA，而补充材料中的模块和函数级 SL/Recall 仍落后于 LocAgent，表明后续需要进一步加强候选前移、跨文件职责闭包和函数间调用链恢复。预测集合的 SL、Recall、Precision、F1 和平均长度应成组放入补充材料，用于排除单纯扩大候选集带来的覆盖收益。

**RQ1 回答。** 在相同 Qwen3.5-397B-A17B 和固定候选预算下，MAGNET 在六项多粒度排名指标中有五项达到最优或并列最优，其改善主要出现在较深的候选预算以及函数级定位上。结果支持 MAGNET 对多跳候选发现和细粒度命中的有效性，但模块级前排序与模块/函数级完整职责覆盖仍是当前方法的主要改进方向。

## 结果呈现与材料分配

| 结果材料 | 作用 | 建议位置 |
| --- | --- | --- |
| 表 1：三级 Acc@10/15 | 在固定预算下支撑 RQ1 的核心结论 | 正文 |
| Acc@1–9、MRR@15、MAP@15 | 分析前排排序质量 | 附录 |
| Strict Acc@1–15 | 分析完整覆盖随预算的变化 | 附录 |
| 三级 SL、Recall、Precision、F1 及平均集合长度 | 界定完整职责覆盖与候选扩张 | 附录，并在正文中引用 |
| Kimi2.6 下的 MAGNET 结果 | 验证方法对基础模型的敏感性 | RQ3/补充材料 |
| 每实例结果与典型失败案例 | 支撑误差分析与可复现性 | 补充材料 |
| 运行时间、token 与工具调用次数 | 支撑效率分析 | 独立效率 RQ |

## 作者校验注（投稿时删除）

1. MAGNET 数值来自 `result/007swe_clean15_agent_Qwen3.5-397B-A17B_swe_clean15_max_quality_r5` 的单次完整运行：92 个成功、0 个 partial、0 个 failure。
2. Baseline 数值来自用户提供的 `swe-bench multimodel clean` 汇总，主表只使用 Qwen3.5-397B-A17B 行中的“宽松排名指标” Acc@10/15。
3. 主表不使用 baseline 的 `Set Metrics @All`，因为不同方法的原生输出长度可能不同。补充材料应同时报告 SL、Recall、Precision、F1 和平均集合长度，或重算统一 `@15` 口径。
4. 表中只使用宽松 Acc@\(k\)，不混用 Strict Acc@\(k\)。严格口径应在补充材料中独立成表。
5. 所有 MAGNET 数值来自同一次运行，不从不同版本逐列挑选最优值。当前各方法只有一次汇总结果，因此正文不使用“统计显著”等推断性表述。
6. Kimi2.6 结果不与 Qwen 结果按列拼接。在同骨干的 8 列候选展示方案（三级 Acc@10/15 加文件级 SL/Recall）中，Kimi2.6 版 MAGNET 只在文件 SL 和模块 Acc@15 上超过同模型 baseline，因而更适合用于 RQ3 的模型敏感性分析，而非 RQ1 主结果。
7. “83.3% 达到最优或并列最优”不应改写为“83.3% 单独最优”；MAGNET 的单独最优比例为 4/6=66.7%。
