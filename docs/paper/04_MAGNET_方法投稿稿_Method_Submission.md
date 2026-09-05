# MAGNET 方法正文与方法附录（中文投稿稿）

> 本稿按论文投稿结构组织。第 3 节可直接作为方法正文的中文初稿；方法附录集中给出实现级评分、阈值与边界。文中的图 1 指当前 MAGNET 框架图。

## 3 方法

### 1. 框架概览

给定软件问题实例 \(I\) 及其目标代码仓库 \(R\)，本文研究多模态问题驱动的代码定位：系统需要从仓库中识别最可能参与修改的文件、模块和函数。与仅使用自然语言问题描述的传统设定不同，真实问题还可能包含截图、复现页面、文档、代码引用以及 PR 或 commit 链接。我们将输入与输出统一表示为

$$
\begin{aligned}
I&=\left(x^{\mathrm{text}},\mathcal{U},\mathcal{M}\right),\\
\mathcal{Y}&=\left(\pi_F,\pi_C,\pi_H,\mathcal{P},\mathcal{T}\right),
\end{aligned}
$$

其中，\(x^{\mathrm{text}}\) 为问题文本，\(\mathcal{U}\) 和 \(\mathcal{M}\) 分别为 URL 与图像集合；\(\pi_F\)、\(\pi_C\) 和 \(\pi_H\) 分别表示文件、模块和函数的有序候选，\(\mathcal{P}\) 是交付给后续修复过程的自适应修改集合，\(\mathcal{T}\) 是可审计的证据与工具轨迹。排序列表与修改集合承担不同目标：排序列表保留足够候选以支持 Acc@\(K\) 和 Recall@\(K\) 等评估，而修改集合追求证据支持下的最小充分范围。整个推理过程不访问 gold 定位标签。

如图 1 所示，MAGNET 包含四个阶段。阶段 1 接收由文本、图像和 URL 构成的问题证据。阶段 2 解析不同证据的作用，构造结构化 Issue Sketch，并通过轻量仓库索引建立搜索入口。阶段 3 由动态定位智能体交替执行横向关注点搜索与纵向依赖导航，在源码阅读和流分析的反馈下持续剪枝或扩展候选。阶段 4 检查候选是否能够解释问题中的状态、数据与预期效果，继而生成三级排序和独立的修改闭包。

MAGNET 的关键并非简单增加检索轮数，而是使搜索范围、工具选择、源码读取量和分析深度随证据状态变化。候选审查器将尚未解决的问题转化为下一轮查询；当修改机制得到多种独立证据支持时，系统能够提前停止；当后续搜索引入噪声时，最佳轮次检查点允许恢复到证据质量更高的状态。算法 1 概括了完整过程。

~~~text
算法 1：MAGNET 动态代码定位
输入：问题证据 I，目标仓库 R，动态轮数与分析预算 B
输出：文件排序 π_F，模块排序 π_C，函数排序 π_H，修改集合 P，轨迹 T

1  E <- UnderstandEvidence(I)
2  S_I <- BuildIssueSketch(E)
3  F_s <- SourceFirstPrefilter(R, S_I, B)
4  G_R <- BuildScopedTypedGraph(F_s)
5  (Q_1, C_0, Z_0) <- AgentBootstrap(S_I, G_R)
6  best <- empty
7  for r = 1, ..., B.rounds do
8      H_r <- MultiChannelSearch(Q_r, C_{r-1}, G_R)
9      C_r <- SignalFamilyBeam(H_r, B.read)
10     Z_r^code <- ReadCode(C_r)
11     Z_r^flow <- LayeredTraceFlow(C_r, S_I, Z_r^code)
12     (C_r, missing_r) <- VerifyAndReview(C_r, Z_r^code, Z_r^flow)
13     Q_{r+1} <- BuildNextFrontier(S_I, C_r, missing_r)
14     best <- UpdateBestCheckpoint(best, C_r, Z_r)
15     if ExplainableStop(C_r, Q_{r+1}, Z_r) then break
16  end for
17  (π_C, π_H) <- RestoreEntityRanks(best)
18  π_F <- PrecisionRerank(CrossRoundFrontier(best))
19  P <- BuildModificationClosure(π_F, S_I, Z^code, Z^flow)
20  return (π_F, π_C, π_H, P, T)
~~~

### 2. 问题证据理解与仓库入口

