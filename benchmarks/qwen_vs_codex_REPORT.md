# Codex (GPT-5.4) vs Qwen2.5-7B-RAFT 对比报告
**题库**: 30 题，覆盖 ch1(intro) / ch3(HMM) / ch4(classical ML) / ch4-2(neural) / ch9(LM)
**课程**: `test-slides`（326 chunks, 220 KG nodes）
**Qwen 后端**: AutoDL RTX 5090 + serve_openai.py + SSH tunnel
**Codex 后端**: codex.ysaikeji.cn/v1 (GPT-5.4)
**评分**: GPT-5.4 当 LLM-as-judge，每题给两个回答打 accuracy(0-5) + completeness(0-5)

## 总览
| 指标 | codex (GPT-5.4) | Qwen2.5-7B-RAFT |
|---|---|---|
| 平均响应时长 | 22.49s | 5.01s |
| 中位响应时长 | 11.63s | 4.63s |
| 最长响应时长 | 180.01s | 8.03s |
| 最短响应时长 | 5.27s | 2.00s |
| 平均答案字符数 | 347 | 515 |
| 失败/错误次数 | 1 | 0 |
| backend_fallback 触发数 | — | 0 (qwen→codex 降级) |
| 平均 accuracy 分 (0-5) | 1.97 | 3.30 |
| 平均 completeness 分 (0-5) | 1.83 | 2.97 |

## Head-to-head 判决
- **codex 胜**: 8 题（27%）
- **qwen 胜**: 17 题（57%）
- **平局**: 5 题（17%）

## 逐题打分表
| ID | 类型 | 章节 | codex(s) | qwen(s) | codex acc | qwen acc | codex comp | qwen comp | winner |
|---|---|---|---|---|---|---|---|---|---|
| q01 | factual_zh | ch3 | 6.30 | 4.47 | 5 | 5 | 5 | 5 | tie |
| q02 | factual_zh | ch3 | 8.09 | 5.26 | 0 | 2 | 0 | 4 | b |
| q03 | conceptual_zh | ch3 | 6.23 | 5.72 | 5 | 5 | 5 | 5 | tie |
| q04 | factual_zh | ch3 | 75.90 | 4.17 | 5 | 5 | 5 | 5 | tie |
| q05 | conceptual_zh | ch3 | 180.01 | 5.65 | 0 | 4 | 0 | 3 | b |
| q06 | factual_zh | ch4 | 109.65 | 3.22 | 0 | 5 | 0 | 5 | b |
| q07 | factual_zh | ch4 | 5.27 | 3.98 | 0 | 5 | 0 | 5 | b |
| q08 | conceptual_zh | ch4 | 5.58 | 8.02 | 0 | 5 | 0 | 5 | b |
| q09 | factual_zh | ch4 | 5.93 | 5.25 | 0 | 0 | 0 | 0 | tie |
| q10 | comparison_zh | ch4 | 6.98 | 6.90 | 0 | 3 | 0 | 2 | b |
| q11 | factual_zh | ch4(2) | 10.38 | 3.99 | 0 | 4 | 0 | 3 | b |
| q12 | conceptual_zh | ch4(2) | 16.32 | 4.12 | 0 | 3 | 0 | 2 | b |
| q13 | factual_zh | ch4(2) | 9.20 | 7.82 | 0 | 2 | 0 | 2 | b |
| q14 | conceptual_zh | ch4(2) | 7.17 | 3.89 | 0 | 3 | 0 | 2 | b |
| q15 | factual_zh | ch4(2) | 9.33 | 4.48 | 0 | 2 | 0 | 2 | b |
| q16 | comparison_zh | ch4(2) | 16.69 | 5.54 | 5 | 4 | 5 | 2 | a |
| q17 | factual_zh | ch4(2) | 14.84 | 4.98 | 4 | 2 | 3 | 1 | a |
| q18 | factual_zh | ch9 | 10.86 | 3.93 | 5 | 4 | 5 | 3 | a |
| q19 | conceptual_zh | ch9 | 19.40 | 8.03 | 5 | 3 | 5 | 2 | a |
| q20 | factual_zh | ch9 | 15.82 | 4.34 | 1 | 4 | 1 | 3 | b |
| q21 | factual_zh | ch9 | 10.37 | 6.40 | 2 | 0 | 1 | 1 | a |
| q22 | conceptual_zh | ch9 | 6.36 | 4.52 | 0 | 4 | 0 | 4 | b |
| q23 | factual_zh | ch9 | 17.97 | 5.13 | 0 | 2 | 0 | 2 | b |
| q24 | factual_en | ch3 | 12.40 | 3.64 | 5 | 5 | 5 | 4 | a |
| q25 | factual_en | ch4(2) | 13.96 | 3.66 | 5 | 3 | 4 | 2 | a |
| q26 | factual_en | ch9 | 14.85 | 4.75 | 2 | 2 | 2 | 2 | tie |
| q27 | application_zh | ch3 | 14.47 | 5.77 | 2 | 3 | 2 | 4 | b |
| q28 | application_zh | ch4 | 20.53 | 4.44 | 2 | 4 | 3 | 4 | b |
| q29 | comparison_zh | ch4(2) | 16.84 | 6.30 | 2 | 3 | 2 | 4 | b |
| q30 | course_meta | ch1 | 7.09 | 2.00 | 4 | 3 | 2 | 1 | a |

