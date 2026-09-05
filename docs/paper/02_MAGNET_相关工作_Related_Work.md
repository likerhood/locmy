# MAGNET 相关工作（中文修订稿）

## 写作结构规划（投稿时删除）

本节按研究问题而非论文时间线组织。每个小节先概括共同范式，再比较代表方法采用的技术机制，最后总结该研究分支尚未解决的共同问题。正文不在每个小节重复介绍本文方法，而仅在末尾用一个综合段落界定研究空白。

| 小节 | 讨论范围 | 主要文献 | 需要揭示的共同边界 |
| --- | --- | --- | --- |
| 2.1 LLM驱动的仓库级问题修复与定位 | 从传统缺陷定位到基于LLM的端到端issue resolution | BugLocator、Ye等、SWE-bench、SWE-agent、AutoCodeRover、Agentless、MASAI、CodeR、OpenHands、SpecRover | 定位通常作为补丁生成的内部步骤，端到端通过率难以解释多粒度定位质量 |
| 2.2 智能体搜索与图结构导航 | 用工具、程序关系、概念知识或因果结构控制仓库探索 | RepoGraph、OrcaLoca、LocAgent、CoSIL、RepoLens、GraphLocator | 结构可达性、语义相关性与真实修改职责并不等价，搜索仍易受入口偏差和图噪声影响 |
| 2.3 多模态证据与程序行为分析 | 利用图像、跨语言问题、静态数据流和运行轨迹补充文本描述 | SWE-bench Multimodal、OmniGIRL、MM-IssueLoc、ARISE、DAIRA | 多模态证据角色与程序行为证据通常分开建模，且静态、动态分析均有环境和语言边界 |

## 2 相关工作

### 2.1 LLM驱动的仓库级问题修复与定位

问题定位旨在从自然语言缺陷报告或功能请求中识别需要检查和修改的程序实体。传统方法主要将问题报告与源代码建模为信息检索对象。BugLocator结合修正的向量空间模型与相似历史报告对文件进行排序 \cite{zhou2012buglocator}；Ye等进一步融合方法结构、API描述、缺陷修复历史和代码变更历史，并通过learning-to-rank学习不同信号的权重 \cite{ye2014learning}。这类方法具有较低的推理成本和稳定的排序过程，但其效果依赖可观察的词汇重合及历史数据。当问题仅描述外部症状、跨越多个业务职责，或者目标实现尚不存在时，静态相似度难以建立从需求语义到根因代码的多步联系。

SWE-bench将真实GitHub issue、仓库快照和测试补丁组织为可执行的仓库级问题修复任务，推动研究从文件推荐转向端到端issue resolution \cite{jimenez2024swebench}。围绕该任务形成的方法大致分为迭代式智能体和阶段化流水线。SWE-agent通过专门设计的Agent-Computer Interface支持仓库导航、文件编辑与测试执行 \cite{yang2024sweagent}；AutoCodeRover在抽象语法树上的类和方法级搜索中结合测试驱动的故障定位 \cite{zhang2024autocoderover}；OpenHands则提供命令行、代码编辑、网页浏览和多智能体协作等通用交互基础设施 \cite{wang2025openhands}。这些系统具有较强的环境交互能力，但较长的自由探索轨迹也可能累积无关上下文、重复工具调用和早期搜索偏差。

另一类工作通过显式阶段或角色分工约束修复过程。Agentless将任务分解为定位、修复和补丁验证，避免让模型自主决定全部工具轨迹 \cite{xia2025agentless}；MASAI为不同子任务配置具有独立目标和策略的子智能体，以缩短单一智能体的上下文链 \cite{arora2024masai}；CodeR使用多智能体和预定义任务图组织问题分析、补丁生成与验证 \cite{chen2024coder}。SpecRover在AutoCodeRover的迭代搜索基础上加入代码意图和规格推断，并由审查智能体检查生成补丁 \cite{ruan2025specrover}。这些设计提高了流程的可控性，但定位仍主要服务于后续补丁生成。以测试通过率衡量的端到端结果会同时受到定位、补丁合成和测试充分性的影响，因而不能直接说明系统是否找全了文件、模块和函数级修改位置，也难以区分“定位正确但补丁失败”和“补丁偶然通过但定位证据不足”两类情况。

### 2.2 智能体搜索与图结构导航

