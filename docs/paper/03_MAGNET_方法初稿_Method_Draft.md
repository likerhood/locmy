# MAGNET 方法章节中文初稿

> 建议题目：**MAGNET: Multimodal Agentic Graph Navigation with Evidence-Grounded Flow Tracing for Issue Localization**  
> 中文释义：**MAGNET：面向软件问题定位的多模态智能体图导航与证据约束流追踪框架**  
> 文中的引用键是占位符，后续应替换为正式 BibTeX 键。

## 3 方法

### 3.1 框架概览

给定一个软件问题实例 \(I\) 及其目标代码仓库 \(R\)，代码定位的目标是识别最可能需要修改的文件、模块和函数。不同于仅包含自然语言描述的传统设置，真实问题还可能携带截图、复现页面、文档、代码引用以及 PR 或 commit 链接。我们将问题证据表示为

$$
I=\left(x^{\mathrm{text}},\mathcal{U},\mathcal{M}\right),
$$

其中，\(x^{\mathrm{text}}\) 为问题文本，\(\mathcal{U}=\{u_i\}\) 为 URL 集合，\(\mathcal{M}=\{m_j\}\) 为图像集合。仓库 \(R\) 包含文件集合 \(\mathcal{F}\)，以及由源代码抽取得到的模块或类实体集合 \(\mathcal{C}\) 和函数或方法实体集合 \(\mathcal{H}\)。MAGNET 在推理时执行一个受证据约束的序贯定位过程，并输出

$$
\mathcal{Y}=\left(\pi_F,\pi_C,\pi_H,\mathcal{P},\mathcal{T}\right),
$$

其中，\(\pi_F\)、\(\pi_C\) 和 \(\pi_H\) 分别是文件、模块和函数的有序候选列表，\(\mathcal{P}\subseteq\mathcal{F}\) 是交付给后续修复智能体的自适应修改集合，\(\mathcal{T}\) 是工具调用、证据更新、剪枝与停止轨迹。排序列表和修改集合具有不同目标：前者保留候选以支持 Acc@\(K\) 和 Recall@\(K\) 等排序评估，后者追求证据支持下的最小充分修改范围。整个推理过程不访问 gold 定位标签。

如图 1 所示，MAGNET 包含四个阶段。阶段 1 接收文本、图像和 URL 等 Issue Evidence。阶段 2 由 Evidence Understanding Agent 解析各项证据的角色，构造 Issue Sketch，并以低成本方式建立仓库入口。由于这两个阶段共同完成“从问题证据到代码搜索空间”的转换，本文在第 3.2 节统一描述。阶段 3 是 Dynamic Localization Agent，它通过横向关注点搜索、纵向依赖导航、源码阅读和动态剪枝逐轮更新候选。阶段 4 使用问题中的状态、数据和预期效果验证候选代码，随后产生文件、模块和函数三级排序以及独立的修改闭包。

该框架的核心不是简单增加搜索轮数，而是让搜索范围、工具选择、代码读取量和分析深度随证据状态变化。候选审查器可以显式指出缺失证据，并将其转化为下一轮查询；当实现位置已经得到多种独立证据支持时，系统能够提前停止；当后续轮次引入噪声时，最佳轮次检查点允许恢复到证据质量更高的早期状态。算法 1 给出了完整流程。

~~~text
算法 1：MAGNET 动态代码定位
输入：问题证据 I，仓库 R，动态轮数上限 R_d，图范围 L_s，读取预算 B
输出：文件排序 π_F，模块排序 π_C，函数排序 π_H，修改闭包 P，轨迹 T

1  E <- UnderstandEvidence(I)
2  S_I <- BuildIssueSketch(E)
3  F_s <- SourceFirstPrefilter(R, S_I, L_s)
4  G_R <- BuildScopedTypedGraph(F_s)
5  (Q_1, C_0, Z_0) <- AgentBootstrap(S_I, G_R)
6  best <- empty
7  for r = 1, ..., R_d do
8      H_r <- MultiChannelSearch(Q_r, C_{r-1}, G_R)
9      C_r <- SignalFamilyBeam(H_r, B)
10     Z_r^code <- ReadCode(C_r)
11     Z_r^flow <- LayeredTraceFlow(C_r, S_I, Z_r^code)
12     C_r <- VerifyAndReview(C_r, Z_r^code, Z_r^flow)
13     Q_{r+1} <- BuildNextFrontier(S_I, C_r, missing_evidence)
14     best <- UpdateBestCheckpoint(best, C_r)
15     SaveCheckpoint(r, C_r, Q_{r+1}, Z_r)
16     if ExplainableStop(C_r, Q_{r+1}) then break
17  end for
18  (π_C, π_H) <- RestoreEntityRanks(best)
19  π_F <- PrecisionRerank(CrossRoundFrontier(best))
20  P <- BuildModificationClosure(π_F, S_I, Z^code, Z^flow)
21  return (π_F, π_C, π_H, P, T)
~~~