## 详细案例对比（6 个最有代表性的题）

### q06 · factual_zh · ch4
**题目**: ID3 决策树的特征选择标准是什么？

**裁判**: winner=b, accuracy(codex/qwen)=0/5, completeness(codex/qwen)=0/5
**评语**: 本题是基础机器学习常识题，关键在于是否准确答出ID3使用信息增益，A未作答而B回答正确。

<details><summary>codex 回答 (109.7s, 151 chars)</summary>

```
文档中没有涉及 `ID3` 决策树或其特征选择标准，所以我不能根据这些材料回答该问题。当前提供的资料主要覆盖自然语言处理、`HMM`、`EM`、`Baum-Welch` 和机器翻译等内容。[Source: ch1.pdf, Page 30/75][Source: ch3.pdf, Page 2/51]
```

</details>

<details><summary>qwen 回答 (3.2s, 295 chars)</summary>

```
- Analyze key points: The question asks for the criterion used by ID3 for choosing features. The document provides an explicit statement describing ID3’s selection rule.
- Quote evidence: ##begin_quote##ID3决策树使用信息增益作为特征选择的标准##end_quote##
- Final conclusion: ID3 使用信息增益（information gain）作为特征选择标准。
```

</details>

**judge 对 codex**: 回答A拒绝作答且未给出ID3的正确特征选择标准，事实判断失败。

**judge 对 qwen**: 回答B正确指出ID3以信息增益作为特征选择标准，且结论完整直接。

---

### q07 · factual_zh · ch4
**题目**: 朴素贝叶斯的'朴素'体现在哪个假设上？

**裁判**: winner=b, accuracy(codex/qwen)=0/5, completeness(codex/qwen)=0/5
**评语**: 本题标准答案是朴素贝叶斯假设特征在给定类别下条件独立，B正确作答而A未作答。

<details><summary>codex 回答 (5.3s, 137 chars)</summary>

```
文档里没有提供“朴素贝叶斯的‘朴素’体现在哪个假设上”的具体定义或假设内容；目前只看到目录中出现了“朴素贝叶斯”，没有相关展开说明。[Source: ch4.pdf, Page 55/64]

如果你愿意，可以继续提供包含“朴素贝叶斯”详细内容的页面，我再根据材料准确回答。
```

</details>

<details><summary>qwen 回答 (4.0s, 441 chars)</summary>

