# 评价指标

我们在文件、模块和函数三个粒度上评估代码定位性能。对于粒度 \(\ell\in\{\mathrm{file},\mathrm{module},\mathrm{function}\}\)，令 \(\mathcal{D}_{\ell}\) 表示该粒度上的可评价问题集合，\(N_{\ell}=|\mathcal{D}_{\ell}|\) 表示问题数量。对于任意问题 \(i\in\mathcal{D}_{\ell}\)，记 \(G_i^{\ell}\) 为真实相关位置集合，\(\pi_i^{\ell}=(p_{i,1}^{\ell},p_{i,2}^{\ell},\ldots)\) 为方法返回的去重后有序候选列表，\(P_{i,k}^{\ell}=\{p_{i,1}^{\ell},\ldots,p_{i,k}^{\ell}\}\) 为前 \(k\) 个候选构成的集合。所有指标均先在单个问题上计算，再在数据集上进行宏平均，使每个问题具有相同权重。

## 宽松排名指标

我们采用 Acc@\(k\)、Recall@\(k\)、MRR@15 和 MAP@15 评价候选列表的定位与排序质量。与要求覆盖全部真实位置的严格指标不同，本文采用的 Acc@\(k\) 只要求前 \(k\) 个候选中至少包含一个真实位置。其定义为

$$
\mathrm{Acc@}k
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\mathbb{I}
\left[
P_{i,k}^{\ell}\cap G_i^{\ell}\neq\varnothing
\right],
$$

其中，\(\mathbb{I}[\cdot]\) 为示性函数。Acc@\(k\) 衡量方法能否在有限候选范围内找到至少一个有效定位入口，但不能反映多位置问题是否得到充分覆盖。

为评价前 \(k\) 个候选对全部真实位置的覆盖程度，我们进一步采用 Recall@\(k\)：

$$
\mathrm{Recall@}k
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\frac{
\left|P_{i,k}^{\ell}\cap G_i^{\ell}\right|
}{
\left|G_i^{\ell}\right|
}.
$$

与 Acc@\(k\) 相比，Recall@\(k\) 能够区分“仅找到一个正确位置”和“覆盖多个正确位置”两种情况，因而更适合评价涉及多文件、多模块或多函数修改的软件问题。

MRR@15（Mean Reciprocal Rank at 15）用于衡量第一个正确位置在候选列表中出现得有多早。令

$$
r_i^{\ell}
=
\min
\left\{
r\mid 1\le r\le 15,
\ p_{i,r}^{\ell}\in G_i^{\ell}
\right\}
$$

表示问题 \(i\) 的第一个正确候选在前15个位置中的排名；若前15个候选均未命中，则该问题的倒数排名记为0。MRR@15 定义为

$$
\mathrm{MRR@15}
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\begin{cases}
\dfrac{1}{r_i^{\ell}},
& r_i^{\ell}\text{ 存在},\\[6pt]
0,
& \text{其他情况}.
\end{cases}
$$

MRR@15 仅关注首次命中的排名。为进一步衡量多个正确位置在候选列表中的整体排序质量，我们采用 MAP@15（Mean Average Precision at 15）。定义位置 \(r\) 的相关性为

$$
\operatorname{rel}_i^{\ell}(r)
=
\mathbb{I}
\left[
p_{i,r}^{\ell}\in G_i^{\ell}
\right],
$$

并定义截至位置 \(r\) 的精确率为

$$
\operatorname{Prec}_i^{\ell}(r)
=
\frac{1}{r}
\sum_{j=1}^{r}
\operatorname{rel}_i^{\ell}(j).
$$

则单个问题的 AP@15 以及数据集级 MAP@15 分别为

$$
\mathrm{AP}_i\mathrm{@15}
=
\frac{1}{|G_i^{\ell}|}
\sum_{r=1}^{15}
\operatorname{Prec}_i^{\ell}(r)
\operatorname{rel}_i^{\ell}(r),
$$

$$
\mathrm{MAP@15}
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\mathrm{AP}_i\mathrm{@15}.
$$

其中，AP@15 使用该问题的全部真实位置数量 \(|G_i^{\ell}|\) 作为归一化分母。因此，只有被排在前15位的正确位置会对 AP@15 产生贡献。