### 3.2 问题证据理解与仓库入口

多模态问题定位基准表明，真实问题通常同时包含语言描述、视觉内容和跨页面上下文，而且视觉证据是否有效取决于其与程序行为的关系 \cite{mmissueloc,omnigirl}。因此，MAGNET 不把全部证据直接拼接成一个检索查询。代码 URL 可能只是导航入口，复现页面能够揭示配置和交互路径但通常不是修改目标，截图可以提示组件或渲染层却不能直接证明某个 CSS 选择器或条件分支存在。Evidence Planning Agent 首先为证据选择解析工具，并分别记录其来源、导航价值和修改先验。远程 URL 工具只接收合法的 HTTP(S) 地址；仓库路径和限定符号被保留为本地代码提示，不会被误送给浏览器工具。图像由 VLM 提取可见文本、视觉症状、可能涉及的代码层和后续查询。若图像或页面不可访问，失败状态会进入轨迹，但定位过程继续使用其余证据。

经过证据解析后，系统将问题压缩为结构化 Issue Sketch：

$$
S_I=
\left(
y,\mathcal{W},\mathcal{C},\mathcal{D},\mathcal{E},\mathcal{N},
\mathcal{R},\mathcal{H}_n,\mathcal{O},\mathcal{A},\mathcal{Z}
\right).
$$

这里，\(y\) 表示任务类型；\(\mathcal{W}\) 表示用户或程序工作流；\(\mathcal{C}\) 表示功能关注点；\(\mathcal{D}\) 表示状态、参数或数据对象；\(\mathcal{E}\) 表示预期行为或可观察效果；\(\mathcal{N}\) 表示显式实体；\(\mathcal{R}\) 表示证据角色；\(\mathcal{H}_n\) 表示导航提示；\(\mathcal{O}\) 表示待验证的流义务；\(\mathcal{A}\) 表示架构类比查询；\(\mathcal{Z}\) 表示尚未被源码证实的实现假设。对于代码 URL，MAGNET 允许较高导航价值与较低修改先验同时存在，因此 URL 指向的文件可以作为 importer、caller 或 used-by 导航种子，但不会自动成为 Top-1 修改目标。

Issue Sketch 主要由结构化 LLM 理解结果构成，确定性规则补充任务类型、显式标识符和领域模式。为了阻止 VLM 或 LLM 引入缺乏问题依据的实现细节，系统对生成术语执行词面落地检查。设 \(T(q)\) 为候选语义项 \(q\) 的有效词集合，\(T(x)\) 为原始问题文本的有效词集合，\(\mathcal{G}\) 为通用软件词集合，则候选语义仅在下式成立时被接纳：

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

视觉推断默认以未验证假设进入 \(\mathcal{Z}\)，只有在后续源码片段中得到支持后才可用于提高最终排名。对于功能请求，目标调用路径可能尚不存在，因此系统还生成“查找同类实现”的架构查询，用于发现相邻功能中的 UI、action、reducer、service 或 data-layer 约定。

Issue Sketch 随后用于建立轻量仓库入口。系统优先加载与基准实例 base commit 对应的仓库结构快照，并在可用时用本地 checkout 补充快照中缺失的文件。结构索引保存文件文本以及轻量抽取的 class、module、function 和 method 实体。当前文件检索使用带路径奖励、词频截断和短语奖励的加权词法匹配。需要说明的是，日志中的 bm25_score 是历史兼容字段名，当前实现并非标准 BM25。对于查询词 \(t\)，其权重为

$$
w(t)=1+\min\left(
1,\,
0.25\log_2\max(1,\operatorname{tf}(t,Q))
\right),
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

实体检索进一步区分精确名称、实体名称、路径和实体正文，其当前评分为

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