问题证据的价值取决于其在定位过程中的角色，而非是否被输入模型。代码 URL 往往提供导航入口，复现页面能够揭示配置与交互路径，截图可以呈现界面状态或可见异常，但这些证据通常不能单独证明某个文件必须修改。为此，Evidence Understanding Agent 先为每项证据确定解析方式，并分别记录其来源、导航价值与修改先验。远程工具只处理合法的 HTTP(S) 地址；仓库路径和限定符号被保留为本地代码提示，避免将本地引用误当作网页。图像由视觉语言模型提取可见文本、视觉症状、潜在代码层和后续查询。若某项外部证据不可访问，失败状态会进入轨迹，定位过程则继续使用其余证据。

经过角色化解析后，系统将证据压缩为 Issue Sketch：

$$
S_I=
\left(
y,\mathcal{W},\mathcal{C},\mathcal{D},\mathcal{E},\mathcal{N},
\mathcal{R},\mathcal{H}_n,\mathcal{O},\mathcal{A},\mathcal{Z}
\right).
$$

其中，\(y\) 表示任务类型；\(\mathcal{W}\) 表示用户或程序工作流；\(\mathcal{C}\) 表示功能关注点；\(\mathcal{D}\) 表示状态、参数或数据对象；\(\mathcal{E}\) 表示预期行为或可观察效果；\(\mathcal{N}\) 表示显式实体；\(\mathcal{R}\) 表示证据角色；\(\mathcal{H}_n\) 表示导航提示；\(\mathcal{O}\) 表示待验证的流义务；\(\mathcal{A}\) 表示架构类比查询；\(\mathcal{Z}\) 表示尚未被源码证实的实现假设。该表示将“问题说了什么”“证据适合做什么”以及“还需要验证什么”分开保存。代码 URL 因而可以具有较高导航价值和较低修改先验：它可以作为 caller、importer 或 used-by 搜索的种子，但不会自动成为 Top-1 修改目标。

Issue Sketch 由结构化 LLM 结果与确定性规则共同生成。规则用于补充显式标识符、路径、任务类型和领域模式，同时对模型生成的术语执行问题文本落地检查。视觉或语言模型推断出的实现细节首先进入假设集合 \(\mathcal{Z}\)，只有在仓库路径、符号或源码片段中得到印证后，才可显著提高最终排名。对于新增功能，目标调用路径可能尚不存在，系统还会查询仓库中的同类能力，以发现 UI、action、state、service 或 data-layer 的既有实现约定。具体术语接纳规则与阈值见附录 A.1。

随后，MAGNET 使用与实例 base commit 对应的仓库结构快照建立轻量索引，并在本地 checkout 可用时补充快照缺失文件。索引保存文件文本以及轻量抽取的模块、类、函数和方法实体。系统融合词法匹配、显式路径、实体名称、证据种子、领域路径和架构类比等低成本信号，并采用 source-first 策略抑制测试、fixture、示例、生成文件和无关 bundle 对初始候选的干扰。当问题明确要求测试或文档修改时，相应路径惩罚会被放宽。文件和实体检索的实现级评分见附录 A.1。

在预筛选得到的局部文件集合 \(\mathcal{F}_s\) 上，MAGNET 构建文件中心的轻量类型图

$$
G_R=(V_F,E_G,\phi,\omega),\qquad V_F=\mathcal{F}_s,
$$

其中，\(\phi:E_G\rightarrow\mathcal{K}\) 表示边类型，\(\omega(e)\) 表示边权。当前关系覆盖 import、reverse-import、call、reverse-call、render、route、state selection、action dispatch/handle、hook、inherit/implement、override、style、config 和 documentation。模块和函数保存在结构索引中，作为文件节点内部的细粒度实体参与后续排序。该表示借鉴图导航代码定位的基本思想 \cite{locagent}，但它是服务于多语言在线定位的文件中心轻量图，而非完整异构实体图或 compiler-grade code property graph。

局部建图兼顾召回与成本。预筛选避免在超大仓库中一次性构建完整语义图，后续轮次又可把新获得的证据文件设为种子，通过有限多跳扩展补回初始范围之外的依赖。范围预算是可配置参数，不构成方法假设；具体扩展分数和默认预算列于附录 A.2。

### 3. 动态定位智能体

仓库入口建立后，Dynamic Localization Agent 通过工具调用逐轮缩短问题语义与实现位置之间的距离。第 \(r\) 轮状态定义为

