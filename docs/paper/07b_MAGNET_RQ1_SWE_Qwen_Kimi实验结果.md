# MAGNET RQ1：SWE 上的三模型实验结果

## RQ1：MAGNET 能否提高多粒度代码定位性能？

我们在 SWE-bench Multimodal Clean15 上评估 MAGNET 的文件、模块和函数三级定位性能。该数据集从 102 个原始实例中保留 92 个可统一评估的实例。为考察框架对基础模型的敏感性，我们分别采用 Qwen3.5-397B-A17B、Kimi2.6 和 MiMo-v2.5，并与相同模型条件下的 CoSIL、GALA、GraphLocator 和 LocAgent 进行比较。MiMo-v2.5 的最新运行成功完成 89 个实例，另有 3 个实例因服务端 HTTP 500 错误失败；下表遵循预定的 `partial_checkpoint_else_zero` 策略，仍以全部 92 个实例为分母。

表 1 报告宽松排名结果。对于实例 (i) 的真实位置集合 (G_i) 和前 (k) 个预测位置 (P_i^k)，只要二者至少存在一个交集，该实例即被视为命中：

$$
\operatorname{Acc@}k_{\mathrm{relaxed}}
=\frac{1}{N}\sum_{i=1}^{N}
\mathbb{I}\left(P_i^k\cap G_i\neq\varnothing\right).
$$

表 2 报告严格排名结果，仅当前 (k) 个预测完整覆盖全部真实位置时才视为成功：

$$
\operatorname{Acc@}k_{\mathrm{strict}}
=\frac{1}{N}\sum_{i=1}^{N}
\mathbb{I}\left(G_i\subseteq P_i^k\right).
$$

两张表均同时报告集合级完整定位率（SL）和宏平均召回率（REC）。其中，SL 与严格覆盖具有相同的实例级判定，但使用方法的完整返回集合；REC 衡量每个实例中真实位置被覆盖的平均比例。所有数值均为百分比，粗体和下划线分别表示同一基础模型下的最优和次优结果。

<p align="center"><strong>表 1. SWE-bench Multimodal Clean15 上的宽松多粒度定位结果（%，N=92）。Acc@k 只要求 Top-k 命中至少一个真实位置；SL 和 REC 基于完整返回集合。</strong></p>