其中，\(N_Q\) 为查询中的显式符号集合。基于这些低成本信号，MAGNET 先融合词法、实体、显式路径、领域路径、架构查询和证据种子，得到范围分数

$$
s_{\mathrm{scope}}(f)=
s_{\mathrm{lex}}(f)
+1.25s_{\mathrm{ent}}(f)
+s_{\mathrm{path}}(f)
+s_{\mathrm{domain}}(f)
+s_{\mathrm{arch}}(f)
+s_{\mathrm{seed}}(f).
$$

为避免测试、示例和生成文件凭借重复问题词主导范围选择，系统进一步应用 source-first 策略。令 \(\rho(f)\) 为路径角色，\(m_\rho\) 和 \(c_\rho\) 分别为角色乘数和可选分数上限，则

$$
\widetilde{s}_{\mathrm{scope}}(f)=
\begin{cases}
\min(c_\rho,m_\rho s_{\mathrm{scope}}(f)),
& c_\rho\text{ 已定义},\\
m_\rho s_{\mathrm{scope}}(f),
& \text{其他情况}.
\end{cases}
$$

实现源码获得温和增益；测试、fixture、示例、生成文件、lockfile 和无关 bundle 被降权或设置硬上限。如果问题明确要求修改测试或文档，相应惩罚会被放宽。系统保留前 \(L_s\) 个文件并加入有限的同目录邻居，形成局部范围 \(\mathcal{F}_s\)。当前默认 \(L_s=120\)，但它是预算参数而不是方法中的固定常数。

最后，MAGNET 在 \(\mathcal{F}_s\) 上构建文件中心的轻量类型图

$$
G_R=(V_F,E_G,\phi,\omega),
$$

其中 \(V_F=\mathcal{F}_s\)，\(\phi:E_G\rightarrow\mathcal{K}\) 给出边类型，\(\omega(e)\) 为边权。当前边类型覆盖 import、reverse-import、call、reverse-call、render、route、state selection、action dispatch/handle、hook、inherit/implement、override、style、config 和 documentation 等关系。模块和函数保存在结构索引中，并作为文件节点的细粒度实体参与后续排序。该表示借鉴图导航定位的思想 \cite{locagent}，但当前实现是面向多语言定位的文件中心轻量图，而不是完整的异构实体图或 compiler-grade code property graph。

从种子 \(u\) 扩展到相邻文件 \(v\) 时，图得分为

$$
s_G(v\mid u,Q)=
\omega(u,v)
+\min\left(5.4,0.45|T(Q)\cap T(v)|\right)
+0.4\mathbb{I}
\left[
\phi(u,v)\in\mathcal{K}_{\mathrm{sem}}
\right],
$$

其中，\(\mathcal{K}_{\mathrm{sem}}\) 包含 call、render、state selection、type flow 和 inheritance 等语义关系。单次扩展受到 seed 数、每个 seed 的边数和 beam 宽度限制；后续轮次将新证据文件设为种子，从而形成按需多跳导航。

### 3.3 动态定位智能体

完成仓库入口构建后，MAGNET 使用 Dynamic Localization Agent 逐轮收缩问题与代码之间的语义距离。第 \(r\) 轮的状态表示为

$$
X_r=
\left(
S_I,Q_r,C_{r-1},Z_{r-1},B_r
\right),
$$

其中，\(Q_r\) 是当前查询集合，\(C_{r-1}\) 是上一轮候选，\(Z_{r-1}\) 是已获得的源码和流证据，\(B_r\) 是剩余工具与读取预算。控制器从 SearchAnchor、NavigateCode、TraceFlow、ReadCode 和 Stop 中选择下一动作。SearchAnchor 从实体、关注点、状态和预期效果中寻找首批锚点；NavigateCode 按 concern、call 或 used-by 模式遍历类型图；TraceFlow 检查状态、参数和效果义务；ReadCode 只读取剪枝后的小型候选集合。LLM 控制器生成结构化的 tool、mode、queries 和 stop 决策；当 LLM 不可用或输出无法解析时，确定性策略保证基本工具循环仍可完成。

横向关注点搜索用于处理 concern scattering。真实功能常分散在组件、状态管理、工具函数和服务层，仅沿调用边搜索可能遗漏没有直接调用关系但共同承担同一业务职责的文件。MAGNET 将 \(\mathcal{W}\)、\(\mathcal{C}\) 和 \(\mathcal{E}\) 分解为多通道查询，并沿 render、state、action、hook、style、config、documentation 和 same-directory 等关系扩展。该过程受到 concern-aware 定位思想的启发 \cite{repolens}，但当前实现是在线 issue-guided 扩展，不包含 RepoLens 的离线概念知识库和 concern clustering。