$$
X_r=
\left(
S_I,Q_r,C_{r-1},Z_{r-1},B_r
\right),
$$

其中，\(Q_r\) 是当前查询前沿，\(C_{r-1}\) 是上一轮候选，\(Z_{r-1}\) 是已经获得的源码、图关系和流证据，\(B_r\) 是剩余工具与读取预算。控制器从 SearchAnchor、NavigateCode、TraceFlow、ReadCode 和 Stop 中选择动作。SearchAnchor 寻找实体、关注点、状态和效果锚点；NavigateCode 沿依赖图查找调用者、实现者或消费者；TraceFlow 检查状态、参数与效果义务；ReadCode 读取剪枝后的少量候选。LLM 负责生成结构化的工具、模式、查询与停止决策；当模型不可用或输出无法解析时，确定性策略维持基本工具循环。

智能体包含互补的横向与纵向搜索。横向关注点搜索处理 concern scattering：同一功能可能分散在组件、状态管理、工具函数和服务层，即使这些文件之间不存在直接调用关系，也可能共同承担一个业务职责。MAGNET 将工作流、关注点与预期效果分解为多通道查询，并沿 render、state、action、hook、style、config、documentation 和同目录关系扩展。该过程受 concern-aware 定位思想启发 \cite{repolens}，但当前实现是在线、问题引导的扩展，不包含离线概念知识库或 concern clustering。

纵向依赖导航用于区分表面入口与实际实现。从代码 URL、显式符号或强词法锚点出发，智能体沿 import、call、reverse-call、inherit/implement、override、hook 和 type-flow 等关系追踪代码。若证据指向展示层或复现代码，used-by 导航优先寻找调用者与委托实现；若问题描述 API 参数、路由或序列化行为，call 导航优先检查参数下传和后端消费。对于新增功能，智能体先定位架构上相似的已有能力，再从交互层横向扩展并向状态或集成层纵向导航，避免因目标调用边尚不存在而提前终止。

两类导航产生的候选由多信号模型融合。文件 \(f\) 在第 \(r\) 轮的原始分数写为

$$
s_r(f)=
\sum_{k\in\mathcal{K}_s}\alpha_k s_{r,k}(f)
+s_{\mathrm{mem}}(f),
$$

其中，\(\mathcal{K}_s\) 包含词法、实体、路径、关注点、证据角色、程序关系、流关系和架构类比等信号族，\(s_{\mathrm{mem}}\) 保留跨轮证据。架构类比主要服务于召回；若缺少路径、符号、调用或源码证据的独立印证，其正向贡献会受到限制。每个分量及其证据原因均被写入轨迹，以支持后续审查。

直接选取总分 Top-\(K\) 容易让单一高频词法信号耗尽读取预算。MAGNET 因而为不同信号族保留小型 beam，并与全局 beam 合并：

$$
C_r=
\operatorname{Trunc}_{B_r}
\left(
\operatorname{Top}_{b_0}(s_r)
\cup
\bigcup_{k\in\mathcal{K}_s}
\operatorname{Top}_{b_k}(s_{r,k})
\right).
$$

这种分信号前沿与逐步收缩候选空间的思想一致 \cite{cosil}：总分 beam 保留当前最优解，实体、路径、程序图和流证据 beam 则保护尚未充分累积总分但拥有独立依据的候选。ReadCode 随后只读取 \(C_r\) 中受预算约束的文件与查询邻域，减少把长文件完整送入模型所带来的噪声和成本。

源码读取后，候选审查器在现有候选集合中区分 patch target、supporting target、navigation only、reproduction only、test/docs 和 unlikely，并返回置信度、证据片段、匹配实体、反证与缺失证据。审查器不能生成索引中不存在的路径，也不能在解析失败时删除确定性检索结果。只有被源码片段落地或被直接程序关系支持的判断才能获得较强正向调整；未读取候选、纯架构相似项和存在反证的候选受到有界限制。missing evidence 被转化为 \(Q_{r+1}\)，由此形成“检索、阅读、判断、补证据”的闭环。

MAGNET 根据候选间隔、独立证据轴、跨轮稳定性和未解决证据控制推理深度。高置信停止要求领先候选是已读取的实现源码，并同时得到符号或路径证据以及程序关系、流关系或 grounded review 的支持。系统也会在预算耗尽、证据进入平台期、Top 候选稳定且查询前沿不再产生实质信息时停止。每轮状态都会形成检查点，最终采用证据质量最优而非时间上最后的轮次，从而降低后续通用 hub 或架构噪声导致的排名退化。完整置信度函数、停止条件与审查器边界见附录 A.3。