<div style="overflow-x: auto;">
<table>
  <thead>
    <tr>
      <th rowspan="2">框架</th><th rowspan="2">模型</th>
      <th colspan="5">File (%)</th><th colspan="5">Module (%)</th><th colspan="5">Function (%)</th>
    </tr>
    <tr>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>CoSIL</td><td rowspan="5">Qwen3.5-397B-A17B</td><td>70.65</td><td><u>70.65</u></td><td>70.65</td><td>26.09</td><td>43.21</td><td>52.17</td><td>52.17</td><td>52.17</td><td>23.91</td><td>36.78</td><td>21.74</td><td>25.00</td><td>27.17</td><td>4.35</td><td>12.92</td></tr>
    <tr><td>GALA</td><td><strong>80.43</strong></td><td><strong>80.43</strong></td><td><u>80.43</u></td><td>36.96</td><td>55.29</td><td><strong>65.22</strong></td><td><strong>65.22</strong></td><td><u>65.22</u></td><td><u>34.78</u></td><td>48.61</td><td><u>30.43</u></td><td><u>33.70</u></td><td>36.96</td><td>13.04</td><td>23.48</td></tr>
    <tr><td>GraphLocator</td><td>48.91</td><td>48.91</td><td>50.00</td><td>14.13</td><td>26.27</td><td>39.13</td><td>39.13</td><td>39.13</td><td>19.57</td><td>27.57</td><td>16.30</td><td>16.30</td><td>20.65</td><td>4.35</td><td>11.00</td></tr>
    <tr><td>LocAgent</td><td><strong>80.43</strong></td><td><strong>80.43</strong></td><td><u>80.43</u></td><td><u>42.39</u></td><td><u>58.73</u></td><td>39.13</td><td>46.74</td><td>60.87</td><td><strong>57.61</strong></td><td><strong>67.88</strong></td><td>28.26</td><td>32.61</td><td><u>38.04</u></td><td><strong>30.43</strong></td><td><strong>49.54</strong></td></tr>
    <tr><td><strong>MAGNET</strong></td><td><u>78.26</u></td><td><strong>80.43</strong></td><td><strong>85.87</strong></td><td><strong>48.91</strong></td><td><strong>64.27</strong></td><td><u>55.43</u></td><td><u>61.96</u></td><td><strong>76.09</strong></td><td><u>34.78</u></td><td><u>51.33</u></td><td><strong>39.13</strong></td><td><strong>41.30</strong></td><td><strong>43.48</strong></td><td><u>14.13</u></td><td><u>27.39</u></td></tr>
    <tr><td>CoSIL</td><td rowspan="5">Kimi2.6</td><td><u>78.26</u></td><td><u>78.26</u></td><td>78.26</td><td>32.61</td><td>50.99</td><td><u>64.13</u></td><td><u>64.13</u></td><td>64.13</td><td>35.87</td><td>48.82</td><td>32.61</td><td><u>39.13</u></td><td>40.22</td><td>13.04</td><td>22.59</td></tr>
    <tr><td>GALA</td><td><strong>88.04</strong></td><td><strong>88.04</strong></td><td><strong>88.04</strong></td><td><u>42.39</u></td><td><strong>62.44</strong></td><td><strong>73.91</strong></td><td><strong>73.91</strong></td><td><u>73.91</u></td><td><strong>40.22</strong></td><td><strong>55.34</strong></td><td><u>34.78</u></td><td><strong>41.30</strong></td><td><strong>45.65</strong></td><td><strong>17.39</strong></td><td><strong>28.42</strong></td></tr>
    <tr><td>GraphLocator</td><td>58.70</td><td>58.70</td><td>59.78</td><td>27.17</td><td>39.71</td><td>50.00</td><td>50.00</td><td>50.00</td><td>31.52</td><td>39.02</td><td>28.26</td><td>31.52</td><td>33.70</td><td>14.13</td><td>21.08</td></tr>
    <tr><td>LocAgent</td><td>67.39</td><td>68.48</td><td>68.48</td><td>28.26</td><td>45.04</td><td>45.65</td><td>50.00</td><td>56.52</td><td>35.87</td><td>48.17</td><td>22.83</td><td>26.09</td><td>26.09</td><td>9.78</td><td>18.52</td></tr>
    <tr><td><strong>MAGNET</strong></td><td>66.30</td><td>73.91</td><td><u>82.61</u></td><td><strong>46.74</strong></td><td><u>61.99</u></td><td>57.61</td><td>63.04</td><td><strong>77.17</strong></td><td><u>36.96</u></td><td><u>53.65</u></td><td><strong>36.96</strong></td><td>38.04</td><td><u>42.39</u></td><td><u>15.22</u></td><td><u>26.70</u></td></tr>
    <tr><td>CoSIL</td><td rowspan="5">MiMo-v2.5</td><td>69.57</td><td>69.57</td><td>69.57</td><td>26.09</td><td>43.35</td><td><u>53.26</u></td><td><u>53.26</u></td><td>53.26</td><td>25.00</td><td>37.19</td><td>22.83</td><td>26.09</td><td>30.43</td><td>10.87</td><td>18.89</td></tr>
    <tr><td>GALA</td><td><u>80.43</u></td><td><u>80.43</u></td><td><u>80.43</u></td><td>39.13</td><td><u>57.26</u></td><td><strong>64.13</strong></td><td><strong>64.13</strong></td><td>64.13</td><td><u>34.78</u></td><td><u>48.21</u></td><td><u>29.35</u></td><td><u>31.52</u></td><td><u>38.04</u></td><td><u>13.04</u></td><td><u>22.70</u></td></tr>
    <tr><td>GraphLocator</td><td>34.78</td><td>35.87</td><td>38.04</td><td>11.96</td><td>21.88</td><td>23.91</td><td>23.91</td><td>23.91</td><td>13.04</td><td>17.43</td><td>13.04</td><td>13.04</td><td>14.13</td><td>3.26</td><td>7.18</td></tr>
    <tr><td>LocAgent</td><td><strong>82.61</strong></td><td><strong>83.70</strong></td><td><strong>84.78</strong></td><td><strong>42.39</strong></td><td><strong>59.77</strong></td><td>48.91</td><td>52.17</td><td><u>65.22</u></td><td><strong>55.43</strong></td><td><strong>67.79</strong></td><td><strong>41.30</strong></td><td><strong>44.57</strong></td><td><strong>50.00</strong></td><td><strong>31.52</strong></td><td><strong>52.38</strong></td></tr>
    <tr><td><strong>MAGNET</strong><sup>†</sup></td><td>65.22</td><td>71.74</td><td>76.09</td><td><u>40.22</u></td><td>54.27</td><td>47.83</td><td><u>53.26</u></td><td><strong>67.39</strong></td><td>31.52</td><td>45.80</td><td>28.26</td><td>29.35</td><td>31.52</td><td>11.96</td><td>19.78</td></tr>
  </tbody>