## 集合级指标

排名指标用于评价候选的相对次序，但下游补丁生成更关心给定候选集合是否覆盖了完成修复所需的全部位置。为此，我们采用集合召回率（REC）和集合定位成功率（SL）评价预测范围的完整性。对于问题 \(i\) 在粒度 \(\ell\) 上的预测集合 \(S_i^{\ell}\)，其 REC 定义为

$$
\mathrm{REC}_i^{\ell}
=
\frac{
\left|S_i^{\ell}\cap G_i^{\ell}\right|
}{
\left|G_i^{\ell}\right|
},
$$

数据集级 REC 为

$$
\mathrm{REC}^{\ell}
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\mathrm{REC}_i^{\ell}.
$$

REC 衡量预测集合平均覆盖了多少真实位置。进一步地，SL 仅在预测集合包含该问题的全部真实位置时记为1：

$$
\mathrm{SL}^{\ell}
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
\mathbb{I}
\left[
G_i^{\ell}\subseteq S_i^{\ell}
\right].
$$

因此，REC 描述部分覆盖程度，而 SL 衡量问题级的完整覆盖比例。对于由排序列表截断得到的集合，我们令 \(S_i^{\ell}=P_{i,k}^{\ell}\)，并在 \(k\in\{8,10,15\}\) 以及 `All` 设置下报告 REC 和 SL，其中 `All` 表示使用结果文件中保留的全部去重候选。需要指出的是，当 \(S_i^{\ell}=P_{i,k}^{\ell}\) 时，REC@\(k\) 与 Recall@\(k\) 在数值上相同；二者分别置于集合完整性分析和排序性能分析中，不作为两项独立证据重复解释。

MAGNET 还输出一个独立于 Top-\(k\) 排名的自适应修改集合 \(\mathcal{P}_i\)。对于该输出，我们令 \(S_i^{\mathrm{file}}=\mathcal{P}_i\)，并采用相同定义计算文件级 REC 和 SL。由于 REC 和 SL 均不惩罚额外候选，报告 `All` 或自适应修改集合结果时，同时给出平均集合大小

$$
\overline{|S^{\ell}|}
=
\frac{1}{N_{\ell}}
\sum_{i\in\mathcal{D}_{\ell}}
|S_i^{\ell}|,
$$

以刻画覆盖能力与候选范围之间的关系。平均集合大小仅作为规模统计量，不作为额外的定位性能指标。

## 评价粒度与统计口径

文件级真实位置由参考补丁实际修改的文件构成。模块级和函数级真实位置通过参考补丁修改行与仓库结构索引中的模块、类、函数或方法范围进行匹配。对于没有显式模块结构的文件，采用文件模块作为模块级回退；对于 CSS、JSON、Markdown、顶层语句、新增代码或无法映射到函数边界的修改，不构造函数级 gold。为避免将 gold 构造的适用性误解释为模型定位失败，文件级评价覆盖全部问题，模块级和函数级指标仅在相应 gold 非空的问题集合 \(\mathcal{D}_{\ell}\) 上计算，并在结果表中同时报告各粒度的有效样本数 \(N_{\ell}\)。

实验默认计算 Acc@1 至 Acc@15 和 Recall@1 至 Recall@15。主文报告 Acc@1、Acc@3、Acc@5、Acc@10、Acc@15，Recall@5、Recall@10、Recall@15，MRR@15 和 MAP@15；完整的 Acc/Recall 曲线可置于补充材料。集合评价报告 REC/SL@8、@10、@15 和 @All，以及自适应修改集合的文件级 REC 和 SL。本文不报告 Strict Acc、集合精确率或集合 F1。

所有超时、接口错误、输出解析失败和空预测均保留在相应评价集合的分母中，并按未命中处理。所有方法使用相同的问题集合、仓库提交、gold 构造程序、路径规范化规则和评价脚本。除基础设施错误触发的请求重试外，每个问题只产生一个最终结果，不从多次运行中选择最优输出。所有指标统一以百分数报告并保留两位小数，表头使用向上箭头标记指标方向，例如 `File Acc@5 ↑`、`Function MAP@15 ↑` 和 `Set SL@15 ↑`。