### 4. 流感知验证与定位结果

阶段 4 检查候选代码是否能够解释 Issue Sketch 中的状态、数据和预期效果。每个待验证要求表示为流义务

$$
o_i=(d_i,b_i,k_i),
$$

其中，\(d_i\in\mathcal{D}\) 是状态、参数或配置，\(b_i\in\mathcal{E}\) 是预期行为，\(k_i\) 是应检查的关系类型。例如，UI 交互问题形成 event-to-handler 义务，URL 问题形成 route-parameter 义务，序列化问题形成 public-API-to-backend 义务，类型检查问题形成 symbol-binding 义务。候选 \(f\) 对义务 \(o_i\) 的支持由问题术语、类型图路径、流分析、源码片段和反证共同决定：

$$
v(f,o_i)=
v_{\mathrm{term}}+
v_{\mathrm{graph}}+
v_{\mathrm{flow}}+
v_{\mathrm{source}}-
v_{\mathrm{counter}}.
$$

一般性的同目录或共享关注点关系只负责导航；只有与当前义务相关的直接流和源码证据才能证明修改机制。流分析按成本分层执行：系统先运行轻量 program-flow 与 parameter-closure，在候选规模可控时增加 statement-flow；当高价值义务仍未解决时，再启用有限静态切片、跨过程追踪和 flow-chain。若输入已经包含运行轨迹，RuntimeTraceVerifier 可以核验该证据，但当前系统不会默认主动执行程序收集动态轨迹。因此，本方法受数据流定位与执行证据工作的启发 \cite{arise,daira}，但不声称构建完整语句级 def-use 图或实现主动动态分析。

动态循环结束后，MAGNET 对跨轮候选进行精确率重排。最终证据质量可抽象为

$$
q(f)=
q_{\mathrm{rank}}+
q_{\mathrm{stable}}+
q_{\mathrm{path}}+
q_{\mathrm{source}}+
q_{\mathrm{mechanism}}+
q_{\mathrm{flow}}
-q_{\mathrm{noise}}
-q_{\mathrm{counter}}.
$$

该重排综合基础名次、跨轮稳定性、路径与符号落地、源码阅读、修改机制、直接流、噪声角色和反证。它只调整已经召回的候选顺序，不生成新路径；因此，召回能力与最终精确率仍可分别分析。具体的有界奖励和惩罚见附录 A.3。

为避免固定输出大量文件导致集合 Precision 和 F1 下降，MAGNET 将 Top-\(K\) 排序与修改集合解耦，并把 \(\mathcal{P}\) 建模为受证据约束的职责覆盖。系统从流义务和候选审查中构造必需义务集合 \(\mathcal{O}_{\mathrm{req}}\)。令

$$
a_{of}=
\mathbb{I}
\left[
f\text{ 以已读取源码、直接流或 grounded review 支持义务 }o
\right],
$$

则集合 \(\mathcal{P}\) 的义务覆盖率为

$$
\operatorname{Cov}(\mathcal{P})=
\frac{
\left|
\left\{
o\in\mathcal{O}_{\mathrm{req}}:
\sum_{f\in\mathcal{P}}a_{of}>0
\right\}
\right|
}{
|\mathcal{O}_{\mathrm{req}}|
}.
$$

该目标可概括为一个带规模约束的最小充分集合问题：

$$
\mathcal{P}^{*}
=
\arg\max_{\mathcal{P}\subseteq\mathcal{C}}
\left[
\operatorname{Cov}(\mathcal{P})
-\lambda|\mathcal{P}|
+\mu\,\operatorname{Ground}(\mathcal{P})
\right],
\qquad |\mathcal{P}|\le B_{\mathcal{P}}.
$$

其中，\(\operatorname{Ground}(\mathcal{P})\) 衡量集合中的源码与流证据质量，\(\lambda\) 抑制冗余文件，\(B_{\mathcal{P}}\) 是可配置预算。当前实现不求解全局组合优化，而采用证据最强的源码文件作为根，通过新增义务覆盖增益逐轮扩展，并在每轮后执行 necessity ablation：若移除某个文件不会增加缺失义务，则该文件被视为冗余。闭包在必需义务被覆盖、没有受支持的新扩展或预算耗尽时停止；当证据不足时，系统明确返回 partial 或 incomplete 状态，而不伪造完整性。