</table>
</div>

<p align="center"><strong>表 2. SWE-bench Multimodal Clean15 上的严格多粒度定位结果（%，N=92）。Acc@k 仅在 Top-k 完整覆盖全部真实位置时记为成功；SL 和 REC 基于完整返回集合。</strong></p>

<div style="overflow-x: auto;">
<table>
  <thead>
    <tr>
      <th rowspan="2">框架</th><th rowspan="2">模型</th>
      <th colspan="5">File (%)</th><th colspan="5">Module (%)</th><th colspan="5">Function (%)</th>
    </tr>
    <tr>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
      <th>Acc@8</th><th>Acc@10</th><th>Acc@15</th><th>SL</th><th>REC</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>CoSIL</td><td rowspan="5">Qwen3.5-397B-A17B</td><td>26.09</td><td>26.09</td><td>26.09</td><td>26.09</td><td>43.21</td><td><u>23.91</u></td><td>23.91</td><td>23.91</td><td>23.91</td><td>36.78</td><td>4.35</td><td>4.35</td><td>4.35</td><td>4.35</td><td>12.92</td></tr>
    <tr><td>GALA</td><td>36.96</td><td>36.96</td><td>36.96</td><td>36.96</td><td>55.29</td><td><strong>34.78</strong></td><td><strong>34.78</strong></td><td><u>34.78</u></td><td><u>34.78</u></td><td>48.61</td><td><strong>11.96</strong></td><td><u>11.96</u></td><td><u>13.04</u></td><td>13.04</td><td>23.48</td></tr>
    <tr><td>GraphLocator</td><td>14.13</td><td>14.13</td><td>14.13</td><td>14.13</td><td>26.27</td><td>19.57</td><td>19.57</td><td>19.57</td><td>19.57</td><td>27.57</td><td>4.35</td><td>4.35</td><td>4.35</td><td>4.35</td><td>11.00</td></tr>
    <tr><td>LocAgent</td><td><strong>40.22</strong></td><td><u>40.22</u></td><td><u>42.39</u></td><td><u>42.39</u></td><td><u>58.73</u></td><td>21.74</td><td><u>27.17</u></td><td><strong>38.04</strong></td><td><strong>57.61</strong></td><td><strong>67.88</strong></td><td><u>10.87</u></td><td>10.87</td><td>11.96</td><td><strong>30.43</strong></td><td><strong>49.54</strong></td></tr>
    <tr><td><strong>MAGNET</strong></td><td><u>39.13</u></td><td><strong>45.65</strong></td><td><strong>48.91</strong></td><td><strong>48.91</strong></td><td><strong>64.27</strong></td><td>21.74</td><td>25.00</td><td><u>34.78</u></td><td><u>34.78</u></td><td><u>51.33</u></td><td>9.78</td><td><strong>13.04</strong></td><td><strong>14.13</strong></td><td><u>14.13</u></td><td><u>27.39</u></td></tr>
    <tr><td>CoSIL</td><td rowspan="5">Kimi2.6</td><td><u>32.61</u></td><td><u>32.61</u></td><td>32.61</td><td>32.61</td><td>50.99</td><td><u>35.87</u></td><td><u>35.87</u></td><td>35.87</td><td>35.87</td><td>48.82</td><td>9.78</td><td>11.96</td><td>13.04</td><td>13.04</td><td>22.59</td></tr>
    <tr><td>GALA</td><td><strong>42.39</strong></td><td><strong>42.39</strong></td><td><u>42.39</u></td><td><u>42.39</u></td><td><strong>62.44</strong></td><td><strong>40.22</strong></td><td><strong>40.22</strong></td><td><strong>40.22</strong></td><td><strong>40.22</strong></td><td><strong>55.34</strong></td><td><u>10.87</u></td><td><strong>15.22</strong></td><td><strong>17.39</strong></td><td><strong>17.39</strong></td><td><strong>28.42</strong></td></tr>
    <tr><td>GraphLocator</td><td>27.17</td><td>27.17</td><td>27.17</td><td>27.17</td><td>39.71</td><td>31.52</td><td>31.52</td><td>31.52</td><td>31.52</td><td>39.02</td><td><strong>11.96</strong></td><td><u>13.04</u></td><td>14.13</td><td>14.13</td><td>21.08</td></tr>
    <tr><td>LocAgent</td><td>27.17</td><td>28.26</td><td>28.26</td><td>28.26</td><td>45.04</td><td>27.17</td><td>30.43</td><td>32.61</td><td>35.87</td><td>48.17</td><td>7.61</td><td>8.70</td><td>8.70</td><td>9.78</td><td>18.52</td></tr>
    <tr><td><strong>MAGNET</strong></td><td>26.09</td><td>29.35</td><td><strong>46.74</strong></td><td><strong>46.74</strong></td><td><u>61.99</u></td><td>26.09</td><td>27.17</td><td><u>36.96</u></td><td><u>36.96</u></td><td><u>53.65</u></td><td>9.78</td><td>11.96</td><td><u>15.22</u></td><td><u>15.22</u></td><td><u>26.70</u></td></tr>
    <tr><td>CoSIL</td><td rowspan="5">MiMo-v2.5</td><td>26.09</td><td>26.09</td><td>26.09</td><td>26.09</td><td>43.35</td><td>25.00</td><td>25.00</td><td>25.00</td><td>25.00</td><td>37.19</td><td>7.61</td><td>7.61</td><td>10.87</td><td>10.87</td><td>18.89</td></tr>
    <tr><td>GALA</td><td><u>39.13</u></td><td><u>39.13</u></td><td>39.13</td><td>39.13</td><td><u>57.26</u></td><td><strong>34.78</strong></td><td><strong>34.78</strong></td><td><u>34.78</u></td><td><u>34.78</u></td><td><u>48.21</u></td><td><u>9.78</u></td><td><u>10.87</u></td><td><u>13.04</u></td><td><u>13.04</u></td><td><u>22.70</u></td></tr>
    <tr><td>GraphLocator</td><td>8.70</td><td>9.78</td><td>11.96</td><td>11.96</td><td>21.88</td><td>13.04</td><td>13.04</td><td>13.04</td><td>13.04</td><td>17.43</td><td>3.26</td><td>3.26</td><td>3.26</td><td>3.26</td><td>7.18</td></tr>
    <tr><td>LocAgent</td><td><strong>41.30</strong></td><td><strong>42.39</strong></td><td><strong>42.39</strong></td><td><strong>42.39</strong></td><td><strong>59.77</strong></td><td><u>30.43</u></td><td><u>33.70</u></td><td><strong>43.48</strong></td><td><strong>55.43</strong></td><td><strong>67.79</strong></td><td><strong>17.39</strong></td><td><strong>18.48</strong></td><td><strong>21.74</strong></td><td><strong>31.52</strong></td><td><strong>52.38</strong></td></tr>
    <tr><td><strong>MAGNET</strong><sup>†</sup></td><td>28.26</td><td>31.52</td><td><u>40.22</u></td><td><u>40.22</u></td><td>54.27</td><td>18.48</td><td>21.74</td><td>31.52</td><td>31.52</td><td>45.80</td><td>7.61</td><td>8.70</td><td>11.96</td><td>11.96</td><td>19.78</td></tr>
  </tbody>