```
- Analyze key points: The question asks which assumption makes “朴素” in “朴素贝叶斯”. We should identify the stated assumption tied to the method name.
- Key quote from the document: ##begin_quote##朴素贝叶斯的基本假设是特征之间相互独立。##end_quote##
- Final conclusion: In the document, “朴素” in “朴素贝叶斯” is attributed to the assumption that features are **mutually independent** (i.e., feature independence is the key simplifying assumption underlying the approach).
```

</details>

**judge 对 codex**: 未回答题目核心内容，属于拒答，事实信息为空。

**judge 对 qwen**: 准确指出朴素贝叶斯中的“朴素”来自特征条件独立这一核心假设，回答完整。

---

### q08 · conceptual_zh · ch4
**题目**: 支持向量机 SVM 的核心思想是什么？请简述硬间隔与软间隔的区别。

**裁判**: winner=b, accuracy(codex/qwen)=0/5, completeness(codex/qwen)=0/5
**评语**: 本题中B直接且正确回答了两个关键点，而A因基于材料缺失而未作答。

<details><summary>codex 回答 (5.6s, 142 chars)</summary>

```
抱歉，提供的文档里只有目录提到“支持向量机”，没有展开介绍其核心思想，也没有说明硬间隔与软间隔的区别，因此我不能依据这些材料准确回答。 [Source: ch4.pdf, Page 55/64]

如果你愿意，我可以继续根据你后续提供的 `SVM` 相关课件页帮你整理成一段简明答案。
```

</details>

<details><summary>qwen 回答 (8.0s, 784 chars)</summary>

```
- Analyze the key point: The question asks for (1) SVM core idea and (2) a comparison between hard margin and soft margin.
- Key evidence from the document: ##begin_quote##支持向量机（Support Vector Machine，SVM）
– 核心思想：寻找一个最优的超平面，使得两类样本在该超平面两侧分开，并且
尽量最大化两类样本之间的间隔。##end_quote##
- Key evidence from the document: ##begin_quote##硬间隔（Hard Margin）
– 要求训练样本严格满足线性可分条件，不存在任何误分类样本。
软间隔（Soft Margin）
– 允许存在少量误分类样本，通过引入松弛变量来容忍样本的不完全线性可分。##end_quote##
- Conclusion: SVM’s core idea is to find an optimal hyperplane that separates two classes on opposite sides while maximizing the gap between them; hard margin requires strict linear separability with no misclassified samples, whereas soft margin allows some misclassification by introducing slack variables to tolerate non-linearly separable cases.
```

</details>

**judge 对 codex**: 拒绝作答，未回答SVM核心思想及硬间隔与软间隔区别。

**judge 对 qwen**: 准确概括了SVM最大间隔超平面的核心思想，并完整区分了硬间隔与软间隔。

---

### q05 · conceptual_zh · ch3
**题目**: 前向算法和后向算法的区别是什么？

**裁判**: winner=b, accuracy(codex/qwen)=0/4, completeness(codex/qwen)=0/3
**评语**: 本题关键在于区分前向算法与后向算法的递推方向，并最好补充各自计算对象与应用场景。

<details><summary>codex 回答 (180.0s, 0 chars)</summary>

```
(空)
```

</details>

<details><summary>qwen 回答 (5.6s, 563 chars)</summary>

```
- Key point analysis: The question asks for a contrast; the document provides separate descriptions of forward vs backward recursion, so we should quote both and infer the difference.
- Evidence from the text: ##begin_quote##前向算法（Forward Algorithm）
- 按时间顺序递推计算
- 向前递推##end_quote## and ##begin_quote##后向算法（Backward Algorithm）
- 从后向前递推计算
- 后向递推##end_quote##
- Conclusion: The document distinguishes them by direction of recursion: Forward Algorithm proceeds "按时间顺序递推计算" ("forward recursively"), while Backward Algorithm proceeds "从后向前递推计算" ("backward recursively").
```

</details>