最后，MAGNET 输出文件、模块和函数三级定位。文件排序来自跨轮候选重排；模块和函数排序来自最佳轮次中的结构实体、文件上下文、问题语义、流支持和源码审查。函数仅继承有上限的文件分数，以避免同一大文件中的全部函数同时获得高排名；审查器只有识别出结构索引中真实存在的实体名称时，才能提供精确实体奖励。对于没有显式类或模块声明的文件，系统使用 file-module fallback 形成模块级候选。每个结果保留路径、名称、行号、分数、支持证据和推理轨迹，使定位依据与停止原因均可审计。

总体而言，MAGNET 的仓库图是文件中心的轻量类型图，横向关注点搜索是在线问题引导扩展，流分析是多语言静态近似与已有运行证据核验。这些边界分别不同于 LocAgent 的完整异构实体图 \cite{locagent}、RepoLens 的离线概念聚类 \cite{repolens}、ARISE 的语句级 def-use 表示 \cite{arise} 和 DAIRA 的主动运行轨迹采集 \cite{daira}。本文据此将贡献限定为证据角色化、多方向动态导航、流感知验证和自适应修改闭包的协同，而不把尚未实现的编译器级分析作为方法能力。

## 方法附录

### A.1 证据落地与轻量检索细节

**模型生成术语的落地检查。** 设 \(T(q)\) 为模型生成语义项 \(q\) 的有效词集合，\(T(x)\) 为原始问题文本的有效词集合，\(\mathcal{G}\) 为通用软件词集合。当前实现仅在以下条件成立时，将 \(q\) 接纳为可直接参与检索的语义项：

$$
\operatorname{Accept}(q)=
\mathbb{I}\left[
|T(q)\cap T(x)|>0
\land
\left(
(T(q)\cap T(x))\setminus\mathcal{G}\neq\varnothing
\lor q\subseteq x
\right)
\land
\frac{|T(q)\cap T(x)|}{\max(1,|T(q)|)}\ge 0.2
\right].
$$

未满足条件的视觉或语言推断可以作为待验证假设保留，但不作为强排序证据。

**文件词法检索。** 当前日志沿用历史字段名 bm25_score，但实现并非标准 BM25。对于查询词 \(t\)，其权重为

$$
w(t)=1+\min\left(
1,\,
0.25\log_2\max(1,\operatorname{tf}(t,Q))
\right).
$$

文件 \(f\) 的词法分数为

$$
s_{\mathrm{lex}}(f,Q)=
\sum_{t\in T(Q)}w(t)
\left[
8\mathbb{I}(t\in\operatorname{path}(f))
+\min(12,\operatorname{tf}(t,f))
\right]
+5\sum_{q\in Q}\mathbb{I}(q\subseteq f).
$$

该实现强调路径命中、正文词频截断与短语匹配，避免少量高频词无限放大。

**实体检索。** 对类、模块、函数或方法实体 \(h\)，当前评分为

$$
\begin{aligned}
s_{\mathrm{ent}}(h,Q)=&
30\mathbb{I}(\operatorname{name}(h)\in N_Q)
+10\sum_t w(t)\mathbb{I}(t\in\operatorname{name}(h))\\
&+\min\left(
24,\,
4\sum_t w(t)\mathbb{I}(t\in\operatorname{path}(h))
\right)\\
&+\min\left(
36,\,
\sum_t w(t)\min(8,\operatorname{tf}(t,h))
\right),
\end{aligned}
$$

其中，\(N_Q\) 为查询中的显式符号集合。初始范围分数融合文件词法、实体、显式路径、领域路径、架构查询和证据种子：

$$
s_{\mathrm{scope}}(f)=
s_{\mathrm{lex}}(f)
+1.25s_{\mathrm{ent}}(f)
+s_{\mathrm{path}}(f)
+s_{\mathrm{domain}}(f)
+s_{\mathrm{arch}}(f)
+s_{\mathrm{seed}}(f).
$$

**source-first 路径先验。** 令 \(\rho(f)\) 为文件路径角色，\(m_\rho\) 和 \(c_\rho\) 分别为角色乘数与可选上限，则预筛选阶段使用