仓库规模扩大后，智能体必须在有限上下文内选择搜索入口、扩展方向和代码读取范围。通用工具调用能够灵活探索仓库，但搜索顺序通常由语言模型即时决定，容易在高频符号、公共工具文件或错误入口附近反复徘徊。OrcaLoca通过优先级调度、动作分解、相关性评分和距离感知的上下文剪枝约束定位动作 \cite{yu2025orcaloca}；RepoGraph则以可插拔的仓库级代码图为修复系统提供结构化导航信息 \cite{ouyang2025repograph}。前者侧重智能体的搜索策略，后者侧重仓库表示，二者均表明仅扩大上下文窗口并不能替代对探索路径的显式控制。

图引导定位进一步把文件、类、函数及其依赖关系纳入推理过程。LocAgent将仓库解析为包含import、call和inheritance等关系的有向异构图，并支持语言模型在实体间执行多跳导航 \cite{chen2025locagent}。CoSIL采用由粗到细的动态图搜索：先在模块调用图上进行文件级广度探索，再展开函数调用图开展迭代搜索，并使用剪枝器和反思机制控制搜索方向 \cite{jiang2025cosil}。这类方法减少了整仓库文本直接进入上下文的需求，也提高了跨文件依赖发现能力。然而，程序图中的可达节点未必承担当前issue的修改职责；反向调用、高连接度枢纽和同目录邻居都可能扩大候选集。对于新增功能，目标调用边可能尚不存在，纯依赖导航还可能偏向已有入口而遗漏需要创建或扩展的实现位置。

除程序依赖外，近期研究开始显式建模业务关注点和问题因果结构。RepoLens针对concern tangling和concern scattering，在离线阶段构建仓库概念知识库，并在在线阶段检索和排序与当前问题相关的关注点 \cite{wang2025repolens}。GraphLocator将问题分解为子问题，并以causal issue graph连接子问题及其代码实体，用于缓解表面症状与根因之间以及单一描述与多个修改位置之间的不匹配 \cite{liu2025graphlocator}。概念知识可以补充调用图无法表示的跨模块语义，因果分解则有助于发现多位置修改，但二者也引入新的依赖：离线关注点质量受到仓库抽象准确性的影响，而动态子问题及因果边仍需语言模型从有限证据中推断。现有方法因此往往在结构精确性、语义覆盖和在线搜索成本之间进行取舍，尚难同时保证高召回、低冗余和可核验的修改职责。

### 2.3 多模态证据与程序行为分析

真实issue中的关键信息可能出现在截图、错误对话框、渲染结果、复现页面和外部链接中。SWE-bench Multimodal将仓库级修复扩展到包含视觉元素的JavaScript软件问题，表明在文本主导的Python基准上有效的系统不能自然泛化到视觉软件领域 \cite{yang2025swebenchmm}。OmniGIRL进一步覆盖Python、Java、JavaScript和TypeScript，并纳入图像及多领域issue，揭示了现有模型在跨语言和图像相关任务上的性能限制 \cite{guo2025omnigirl}。MM-IssueLoc则将视觉证据从补丁生成中分离出来，提供text-only/with-image配对设置、图像类别和相关性标注，以及文件和函数级gold位置，从而直接评估图像是否改善代码定位 \cite{zhan2026mmissueloc}。

这些基准说明“包含图像”并不等于“有效使用图像”。截图可能提供组件名称、错误文本或状态变化，也可能只是现象展示；代码URL可以是修改目标，也可以仅是复现入口或调用方。若系统将所有视觉描述和网页内容无差别拼接进查询，弱相关证据可能放大通用UI术语并干扰代码检索。另一方面，现有多模态基准大多以最终修复或固定Top-\(k\)命中进行评估，对证据的导航价值、修改先验和反证作用缺少统一表示。因此，多模态定位不仅需要视觉理解，还需要判断不同证据在搜索过程中的功能角色。

程序行为分析为从可观察症状追踪根因提供了另一类证据。ARISE将仓库图扩展到语句级节点和过程内definition-use边，并将数据流切片封装为智能体可调用工具 \cite{seddik2026arise}；DAIRA将轻量运行时追踪嵌入问题修复循环，把调用路径、变量状态和状态转换整理为结构化报告 \cite{liu2026daira}。静态数据流能够分析未执行路径并提供稳定的依赖关系，但精细图构建通常受编程语言、框架语义和分析成本限制；动态轨迹反映具体执行中的真实行为，却依赖可运行环境、能够触发故障的测试以及实际覆盖到的路径。二者均能提高根因分析的证据密度，但尚未自然解决视觉现象、URL导航线索与多层代码职责之间的对应关系。