纵向依赖导航用于识别入口的实际实现者和下游消费者。从代码 URL、显式函数或强词法锚点出发，智能体沿 import、call、reverse-call、inherit/implement、override、hook 和 type-flow 等边搜索。若 URL 指向展示层或复现代码，used-by 模式优先寻找调用者、消费者或委托实现；若问题描述 API 参数、路由或序列化行为，call 模式优先追踪参数下传和后端实现。对于功能请求，系统先读取架构上相似的已有能力，再从交互层横向扩展并向 action、state 或 integration 层纵向导航，避免因为新功能尚无调用边而错误终止搜索。

横向与纵向导航产生的候选通过多信号分数融合。文件 \(f\) 在第 \(r\) 轮的原始得分写为

$$
s_r(f)=
\sum_{k\in\mathcal{K}_s}
\alpha_k s_{r,k}(f)
+s_{\mathrm{mem}}(f),
$$

其中，\(\mathcal{K}_s\) 包括词法、关注点、证据角色、效果、显式路径、领域路径、架构路径、实体、程序图和流图等信号。每个分量及其原因被独立记录。架构路径只用于召回：若缺少符号、路径、调用或流证据的独立印证，其分量会被限制在较低上限。轮内 source-first 策略对实现文件使用有界加法，对噪声路径使用乘法降权：

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

仅取总分 Top-\(K\) 会让单一强词法信号耗尽代码读取预算。MAGNET 因而借鉴逐步缩小候选前沿的思想 \cite{cosil}，为各信号族保留独立的小型 beam。设 \(\operatorname{Top}_{b_k}(s_k)\) 为信号 \(k\) 的前 \(b_k\) 个候选，则本轮读取集合为

$$
C_r=
\operatorname{Trunc}_{B}
\left(
\operatorname{Top}_{b_0}(s_r)
\cup
\bigcup_{k\in\mathcal{K}_s}
\operatorname{Top}_{b_k}(s_{r,k})
\right).
$$

该策略保留一个总分 beam，同时为实体、路径、关注点、调用图和流证据预留名额。因此，总分暂时不高但拥有独立程序证据的候选不会在源码阅读前被过早删除。ReadCode 随后只处理 \(C_r\) 中的前若干文件，当前实现对每个文件至多提取两个查询词邻域片段和有限数量的实体边界。

读取源码后，候选审查器在已有文件集合内区分 patch target、supporting target、navigation only、reproduction only、test/docs 和 unlikely，并输出置信度、代码证据、匹配实体、反证和缺失证据。它不能生成索引中不存在的路径，解析失败时也不能删除确定性检索结果。令候选角色先验为 \(\eta(r_f)\)，审查置信度为 \(p_f\)，顺序奖励为 \(o_f\)，则其有界调整可写为

$$
\Delta_{\mathrm{review}}(f)=
\operatorname{clip}
\left(
\eta(r_f)p_f+o_f;\,
-B^{-},B^{+}(f)
\right).
$$

正向上限 \(B^{+}(f)\) 取决于候选是否被源码片段落地以及修改机制是否已验证。未读取或未落地候选只能获得很小奖励，反证则引入额外惩罚。审查器报告的 missing evidence 会进入 \(Q_{r+1}\)，从而形成“观察、判断、补证据”的闭环。

MAGNET 使用相对排名间隔和独立证据轴控制分析深度与停止。设前两名分数为 \(s_1\) 和 \(s_2\)，则

$$
\delta_r=
\frac{\max(0,s_1-s_2)}
{\max(|s_1|,|s_2|,1)}.
$$

令 \(A_r\) 为 Top-1 已具备的 symbol、path、concern、program、flow、review、source 和 read 证据轴集合，当前置信度为

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

\(c_r\) 不是校准概率，而是预算控制指标。高置信早停要求 Top-1 是已读取的实现源码，具备符号、路径或 grounded review 证据，并同时得到程序关系、流证据或审查器停止判断的支持；此外，候选需要跨轮稳定或由审查器明确确认，且不存在关键证据缺口。当前默认阈值为 0.78。系统也会在达到最大轮数、证据连续进入平台期、Top-3 稳定且无实质新查询或查询前沿为空时停止。由于更多轮次可能引入通用 hub 或架构噪声，MAGNET 保存每轮检查点，并默认恢复证据质量最高的轮次，而不是无条件采用最后一轮。