$$
\widetilde{s}_{\mathrm{scope}}(f)=
\begin{cases}
\min(c_\rho,m_\rho s_{\mathrm{scope}}(f)),
& c_\rho\text{ 已定义},\\
m_\rho s_{\mathrm{scope}}(f),
& \text{其他情况}.
\end{cases}
$$

实现源码获得温和增益，测试、fixture、示例、生成文件、lockfile 和无关 bundle 被降权或设置上限。若任务明确要求测试或文档修改，对应路径角色会被重新解释。

### A.2 范围化图与轮内候选评分

**图扩展。** 从种子文件 \(u\) 扩展到邻接文件 \(v\) 时，当前实现使用

$$
s_G(v\mid u,Q)=
\omega(u,v)
+\min\left(5.4,0.45|T(Q)\cap T(v)|\right)
+0.4\mathbb{I}
\left[
\phi(u,v)\in\mathcal{K}_{\mathrm{sem}}
\right],
$$

其中，\(\mathcal{K}_{\mathrm{sem}}\) 包含 call、render、state selection、type flow 和 inheritance 等语义关系。单次扩展受到种子数、每个种子的边数和 beam 宽度限制。当前深度配置默认在预筛选中保留约 120 个文件，但该数值可由运行参数覆盖。

**轮内路径角色调整。** 对候选原始分数 \(s_r(f)\)，实现源码使用有界加法，噪声角色使用乘法降权：

$$
\widetilde{s}_r(f)=
\begin{cases}
s_r(f)+\min(B_\rho,(m_\rho-1)s_r(f)),
& m_\rho>1,\\
\min(c_\rho,m_\rho s_r(f)),
& m_\rho<1\text{ 且存在 }c_\rho,\\
m_\rho s_r(f),
& \text{其他情况}.
\end{cases}
$$

**候选审查。** 设审查角色先验为 \(\eta(r_f)\)，审查置信度为 \(p_f\)，顺序奖励为 \(o_f\)，其调整写为

$$
\Delta_{\mathrm{review}}(f)=
\operatorname{clip}
\left(
\eta(r_f)p_f+o_f;\,
-B^{-},B^{+}(f)
\right).
$$

正向上限 \(B^{+}(f)\) 依赖源码是否已读取、证据片段是否落地以及修改机制是否得到验证。未读取候选只能获得较小奖励；反证、纯导航角色和证据专用文件可触发负向调整。

### A.3 置信度、停止与跨轮精确率重排

**轮次置信度。** 设前两名分数为 \(s_1\) 与 \(s_2\)，相对间隔为

$$
\delta_r=
\frac{\max(0,s_1-s_2)}
{\max(|s_1|,|s_2|,1)}.
$$

令 \(A_r\) 为 Top-1 已具备的 symbol、path、concern、program、flow、review、source 和 read 证据轴集合，当前预算控制量为

$$
\begin{aligned}
c_r=\min\big(
&0.98,\,
0.12+\min(0.28,0.70\delta_r)+0.055|A_r|\\
&+0.08\mathbb{I}_{\mathrm{source}}
+0.08\mathbb{I}_{\mathrm{symbol}\land\mathrm{read}}
+0.10\mathbb{I}_{\mathrm{review}\land\mathrm{read}}
\big).
\end{aligned}
$$

\(c_r\) 不是统计校准概率，而是控制是否继续消耗分析预算的启发式量。当前高置信停止阈值默认为 0.78，但达到阈值仍不足以单独停止：领先候选还必须是已读取的实现源码，具备直接证据，并满足跨轮稳定或审查确认，同时不存在关键证据缺口。

**跨轮事实。** 设文件 \(f\) 的基础名次为 \(r_f\)，各轮名次为 \(\{r_f^{(t)}\}\)，进入跨轮候选的次数为 \(n_f\)，实现中的基础先验与稳定性项为

$$
p_{\mathrm{rank}}(f)=
1.35\max(0,9-r_f)+\frac{4}{r_f},
$$

$$
p_{\mathrm{stable}}(f)=
\min\left(
5,\,
12\sum_t\frac{1}{8+r_f^{(t)}}
+\min(2,0.25n_f)
\right).
$$

这两项与路径、源码、机制、直接流、噪声角色和反证共同构成正文中的 \(q(f)\)。重排只作用于已召回文件，因此无法弥补完全未进入候选前沿的 gold 文件。