</table>
</div>

<p><sup>†</sup> MiMo-v2.5 运行中有 3 个实例因服务端 HTTP 500 错误未产生可评估结果，按预定策略记为零分。</p>

### 结果分析

在 Qwen3.5-397B-A17B 条件下，MAGNET 的优势主要出现在较深预算和完整覆盖上。其文件级宽松 Acc@15、SL 和 REC 分别达到 85.87%、48.91% 和 64.27%，较对应最强 baseline 分别提高 5.44、6.52 和 5.54 个百分点；模块级宽松 Acc@15 达到 76.09%，提高 10.87 个百分点；函数级宽松 Acc@8、Acc@10 和 Acc@15 分别达到 39.13%、41.30% 和 43.48%，均为该模型组最高。在严格口径下，MAGNET 同样取得最高的文件级 Acc@10/15 和函数级 Acc@10/15。不过，模块和函数级 SL/REC 仍低于 LocAgent，说明 MAGNET 更擅长将关键位置推进至较深候选范围，但细粒度职责的完整恢复仍有提升空间。

在 Kimi2.6 条件下，GALA 在多数宽松早期命中指标上表现最好，而 MAGNET 在深预算完整覆盖上更具优势。MAGNET 的文件级严格 Acc@15 和 SL 均为 46.74%，分别比最强 baseline 高 4.35 个百分点；模块级宽松 Acc@15 达到 77.17%，比 GALA 高 3.26 个百分点。与此同时，GALA 在文件和模块级 Acc@8/10 以及多数函数级指标上领先，表明 MAGNET-Kimi 的主要优势来自后续搜索形成的补充覆盖，而不是初始候选排序。