### 3.4 流感知验证与定位结果

阶段 4 首先判断候选代码能否解释问题中的状态、数据和预期效果。Issue Sketch 将这些要求表示为流义务

$$
o_i=(d_i,b_i,k_i),
$$

其中，\(d_i\in\mathcal{D}\) 是状态、参数或配置，\(b_i\in\mathcal{E}\) 是预期行为，\(k_i\) 是需要检查的关系类型。例如，UI 交互问题形成 event-to-handler 义务，URL 错误形成 route-parameter 义务，序列化问题形成 public-API-to-backend 义务，类型检查问题形成 symbol-binding 义务。对于候选 \(f\)，验证器汇总

$$
v(f,o_i)=
v_{\mathrm{term}}
+v_{\mathrm{graph}}
+v_{\mathrm{flow}}
+v_{\mathrm{source}}
-v_{\mathrm{counter}},
$$

其中各项分别表示状态或效果术语、类型图路径、流后端、源码片段和反证。一般性的同目录或共享关注点关系只用于导航，只有与当前问题义务相关的直接流才能用于证明修改机制。

流分析采用按需分层策略。系统默认先运行低成本 program-flow 和 parameter-closure；当候选规模受控时增加 statement-flow；当置信度较低且仍有未解决的高价值流义务时，再启用 static-slice、有限跨过程流和 flow-chain。若输入证据本身包含运行轨迹，RuntimeTraceVerifier 可以核验这些已有观察。该设计受到数据流工具和执行证据工作的启发 \cite{arise,daira}，但当前实现是面向多语言定位的轻量近似，不声称构造完整语句级 def-use 图，也不会默认主动执行程序收集动态轨迹。验证器同时返回已覆盖义务和缺失义务，后者会触发下一轮补充搜索。

动态循环结束后，MAGNET 使用跨轮事实证据提高文件排序精确率。由于不同仓库和信号通道的原始分数尺度不同，最终重排依赖候选名次、跨轮稳定性和源码证据。设文件 \(f\) 的基础名次为 \(r_f\)，各轮名次为 \(\{r_f^{(t)}\}\)，其基础先验与稳定性项为

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
\right),
$$

其中，\(n_f\) 是文件进入跨轮候选的次数。最终证据质量概括为

$$
q(f)=
p_{\mathrm{rank}}
+p_{\mathrm{stable}}
+p_{\mathrm{path}}
+p_{\mathrm{read}}
+p_{\mathrm{ground}}
+p_{\mathrm{mechanism}}
+p_{\mathrm{direct}}
-p_{\mathrm{noise}}
-p_{\mathrm{counter}}.
$$

精确路径、路径语义、源码读取、证据引用、实体支持和机制验证产生正向增益；仅由架构路径命中、未验证的 selector hub、证据专用文件和反证产生负向调整。该过程只重排已召回候选，不生成新路径。

为避免固定输出 15 个文件导致集合 Precision 和 F1 下降，MAGNET 将排序候选与修改集合分离，并将最终集合建模为受证据约束的职责覆盖。系统从 Issue Sketch、流证据和候选审查中构造义务集合 \(\mathcal{O}\)，其中至少包含根修改机制，并可包含交互层、状态或集成层以及得到流证据支持的职责。concern 和 expected effect 可以帮助解释候选，但不会仅凭词面重叠强制增加文件。令

$$
a_{of}=
\mathbb{I}
\left[
f\text{ 以已读取源码、直接流或 grounded review 支持义务 }o
\right],
$$

则修改集合 \(\mathcal{P}\) 的必需义务覆盖率为

$$
\operatorname{Cov}(\mathcal{P})=
\frac{
\left|
\left\{
o\in\mathcal{O}_{\mathrm{req}}:
\sum_{f\in\mathcal{P}}a_{of}>0
\right\}
\right|
}
{|\mathcal{O}_{\mathrm{req}}|}.
$$

闭包从证据最强且已经读取的源码文件开始，每轮只加入能够覆盖当前缺失义务的文件。候选 \(f\) 相对于已选集合的扩展效用可写为