**judge 对 codex**: 空白作答，既没有给出事实内容也没有覆盖题目要求。

**judge 对 qwen**: 正确指出前向与后向算法的递推方向差异，但未进一步说明它们在 HMM 中分别计算的量及用途。

---

### q11 · factual_zh · ch4(2)
**题目**: 卷积神经网络 CNN 中的'卷积'操作具体在做什么？

**裁判**: winner=b, accuracy(codex/qwen)=0/4, completeness(codex/qwen)=0/3
**评语**: A 完全没有回答题目本身，B 虽不够完整但正确概括了 CNN 卷积的核心作用。

<details><summary>codex 回答 (10.4s, 371 chars)</summary>

```
参考文档里没有直接讲解 CNN，也没有给出“卷积”操作的定义或公式，所以我不能依据这些材料准确说明 CNN 中“卷积”具体在做什么。 [Source: ch9.pdf, Page 68/70]

从现有材料看，这些文档主要覆盖：
- 自然语言处理研究内容，其中包括语言模型等方向，但未展开 CNN 卷积机制。 [Source: ch1.pdf, Page 30/75]
- 神经网络中的反向传播、RNN、Transformer 等内容，而非 CNN。 [Source: ch4(2).pdf, Page 5/97]
- 语言模型、预训练语言模型和大规模语言模型。 [Source: ch9.pdf, Page 68/70]

如果你愿意，我可以改为根据你课程里现有这几份资料，帮你梳理“RNN、Transformer 与语言模型”的相关概念。
```

</details>

<details><summary>qwen 回答 (4.0s, 438 chars)</summary>

```
- Analyze key points: The question asks for the role/purpose of convolution in CNNs; we should cite the document’s explicit description of convolution’s function.
- Quote evidence: ##begin_quote##CNN 中的‘卷积’操作实际上是在空间域中对图像进行局部感受（local perception）和特征提取（feature extraction）。##end_quote##
- Final conclusion: In CNNs, convolution performs local perception and feature extraction by applying a local operation across the image’s spatial domain.
```

</details>

**judge 对 codex**: 该回答属于拒答，未说明卷积是用卷积核在局部感受野上滑动并做加权求和以提取特征。

**judge 对 qwen**: 该回答抓住了卷积用于局部感受和特征提取这一核心，但缺少滑动卷积核、权重共享和加权求和等关键机制。

---

### q22 · conceptual_zh · ch9
**题目**: 大规模语言模型的 scaling law 描述的是什么？

**裁判**: winner=b, accuracy(codex/qwen)=0/4, completeness(codex/qwen)=0/4
**评语**: 本题关键是给出scaling law的基本定义，A未作答而B基本答对。

<details><summary>codex 回答 (6.4s, 143 chars)</summary>

```
文档中没有提到“大规模语言模型的 scaling law”的定义或具体描述。已提供材料只说明了大规模语言模型相关内容包括 ChatGPT 的三阶段过程：基础大模型预训练、指令微调和类人对齐，但没有介绍 scaling law。 [Source: ch9.pdf, Page 56/70]
```

</details>

<details><summary>qwen 回答 (4.5s, 444 chars)</summary>

```
- Analyze key points: The question asks for the definition of “scaling law” in this context, which the document provides directly as a statement of what it describes.
- Quote evidence: ##begin_quote##大规模语言模型的 scaling law 描述的是模型规模（如参数量、训练批次大小等）与性能（如准确率、吞吐量等）之间的关系。##end_quote##
- Final conclusion: In these notes, “scaling law” refers to how model scale (e.g., parameters, batch size) relates to performance metrics (e.g., accuracy, throughput).
```

</details>

**judge 对 codex**: 回答A直接否认该概念在材料中出现，属于拒答且未回答题目。

**judge 对 qwen**: 回答B正确概括了scaling law是规模与性能关系，但举例中的训练批次大小、吞吐量不如经典表述常见。

---