### A.4 修改闭包与实体级恢复

**闭包扩展。** 设 \(\Delta\mathcal{O}_{\mathrm{req}}(f)\) 为加入候选 \(f\) 后新覆盖的必需义务，\(\mathcal{L}_f\) 为问题相关的直接流族，\(E(f,\mathcal{P})\) 为候选与当前集合之间的强类型图边。实现中的扩展效用为

$$
\begin{aligned}
u(f\mid\mathcal{P})=&
5|\Delta\mathcal{O}_{\mathrm{req}}(f)|
+4\mathbb{I}_{\mathrm{mechanism}}\\
&+\min(3,1.2|\mathcal{L}_f|)
+\min\left(
3,\sum_{e\in E(f,\mathcal{P})}\omega(e)
\right)\\
&+u_{\mathrm{review}}+u_{\mathrm{rank}}.
\end{aligned}
$$

普通图邻居若不能覆盖新的必需义务，不会仅因“相关”而进入集合。集合大小由证据和职责数共同决定，而非固定为 Top-15。当前默认闭包预算上限为 6；当完整闭包无法建立时，不完整回退通常选择不超过 4 个强候选。上述数值均属于可配置实现预算，应在消融实验中独立报告。

**模块与函数恢复。** 对结构实体 \(h\)，一般评分形式为

$$
s_{\mathrm{entity}}(h)=
\beta_1s_{\mathrm{entity-search}}(h)
+\beta_2\operatorname{clip}(s_{\mathrm{file}}(f_h))
+s_{\mathrm{semantic}}(h,Q)
+s_{\mathrm{flow}}(h)
+s_{\mathrm{review}}(h).
$$

设函数正文与问题语义原子的匹配集合为 \(M_h\)，当前函数语义增益为

$$
s_{\mathrm{semantic}}(h,Q)=
\min\left(
225,\,
70|M_h|
+5\sum_{t\in M_h}
\min(3,\operatorname{tf}(t,h))
\right).
$$

文件分数的继承受到上限约束；精确实体奖励只应用于结构索引中真实存在且被源码审查识别的名称。文件最终重排不会凭空生成新的模块或函数候选。

### A.5 能力边界与实现映射

| 论文组件 | 当前代码位置 | 能力边界 |
| --- | --- | --- |
| Issue Sketch 与术语落地 | evidence/issue_sketch.py | 词面落地用于限制无依据语义扩展 |
| URL 角色与本地代码提示 | evidence/understanding_agent.py | 本地路径不会发送给远程网页工具 |
| 文件和实体轻量检索 | repo_index/structure_index.py | 历史字段名含 BM25，但当前并非标准 BM25 |
| source-first 与局部范围 | dynamic_retrieval/search_agent.py | 范围大小与路径先验均可配置 |
| 文件中心类型图 | repo_index/typed_graph.py | 不是完整 CPG 或异构实体图 |
| 工具智能体与分信号 beam | dynamic_retrieval/react_agent.py；dynamic_retrieval/tools.py | 借鉴逐步剪枝思想，并非复现 CoSIL |
| 分层流验证 | dynamic_retrieval/tools.py 中的 TraceFlowTool | 多语言静态近似，不默认主动执行 |
| 置信度与最佳轮次 | dynamic_retrieval/search_agent.py | 置信度是预算控制量，不是校准概率 |
| 精确率重排与修改闭包 | dynamic_retrieval/search_agent.py | 排序列表与修改集合独立生成 |
| 模块和函数恢复 | dynamic_retrieval/search_agent.py 中的 _rank_entities | 只对真实结构实体进行排序 |

## 投稿前编辑提示

1. 正文建议保留图 1 和算法 1；附录中的实现常数不必在正文重复。
2. 实验部分应分别消融证据角色化、横向搜索、纵向导航、流验证、最佳轮次检查点和修改闭包，避免只报告整体提升。
3. 若投稿模板对篇幅严格，可将正文第 2 节的证据工具失败处理、第 3 节的 fallback 策略和第 4 节的实体恢复细节进一步移入附录。
4. 引用键需要与最终 BibTeX 对齐；当前保留 locagent、cosil、repolens、arise、daira 等占位键。
5. 在最终英文稿中，应将 engineering heuristic、budget-control score 和 approximate flow analysis 明确表述为实现选择，避免把它们写成具有理论最优性或完备性的算法。