$$
\begin{aligned}
u(f\mid\mathcal{P})=&
5|\Delta\mathcal{O}_{\mathrm{req}}(f)|
+4\mathbb{I}_{\mathrm{mechanism}}\\
&+\min(3,1.2|\mathcal{L}_f|)
+\min\left(
3,\sum_{e\in E(f,\mathcal{P})}\omega(e)
\right)\\
&+u_{\mathrm{review}}+u_{\mathrm{rank}},
\end{aligned}
$$

其中，\(\mathcal{L}_f\) 是与问题相关的直接流族，\(E(f,\mathcal{P})\) 是候选与已选集合之间的强类型图边。没有新增必需义务覆盖的普通图邻居不会仅因“相关”而进入修改集合。每轮扩展后，系统执行 necessity ablation：若移除某个文件前后缺失义务集合不变，则该文件被判定为冗余并删除。闭包在义务完整覆盖、没有受支持的新扩展或达到预算时停止。若完整闭包无法建立，系统返回 partial 或 incomplete 状态，而不是伪造完整性。集合预算默认上限为 6；不完整回退根据强候选数、任务类型和职责数自适应决定大小，通常不超过 4。

最后，MAGNET 输出文件、模块和函数三级定位。模块和函数排序基于最佳轮次中的文件候选、实体检索、问题语义、流支持和源码审查。对于实体 \(h\)，其一般形式为

$$
s_{\mathrm{entity}}(h)=
\beta_1s_{\mathrm{entity-search}}(h)
+\beta_2\operatorname{clip}(s_{\mathrm{file}}(f_h))
+s_{\mathrm{semantic}}(h,Q)
+s_{\mathrm{flow}}(h)
+s_{\mathrm{review}}(h).
$$

函数只继承有上限的文件分数，避免同一大文件中的所有函数获得相同高分。设函数正文与问题语义原子的匹配集合为 \(M_h\)，当前语义增益为

$$
s_{\mathrm{semantic}}(h,Q)=
\min\left(
225,\,
70|M_h|
+5\sum_{t\in M_h}
\min(3,\operatorname{tf}(t,h))
\right).
$$

候选审查器只有在源码中识别到结构索引内真实存在的实体名称时，才增加精确实体奖励；未知实体被忽略。对于没有显式 class 或 module 的文件，系统使用 file-module fallback 提供模块级结果。最终结果为每个文件或实体保留路径、名称、行号、分数、支持证据和推理轨迹，从而使排序结果和停止原因均可审计。

需要强调的是，MAGNET 当前的仓库图是文件中心的轻量类型图，横向关注点搜索是在线扩展，流分析是多语言静态近似及已有运行证据核验。这些设计分别不同于 LocAgent 的完整异构实体图 \cite{locagent}、RepoLens 的离线概念聚类 \cite{repolens}、ARISE 的完整语句级 def-use 表示 \cite{arise} 和 DAIRA 的主动运行时轨迹采集 \cite{daira}。这些边界限定了本文应提出的能力主张，也为后续接入 tree-sitter、SCIP、CodeQL 或受控执行后端提供了扩展方向。

---

## 作者校验注（投稿时删除）

| 论文表述 | 当前代码依据 | 边界 |
| --- | --- | --- |
| Issue Sketch 与 0.2 落地阈值 | evidence/issue_sketch.py | 与实现一致 |
| URL 角色和本地代码提示 | evidence/understanding_agent.py | 本地路径不会发送给远程工具 |
| 文件与实体词法公式 | repo_index/structure_index.py | 不是标准 BM25 |
| source-first 与局部范围 | dynamic_retrieval/search_agent.py | 默认范围 120，可配置 |
| 文件中心类型图与扩展公式 | repo_index/typed_graph.py | 不是完整 CPG |
| 四工具 Agent 与分信号 beam | dynamic_retrieval/react_agent.py、dynamic_retrieval/tools.py | CoSIL-inspired，不是算法复现 |
| 分层流验证 | dynamic_retrieval/tools.py 中的 TraceFlowTool | 不默认主动执行程序 |
| 置信度、早停和最佳轮次 | dynamic_retrieval/search_agent.py | 置信度是控制量，不是校准概率 |
| 精确率重排和修改闭包 | dynamic_retrieval/search_agent.py | Top-K 排序与修改集合分离 |
| 模块和函数排序 | dynamic_retrieval/search_agent.py 中的 _rank_entities | 文件精确率重排后不伪造新实体 |