综上，现有研究分别推进了LLM仓库级问题修复、智能体搜索控制、程序图导航、多模态评估以及静态或动态行为分析。然而，这些能力通常被放置在相对独立的阶段：多模态内容被转换为提示上下文，图结构负责候选扩展，数据流或执行反馈用于后期修复验证。仍然缺少一种统一的定位过程，能够先区分异构问题证据的角色，再联合业务关注点与程序依赖开展受控搜索，并以源码和流证据核验候选，最终同时给出可审计的文件、模块和函数排序及边界明确的修改集合。MAGNET针对这一尚未充分覆盖的交叉问题展开研究。

## 参考文献与链接

1. `zhou2012buglocator` Jian Zhou, Hongyu Zhang, and David Lo. **Where Should the Bugs Be Fixed? More Accurate Information Retrieval-Based Bug Localization Based on Bug Reports.** ICSE 2012, pp. 14–24. [DOI](https://doi.org/10.1109/ICSE.2012.6227210)
2. `ye2014learning` Xin Ye, Razvan C. Bunescu, and Chang Liu. **Learning to Rank Relevant Files for Bug Reports Using Domain Knowledge.** FSE 2014, pp. 689–699. [DOI](https://doi.org/10.1145/2635868.2635874)
3. `jimenez2024swebench` Carlos E. Jimenez, John Yang, Alexander Wettig, Shunyu Yao, Kexin Pei, Ofir Press, and Karthik Narasimhan. **SWE-bench: Can Language Models Resolve Real-World GitHub Issues?** ICLR 2024. [ICLR Proceedings](https://proceedings.iclr.cc/paper_files/paper/2024/hash/edac78c3e300629acfe6cbe9ca88fb84-Abstract-Conference.html)
4. `yang2024sweagent` John Yang, Carlos E. Jimenez, Alexander Wettig, Kilian Lieret, Shunyu Yao, Karthik Narasimhan, and Ofir Press. **SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering.** NeurIPS 2024. [NeurIPS Proceedings](https://papers.neurips.cc/paper_files/paper/2024/hash/5a7c947568c1b1328ccc5230172e1e7c-Abstract-Conference.html)
5. `zhang2024autocoderover` Yuntong Zhang, Haifeng Ruan, Zhiyu Fan, and Abhik Roychoudhury. **AutoCodeRover: Autonomous Program Improvement.** ISSTA 2024, pp. 1592–1604. [DOI](https://doi.org/10.1145/3650212.3680384) | [arXiv](https://arxiv.org/abs/2404.05427)
6. `xia2025agentless` Chunqiu Steven Xia, Yinlin Deng, Soren Dunn, and Lingming Zhang. **Demystifying LLM-Based Software Engineering Agents.** Proceedings of the ACM on Software Engineering, 2(FSE), Article FSE037, 2025. [DOI](https://doi.org/10.1145/3715754)
7. `arora2024masai` Daman Arora, Atharv Sonwane, Nalin Wadhwa, Abhav Mehrotra, Saiteja Utpala, Ramakrishna Bairi, Aditya Kanade, and Nagarajan Natarajan. **MASAI: Modular Architecture for Software-Engineering AI Agents.** arXiv preprint arXiv:2406.11638, 2024. [arXiv](https://arxiv.org/abs/2406.11638)
8. `chen2024coder` Dong Chen, Shaoxin Lin, Muhan Zeng, et al. **CodeR: Issue Resolving with Multi-Agent and Task Graphs.** arXiv preprint arXiv:2406.01304, 2024. [arXiv](https://arxiv.org/abs/2406.01304)
9. `wang2025openhands` Xingyao Wang, Boxuan Li, Yufan Song, et al. **OpenHands: An Open Platform for AI Software Developers as Generalist Agents.** ICLR 2025. [OpenReview](https://openreview.net/forum?id=OJd3ayDD0F) | [arXiv](https://arxiv.org/abs/2407.16741)
10. `ruan2025specrover` Haifeng Ruan, Yuntong Zhang, and Abhik Roychoudhury. **SpecRover: Code Intent Extraction via LLMs.** ICSE 2025. [DOI](https://doi.org/10.1109/ICSE55347.2025.00080) | [arXiv](https://arxiv.org/abs/2408.02232)
11. `yu2025orcaloca` Zhongming Yu, Hejia Zhang, Yujie Zhao, Hanxian Huang, Matrix Yao, Ke Ding, and Jishen Zhao. **OrcaLoca: An LLM Agent Framework for Software Issue Localization.** ICML 2025, PMLR 267:73416–73436. [PMLR](https://proceedings.mlr.press/v267/yu25x.html)
12. `ouyang2025repograph` Siru Ouyang, Wenhao Yu, Kaixin Ma, et al. **RepoGraph: Enhancing AI Software Engineering with Repository-Level Code Graph.** ICLR 2025. [OpenReview](https://openreview.net/forum?id=dw9VUsSHGB) | [arXiv](https://arxiv.org/abs/2410.14684)
13. `chen2025locagent` Zhaoling Chen, Robert Tang, Gangda Deng, Fang Wu, Jialong Wu, Zhiwei Jiang, Viktor Prasanna, Arman Cohan, and Xingyao Wang. **LocAgent: Graph-Guided LLM Agents for Code Localization.** ACL 2025, pp. 8697–8727. [ACL Anthology](https://aclanthology.org/2025.acl-long.426/) | [DOI](https://doi.org/10.18653/v1/2025.acl-long.426)
14. `jiang2025cosil` Zhonghao Jiang, Xiaoxue Ren, Meng Yan, Wei Jiang, Yong Li, and Zhongxin Liu. **Issue Localization via LLM-Driven Iterative Code Graph Searching.** ASE 2025. [DOI](https://doi.org/10.1109/ASE63991.2025.00249) | [arXiv](https://arxiv.org/abs/2503.22424)
15. `wang2025repolens` Ying Wang, Wenjun Mao, Chong Wang, Zhenhao Zhou, Yicheng Zhou, Wenyun Zhao, Yiling Lou, and Xin Peng. **Extracting Conceptual Knowledge to Locate Software Issues.** arXiv preprint arXiv:2509.21427, 2025. [arXiv](https://arxiv.org/abs/2509.21427)
16. `liu2025graphlocator` Wei Liu, Chao Peng, Pengfei Gao, Aofan Liu, Wei Zhang, Haiyan Zhao, and Zhi Jin. **GraphLocator: Graph-Guided Causal Reasoning for Issue Localization.** arXiv preprint arXiv:2512.22469, 2025. [arXiv](https://arxiv.org/abs/2512.22469)
17. `yang2025swebenchmm` John Yang, Carlos E. Jimenez, Alex L. Zhang, et al. **SWE-bench Multimodal: Do AI Systems Generalize to Visual Software Domains?** ICLR 2025. [OpenReview](https://openreview.net/forum?id=riTiq3i21b) | [arXiv](https://arxiv.org/abs/2410.03859)
18. `guo2025omnigirl` Lianghong Guo, Wei Tao, Runhan Jiang, et al. **OmniGIRL: A Multilingual and Multimodal Benchmark for GitHub Issue Resolution.** Proceedings of the ACM on Software Engineering, 2(ISSTA), Article ISSTA002, 2025. [DOI](https://doi.org/10.1145/3728871) | [arXiv](https://arxiv.org/abs/2505.04606)
19. `zhan2026mmissueloc` Shaoxiong Zhan, Shi Hu, Boyu Feng, et al. **MM-IssueLoc: A Controlled Benchmark for Evaluating Visual Evidence in Multimodal Repository-Level Issue Localization.** arXiv preprint arXiv:2607.15205, 2026. [arXiv](https://arxiv.org/abs/2607.15205)
20. `seddik2026arise` Shahd Seddik, Fahd Seddik, Amirrezza Esmaeili, Mahdieh Sadatbenis, and Fatemeh Fard. **ARISE: A Repository-Level Graph Representation and Toolset for Agentic Program Repair and Fault Localization.** arXiv preprint arXiv:2605.03117, 2026. [arXiv](https://arxiv.org/abs/2605.03117)
21. `liu2026daira` Mingwei Liu, Zihao Wang, Zhenxi Chen, Zheng Pei, Yanlin Wang, and Zibin Zheng. **Dynamic Analysis Enhances Issue Resolution.** arXiv preprint arXiv:2603.22048, 2026. [arXiv](https://arxiv.org/abs/2603.22048)

## 作者校验注（投稿时删除）

- 文献及发表状态核验日期为2026-09-03。正式投稿前应再次检查2025–2026年预印本的标题、作者顺序和发表状态。
- RepoLens、GraphLocator、MM-IssueLoc、ARISE和DAIRA当前按arXiv预印本引用；MASAI和CodeR在本稿中也保守地按arXiv版本记录。
- 本地`008_ARISE_V2.pdf`实际为DAIRA内容，不作为独立文献重复引用。
- 本地GALA仓库目前没有对应的可核验论文或arXiv记录，因此仍未写入相关工作参考文献。若后续获得正式论文，应优先加入2.3节并说明其视觉证据到代码图对齐机制。
- 本稿对既有工作的局限均限定在任务设定、证据来源或技术机制上，不据此推断其在统一实验条件下的性能优劣。