在 MiMo-v2.5 条件下，最新版 MAGNET 的文件级宽松 Acc@15 达到 76.09%，较旧版运行提高 19.57 个百分点，但仍分别低于 GALA 和 LocAgent 4.34 与 8.69 个百分点。其文件级 SL 为 40.22%，高于 GALA 1.09 个百分点，与 LocAgent 的差距缩小至 2.17 个百分点；模块级宽松 Acc@15 为 67.39%，超过 LocAgent 和 GALA 2.17 与 3.26 个百分点。然而，MAGNET 的模块级 SL/REC 仅为 31.52%/45.80%，函数级 Acc@15 和 REC 仅为 31.52% 和 19.78%，明显低于 LocAgent。这表明新版已修复 MiMo 的大部分结构化控制问题，但全局入口的文件召回和文件内实体的完整恢复仍是主要瓶颈。

**RQ1 回答。** MAGNET 在 Qwen3.5-397B-A17B 上提高了深候选预算下的多粒度定位性能，并在 Kimi2.6 上改善了文件完整覆盖和模块级深预算定位。在 MiMo-v2.5 上，新版已明显缩小文件级差距，并在模块级 Acc@15 上取得最优结果，但函数级召回和多位置完整覆盖仍落后于 LocAgent。总体上，MAGNET 的动态证据搜索对深层候选召回有效，但其收益仍受全局种子质量、基础模型的结构化决策能力以及细粒度图导航能力影响。

## 结果呈现说明

1. 所有比较均限定在相同基础模型内部；不从三个模型中挑选不同指标拼接为一个“MAGNET-best”结果。
2. 宽松 Acc@(k) 表示 Top-(k) 至少命中一个真实位置；严格 Acc@(k) 表示 Top-(k) 覆盖全部真实位置，二者不得混用。
3. SL 和 REC 使用方法完整返回集合计算，与 Acc 的宽松或严格命中定义无关，因此在两张表中保持一致。
4. 当前结果来自每种配置的一次批量运行；Qwen 和 Kimi 完成全部实例，MiMo 的 3 个失败实例按预定策略计分。正文使用描述性比较，不在缺少重复运行方差或显著性检验时声称“显著优于”。正式投稿前应补跑这 3 个实例，并冻结一份 92 例均成功的结果快照。

## 作者核验注（投稿时删除）

1. MAGNET-Qwen 数值来自 `result/007swe_clean15_agent_Qwen3.5-397B-A17B_swe_clean15_max_quality_r5`。
2. MAGNET-Kimi 数值来自 `result/008swe_clean15_agent_Kimi2.6_swe_clean15_top1u`。
3. MAGNET-MiMo 数值来自 `result/swe_clean15_agent_MiMo-v2.5_swe_clean15_top1test`；该运行成功 89 例、失败 3 例，失败样本按预定策略记为零分。
4. Baseline 数值来自用户提供的 `swe-bench multimodel clean` 汇总，仅采用与 MAGNET 相同的三种基础模型。
5. 三组 MAGNET 运行和各 baseline 均以 92 个 Clean15 实例为评价分母。
