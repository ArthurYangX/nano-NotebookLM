# Benchmark Report — 4 路线回答风格对照

**数据集**：`benchmarks/questions_100.json` — 100 题来自 5 份 NLP 课件 （ch1 绪论 / ch3 HMM / ch4 经典 ML / ch4(2) 深度学习 / ch9 语言模型）；按题型分布 51 概念 + 29 公式 + 20 计算。

**评测路线**（共 4 条）：
- **GPT-bare (GPT-5.5, no RAG)** — 已答 99/100
- **GPT-RAGKG (GPT-5.5 + graphrag/RAG)** — 已答 99/100
- **Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** — 已答 26/100
- **Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** — 已答 20/100

**说明**：本报告**不做 LLM-judge 定量评分**（裁判 bias + codex 当日积分已耗尽两重原因），改为 (1) 量化风格指标 (2) 5 题 4 路线并排样例的定性观察。

## 1. 量化风格指标

| 路线 | n | 均长（字） | 中位长 | p90 长 | cite 率 | CoT preamble 率 | 分点率 | LaTeX 率 | 中英混合率 | 均延迟(ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| GPT-bare (GPT-5.5, no RAG) | 99 | 472.9 | 459 | 803 | 0% | 0% | 77% | 75% | 87% | 49105.0 |
| GPT-RAGKG (GPT-5.5 + graphrag/RAG) | 99 | 356.7 | 315 | 600 | 96% | 0% | 55% | 42% | 98% | 46138.0 |
| Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG) | 26 | 401.9 | 418.5 | 640 | 100% | 0% | 65% | 42% | 100% | 58925.0 |
| Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG) | 20 | 560.0 | 549.5 | 783 | 100% | 0% | 85% | 25% | 100% | 102740.0 |

**指标定义**：
- **cite 率**：答案含 `[Source: …]` / `[来源: …]` 引用标记的比例
- **CoT preamble 率**：含「先分析问题要点 / 引用原文关键内容 / 给出最终结论 / 首先 / 综上」任一套话
- **分点率**：含 ≥2 个 `- ` / `1. ` / `①②③` 等列表标记
- **LaTeX 率**：含 ≥2 个 LaTeX 命令或数学符号 token（`\sigma`/`softmax`/`_t`/`^T`/...）
- **中英混合率**：答案同时含中文 + ≥3 个英文词

## 2. 五题 4 路线并排样例

### Q001（ch1 · concept）

**题目**：用 2-3 句话说明自然语言处理（NLP）的目标，并区分 NLU 与 NLG。

**参考答案**：

> NLP 目标是研究让计算机理解、处理和生成自然语言的方法，实现人与计算机之间用自然语言进行有效交流。NLU 关注理解自然语言意义，NLG 关注把意图或思想组织成自然语言文本。

#### GPT-bare (GPT-5.5, no RAG)

🌐 mixed · 📏 138 字 · ⏱ 18147ms

> 自然语言处理（NLP）的目标是让计算机能够对人类语言进行处理、理解、分析和生成，从而实现人与机器之间更自然的交互。  
> 自然语言理解（NLU）侧重于“读懂”语言，例如意图识别、情感分析、语义解析；自然语言生成（NLG）侧重于“表达”语言，例如自动摘要、机器翻译、对话回复生成。

#### GPT-RAGKG (GPT-5.5 + graphrag/RAG)

📎 cite · 🌐 mixed · 📏 212 字 · ⏱ 16181ms

> 自然语言处理（NLP）的目标是让计算机理解和运用人类自然语言，实现人机之间的自然语言通信，并完成查询资料、解答问题、摘录文献等语言信息处理任务。[Source: ch1.pdf, Page 73/75]  
> NLU（自然语言理解）侧重把输入的文字或语音理解为语义；NLG（自然语言生成）侧重根据语义或任务目标生成自然语言文本，二者是任何 NLP 系统的两个基本过程。[Source: ch1.pdf, Page 19/75]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.542 · `ch1.pdf` loc=`Page 38/75` score=0.501 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.501 · `ch3.pdf` loc=`Page 49/51` score=0.483 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · …(+12 more)

#### Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)

📎 cite · 🌐 mixed · 📏 219 字 · ⏱ 18884ms

> 自然语言处理（NLP）的目标是让计算机模拟人的语言交际过程，理解并运用人类自然语言，实现人机之间的自然语言通信，并完成查询资料、解答问题、摘录文献等语言信息处理任务。[Source: ch1.pdf, Page 73/75]
>
> NLU（自然语言理解）侧重把输入的文字或语音理解成语义；NLG（自然语言生成）侧重根据语义或任务结果生成目标语言文本，例如问答答案、摘要或机器翻译结果。[Source: ch1.pdf, Page 19/75]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.542 · `ch1.pdf` loc=`Page 38/75` score=0.501 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.501 · `ch3.pdf` loc=`Page 49/51` score=0.483 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · …(+12 more)

#### Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)

📎 cite · 🌐 mixed · 📏 249 字 · ⏱ 81758ms

> 自然语言处理（NLP）的目标是让计算机模拟人的语言交际过程，能够理解和运用人类自然语言，从而实现人机之间的自然语言通信，并完成查询资料、解答问题、摘录文献等语言信息处理任务。[Source: ch1.pdf, Page 73/75]  
>
> NLU（自然语言理解）侧重把输入的文字或语音转化为计算机可处理的语义，例如问答系统、信息检索、机器翻译中的理解环节；NLG（自然语言生成）侧重根据语义或任务需求生成目标语言语句、答案或摘要等文本输出。[Source: ch1.pdf, Page 19/75]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.542 · `ch1.pdf` loc=`Page 38/75` score=0.501 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.501 · `ch3.pdf` loc=`Page 49/51` score=0.483 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · …(+12 more)

---

### Q008（ch1 · concept）

**题目**：列举至少 5 个 NLP 应用领域，并各给出一个典型任务。

**参考答案**：

> 例：教育-自动判卷/自动答疑；医疗-聊天机器人/辅助系统；商务-呼叫中心/投诉分类；政务-自动咨询/投诉分析；公共设施-天气播报/餐饮查询；还可包括司法、金融、搜索等。

#### GPT-bare (GPT-5.5, no RAG)

• 8 bullets · 📏 223 字 · ⏱ 22066ms

> - **机器翻译**：将中文句子自动翻译成英文。
> - **信息检索**：根据用户查询返回相关网页或文档。
> - **情感分析**：判断商品评论是正面、负面还是中性。
> - **问答系统**：根据问题自动生成或抽取答案。
> - **文本分类**：将新闻自动分类为体育、财经、科技等类别。
> - **信息抽取**：从文本中抽取人名、地点、时间等实体。
> - **对话系统**：实现智能客服与用户的多轮对话。
> - **文本摘要**：自动生成一篇长文档的简短摘要。

#### GPT-RAGKG (GPT-5.5 + graphrag/RAG)

📎 cite · 🌐 mixed · 📏 583 字 · ⏱ 42934ms

> | NLP 应用领域 | 典型任务 |
> |---|---|
> | 文化教育 | 问答系统：回答学习者提出的问题。自然语言处理可用于文化教育领域，问答系统是其主要研究任务之一。[Source: ch1.pdf, Page 38/75][Source: ch1.pdf, Page 19/75] |
> | 医疗 | 信息检索：从相关文档集合中检索所需信息。自然语言处理可用于医疗领域，信息检索是其主要研究任务之一。[Source: ch1.pdf, Page 38/75][Source: ch1.pdf, Page 19/75] |
> | 商务 | 机器翻译：把源语言句子转换为目标语言句子。自然语言处理可用于商务领域，机器翻译是其主要研究任务之一。[Source: ch1.pdf, Page 38/75][Source: ch1.pdf, Page 19/75] |
> | 政务 | 自动文摘：从原始文档生成摘要。自然语言处理可用于政务领域，自动文摘是其主要研究任务之一。[Source: ch1.pdf, Page 38/75][Source: ch1.pdf, Page 19/75] |
> | 社交网络 | 舆情分析、观点挖掘、新事件发现，或微博数据挖掘。课件明确把这些列为社交网络中的 NLP 应用任务。[Source: ch1.pdf, Page 38/75] |

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.511 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.495 · `ch1.pdf` loc=`Page 73/75` score=0.476 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+11 more)

#### Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)

📎 cite · 🌐 mixed · 📏 420 字 · ⏱ 37895ms

> 课件列出的应用领域包括文化教育、医疗、商务、政务、公共设施、内容安全、移动计算、社交网络等；课件也说明 NLP 可用于查询资料、解答问题、摘录文献、汇编资料等自然语言信息处理任务。[Source: ch1.pdf, Page 38/75] [Source: ch1.pdf, Page 73/75]
>
> | NLP 应用领域 | 典型任务举例 |
> |---|---|
> | 文化教育 | 自动解答学生问题 / 问答系统 |
> | 医疗 | 医疗问答或病历文本信息摘录 |
> | 商务 | 客户咨询问答或资料查询 |
> | 政务 | 政务信息查询与自动答复 |
> | 内容安全 | 文本内容审核、风险信息识别 |
> | 社交网络 | 微博数据挖掘、舆情分析、观点挖掘或新事件发现 |
>
> 其中，社交网络中的“微博数据挖掘、社交网络数据分析、舆情分析、观点挖掘、新事件发现”等任务是课件明确列出的例子。[Source: ch1.pdf, Page 38/75]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.511 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.495 · `ch1.pdf` loc=`Page 73/75` score=0.476 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+11 more)

#### Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)

📎 cite · • 6 bullets · 🌐 mixed · 📏 727 字 · ⏱ 96186ms

> 下面列举 5 个以上 NLP 应用领域，并配一个资料中提到的典型任务类型：
>
> - **文化教育**：问答系统，用于回答学习者提出的问题；资料将“文化教育”列为应用领域，并把“问答系统”列为 NLP 主要研究内容之一。[Source: ch1.pdf, Page 38/75] [Source: ch1.pdf, Page 19/75]
> - **医疗**：信息检索，用于从相关文档集合中检索需要的资料；资料列出“医疗”为应用领域，并提到“信息检索”任务。[Source: ch1.pdf, Page 38/75] [Source: ch1.pdf, Page 19/75]
> - **商务**：机器翻译，用于把源语言句子转换为目标语言句子；资料列出“商务”为应用领域，并将“机器翻译”列为主要研究内容。[Source: ch1.pdf, Page 38/75] [Source: ch1.pdf, Page 19/75]
> - **政务**：自动文摘，用于把原始文档压缩成摘要；资料列出“政务”为应用领域，并提到“自动文摘”。[Source: ch1.pdf, Page 38/75] [Source: ch1.pdf, Page 19/75]
> - **内容安全**：文本理解，用于分析文本语义；资料指出 NLP 系统离不开“文本理解”和“文本生成”，并列出“内容安全”为应用领域。[Source: ch1.pdf, Page 19/75] [Source: ch1.pdf, Page 38/75]
> - **社交网络**：舆情分析、观点挖掘、新事件发现等；资料在“社交网络”应用下明确列出了这些任务。[Source: ch1.pdf, Page 38/75]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.511 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.495 · `ch1.pdf` loc=`Page 73/75` score=0.476 · `ch9.pdf` loc=`Page 39/70` score=0.0014 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+11 more)

---

### Q017（ch3 · formula）

**题目**：马尔科夫模型的状态转移概率矩阵 `A=(a_ij)` 需要满足哪两个约束？

**参考答案**：

> `a_ij >= 0`；对任意状态 `i`，`sum_j a_ij = 1`。

#### GPT-bare (GPT-5.5, no RAG)

• 2 bullets · 𝑓 6 latex · 🌐 mixed · 📏 207 字 · ⏱ 16664ms

> 马尔科夫模型的状态转移概率矩阵 \(A=(a_{ij})\) 需要满足两个约束：
>
> 1. **非负性**：  
>    \[
>    a_{ij} \ge 0
>    \]
>    表示从状态 \(i\) 转移到状态 \(j\) 的概率不能为负。
>
> 2. **归一化**：  
>    \[
>    \sum_{j} a_{ij} = 1
>    \]
>    即从任一状态 \(i\) 出发，转移到所有可能状态的概率之和为 1。

#### GPT-RAGKG (GPT-5.5 + graphrag/RAG)

📎 cite · • 2 bullets · 𝑓 7 latex · 🌐 mixed · 📏 215 字 · ⏱ 14358ms

> 马尔科夫模型的状态转移概率矩阵 $A=(a_{ij})$ 需要满足两个约束：
>
> - 非负性：每个转移概率都不能小于 $0$，即 $a_{ij}\ge 0$。
> - 归一性：从任一状态 $i$ 出发，转移到所有可能状态的概率之和为 $1$，即 $\sum_{j=1}^{N} a_{ij}=1$。
>
> 这些约束对应文档中对模型参数初始化时要求 $a_{ij}$ 满足的概率约束。[Source: ch3.pdf, Page 49/51]

**引用**：`ch3.pdf` loc=`Page 49/51` score=0.487 · `ch3.pdf` loc=`Page 8/51` score=0.463 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.453 · `ch3.pdf` loc=`Page 26/51` score=0.452 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+3 more)

#### Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)

📎 cite · • 2 bullets · 𝑓 6 latex · 🌐 mixed · 📏 208 字 · ⏱ 18006ms

> 马尔科夫模型的状态转移概率矩阵 $A=(a_{ij})$ 需要满足两个约束：
>
> - 非负性：任意状态转移概率都不能为负，即 $a_{ij}\ge 0$。
> - 行归一化：从某个状态 $i$ 出发转移到所有可能状态的概率之和为 $1$，即 $\sum_{j=1}^{N} a_{ij}=1$。
>
> 文档中给出的转移矩阵示例每一行概率之和都为 $1$，体现了这一约束。[Source: ch3.pdf, Page 8/51]

**引用**：`ch3.pdf` loc=`Page 49/51` score=0.487 · `ch3.pdf` loc=`Page 8/51` score=0.463 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.453 · `ch3.pdf` loc=`Page 26/51` score=0.452 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+3 more)

#### Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)

📎 cite · • 2 bullets · 𝑓 7 latex · 🌐 mixed · 📏 300 字 · ⏱ 79516ms

> 马尔科夫模型的状态转移概率矩阵 $A=(a_{ij})$ 需要满足两个约束：
>
> - **非负性**：任意状态 $i$ 到状态 $j$ 的转移概率都不能为负，即 $a_{ij}\ge 0$。
> - **行归一化**：从任一状态 $i$ 出发，转移到所有可能状态的概率之和为 $1$，即 $\sum_j a_{ij}=1$。
>
> 参考资料中的示例矩阵每一行概率相加都为 $1$，体现了状态转移概率矩阵的归一化要求；Baum-Welch 参数估计部分也给出了对 $a_{ij}$ 的约束条件。 [Source: ch3.pdf, Page 8/51] [Source: ch3.pdf, Page 49/51]

**引用**：`ch3.pdf` loc=`Page 49/51` score=0.487 · `ch3.pdf` loc=`Page 8/51` score=0.463 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.453 · `ch3.pdf` loc=`Page 26/51` score=0.452 · `ch9.pdf` loc=`Page 50/70` score=0.00115 · …(+3 more)

---

### Q024（ch3 · formula）

**题目**：定义前向变量 `alpha_t(i)`，并写出初始化、递推和终止求和公式。

**参考答案**：

> `alpha_t(i)=P(o1...ot,q_t=s_i|mu)`；初始化 `alpha_1(i)=pi_i b_i(o1)`；递推 `alpha_{t+1}(j)=[sum_i alpha_t(i)a_ij]b_j(o_{t+1})`；终止 `P(O|mu)=sum_i alpha_T(i)`。

#### GPT-bare (GPT-5.5, no RAG)

𝑓 36 latex · 🌐 mixed · 📏 507 字 · ⏱ 26248ms

> 前向变量定义为：
>
> \[
> \alpha_t(i)=P(o_1,o_2,\dots,o_t,\ q_t=i \mid \lambda)
> \]
>
> 表示在模型 \(\lambda\) 下，时刻 \(t\) 处于状态 \(i\)，并且已经观测到序列前 \(t\) 个观测值的联合概率。
>
> **初始化：**
>
> \[
> \alpha_1(i)=\pi_i b_i(o_1), \quad 1\le i\le N
> \]
>
> 其中，\(\pi_i\) 是初始状态概率，\(b_i(o_1)\) 是状态 \(i\) 生成观测 \(o_1\) 的概率。
>
> **递推：**
>
> \[
> \alpha_{t+1}(j)=\left[\sum_{i=1}^{N}\alpha_t(i)a_{ij}\right] b_j(o_{t+1})
> \]
>
> 其中，\(a_{ij}\) 是从状态 \(i\) 转移到状态 \(j\) 的概率。
>
> **终止求和：**
>
> \[
> P(O\mid \lambda)=\sum_{i=1}^{N}\alpha_T(i)
> \]
>
> 其中，\(O=(o_1,o_2,\dots,o_T)\) 是观测序列，\(T\) 是序列长度。

#### GPT-RAGKG (GPT-5.5 + graphrag/RAG)

📎 cite · • 3 bullets · 𝑓 52 latex · 🌐 mixed · 📏 661 字 · ⏱ 36294ms

> **前向变量定义：**  
> $\alpha_t(i)$ 表示在模型 $\mu$ 下，到时刻 $t$ 为止已经观察到 $o_1,\dots,o_t$，且当前状态为 $s_i$ 的联合概率：
>
> $$
> \alpha_t(i)=P(o_1,o_2,\dots,o_t,\ q_t=s_i\mid \mu)
> $$
>
> HMM 中观测序列概率可通过对所有状态路径的联合概率求和得到，且联合概率由初始概率、转移概率和发射概率相乘构成。[Source: ch3.pdf, Page 18/51 (part 1)]
>
> **初始化：**
>
> $$
> \alpha_1(i)=\pi_i b_i(o_1),\quad 1\le i\le N
> $$
>
> **递推：**
>
> $$
> \alpha_t(i)=\left[\sum_{j=1}^{N}\alpha_{t-1}(j)a_{ji}\right]b_i(o_t),\quad 2\le t\le T,\ 1\le i\le N
> $$
>
> 其中：
> - $\pi_i$：初始时刻处于状态 $s_i$ 的概率
> - $a_{ji}$：从状态 $s_j$ 转移到状态 $s_i$ 的概率
> - $b_i(o_t)$：状态 $s_i$ 生成观测 $o_t$ 的发射概率
>
> **终止求和：**
>
> $$
> P(O\mid \mu)=\sum_{i=1}^{N}\alpha_T(i)
> $$
>
> 这一步对应“对可能状态路径求和得到观察序列概率”的思想。[Source: ch3.pdf, Page 18/51 (part 1)]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.581 · `ch3.pdf` loc=`Page 49/51` score=0.474 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.461 · `ch4(2).pdf` loc=`Page 24/97` score=0.445 · `ch9.pdf` loc=`Page 54/70` score=0.00115 · …(+13 more)

#### Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)

📎 cite · 𝑓 49 latex · 🌐 mixed · 📏 736 字 · ⏱ 122759ms

> **前向变量定义：**  
> $\alpha_t(i)$ 表示在模型 $\mu$ 下，到时刻 $t$ 为止已经观察到序列 $o_1,\ldots,o_t$，并且当前隐藏状态为 $s_i$ 的概率，即：
>
> $$
> \alpha_t(i)=P(o_1,o_2,\ldots,o_t,\ q_t=s_i\mid \mu)
> $$
>
> HMM 中一条路径的联合概率由初始概率、状态转移概率和发射概率相乘得到，这是前向递推的依据。[Source: ch3.pdf, Page 18/51 (part 1)]
>
> **1. 初始化：**
>
> $$
> \alpha_1(i)=\pi_i b_i(o_1),\quad 1\le i\le N
> $$
>
> **2. 递推：**
>
> $$
> \alpha_{t+1}(j)=\left[\sum_{i=1}^{N}\alpha_t(i)a_{ij}\right]b_j(o_{t+1}),\quad 1\le j\le N,\ 1\le t\le T-1
> $$
>
> 其中 $a_{ij}$ 是从状态 $s_i$ 转移到 $s_j$ 的概率，$b_j(o_{t+1})$ 是状态 $s_j$ 生成观察值 $o_{t+1}$ 的发射概率；文档中也说明了 HMM 路径概率由转移概率与发射概率共同构成。[Source: ch3.pdf, Page 18/51 (part 1)]
>
> **3. 终止求和：**
>
> $$
> P(O\mid \mu)=\sum_{i=1}^{N}\alpha_T(i)
> $$
>
> 即把最后时刻处于所有可能状态的前向概率相加，得到整个观察序列的概率；文档中对应问题是“计算观察值序列的概率”。[Source: ch3.pdf, Page 26/51]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.581 · `ch3.pdf` loc=`Page 49/51` score=0.474 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.461 · `ch4(2).pdf` loc=`Page 24/97` score=0.445 · `ch9.pdf` loc=`Page 54/70` score=0.00115 · …(+13 more)

#### Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)

📎 cite · • 4 bullets · 𝑓 56 latex · 🌐 mixed · 📏 783 字 · ⏱ 121407ms

> **前向变量定义**：$\alpha_t(i)$ 表示在模型 $\mu$ 下，到时刻 $t$ 为止已经观察到序列 $o_1,\ldots,o_t$，且当前隐藏状态为 $s_i$ 的概率：  
> $$
> \alpha_t(i)=P(o_1,o_2,\ldots,o_t,\ q_t=s_i\mid \mu)
> $$
> 它用于把观察序列概率 $P(O\mid \mu)$ 分解为对隐藏状态路径的动态规划求和；资料中也说明 $P(O)$ 可通过对所有状态路径 $Q$ 的联合概率 $P(O,Q)$ 求和得到。[Source: ch3.pdf, Page 18/51 (part 1)]
>
> **初始化**：  
> $$
> \alpha_1(i)=\pi_i b_i(o_1),\quad 1\le i\le N
> $$
>
> **递推**：  
> $$
> \alpha_{t+1}(j)=\left[\sum_{i=1}^{N}\alpha_t(i)a_{ij}\right]b_j(o_{t+1}),\quad 1\le t\le T-1,\ 1\le j\le N
> $$
>
> **终止求和**：  
> $$
> P(O\mid \mu)=\sum_{i=1}^{N}\alpha_T(i)
> $$
>
> 其中：  
> - $\pi_i$：初始时刻处于状态 $s_i$ 的概率  
> - $a_{ij}$：从状态 $s_i$ 转移到状态 $s_j$ 的概率  
> - $b_j(o_{t+1})$：状态 $s_j$ 生成观察值 $o_{t+1}$ 的概率  
> - $N$：隐藏状态个数，$T$：观察序列长度  
>
> 资料给出了反向变量 $\beta_t(i)$ 的初始化、递推与终止求和形式；前向变量公式与其方向相反，用于同一个“计算观察值序列概率”的问题。[Source: ch3.pdf, Page 26/51]

**引用**：`ch3.pdf` loc=`Page 26/51` score=0.581 · `ch3.pdf` loc=`Page 49/51` score=0.474 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.461 · `ch4(2).pdf` loc=`Page 24/97` score=0.445 · `ch9.pdf` loc=`Page 54/70` score=0.00115 · …(+13 more)

---

### Q025（ch3 · calculation）

**题目**：天气 HMM 中状态为 `{晴,阴,雨}`，`pi=(0.63,0.17,0.20)`，发射概率 `b_晴(干透)=0.60`、`b_阴(干透)=0.25`、`b_雨(干透)=0.05`。计算 `alpha_1`。

**参考答案**：

> `alpha_1=(0.63x0.60, 0.17x0.25, 0.20x0.05)=(0.378,0.0425,0.0100)`。

#### GPT-bare (GPT-5.5, no RAG)

• 3 bullets · 𝑓 17 latex · 🌐 mixed · 📏 280 字 · ⏱ 17471ms

> \[
> \alpha_1(i)=\pi_i \, b_i(o_1)
> \]
>
> 观测为“干透”，所以：
>
> - \(\alpha_1(\text{晴})=0.63\times 0.60=0.378\)
> - \(\alpha_1(\text{阴})=0.17\times 0.25=0.0425\)
> - \(\alpha_1(\text{雨})=0.20\times 0.05=0.010\)
>
> 因此：
>
> \[
> \alpha_1=(0.378,\ 0.0425,\ 0.010)
> \]
>
> 按状态顺序 \(\{\text{晴},\text{阴},\text{雨}\}\)。

#### GPT-RAGKG (GPT-5.5 + graphrag/RAG)

📎 cite · • 3 bullets · 𝑓 14 latex · 🌐 mixed · 📏 326 字 · ⏱ 23774ms

> `alpha_1` 的初始化为：初始状态概率乘以该状态生成第一个观测“干透”的发射概率，即 $\alpha_1(i)=\pi_i b_i(o_1)$；HMM 中路径概率由初始概率、转移概率和发射概率相乘得到。[Source: ch3.pdf, Page 18/51 (part 1)]
>
> - $\alpha_1(\text{晴})=0.63 \times 0.60=0.378$
> - $\alpha_1(\text{阴})=0.17 \times 0.25=0.0425$
> - $\alpha_1(\text{雨})=0.20 \times 0.05=0.01$
>
> 所以：
>
> $$
> \alpha_1=(0.378,\ 0.0425,\ 0.01)
> $$

**引用**：`ch4.pdf` loc=`Page 21/64` score=0.502 · `ch3.pdf` loc=`Page 34/51 (part 2)` score=0.493 · `ch3.pdf` loc=`Page 49/51` score=0.00155 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.0013 · `ch3.pdf` loc=`Page 8/51` score=0.0013 · …(+1 more)

#### Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)

📎 cite · • 3 bullets · 𝑓 10 latex · 🌐 mixed · 📏 385 字 · ⏱ 26081ms

> `alpha_1` 的初始化就是“初始状态概率 × 当前观测的发射概率”，即：
>
> - $\alpha_1(\text{晴})=0.63 \times 0.60=0.378$
> - $\alpha_1(\text{阴})=0.17 \times 0.25=0.0425$
> - $\alpha_1(\text{雨})=0.20 \times 0.05=0.010$
>
> 所以：
>
> $$
> \alpha_1=(0.378,\ 0.0425,\ 0.010)
> $$
>
> 这里用到的是 HMM 中路径概率由初始概率、转移概率和发射概率相乘得到的思想；给出的天气例子中也出现了相同的初始数值 $0.378, 0.0425, 0.01$。[Source: ch3.pdf, Page 18/51 (part 1)] [Source: ch3.pdf, Page 34/51 (part 2)]

**引用**：`ch4.pdf` loc=`Page 21/64` score=0.502 · `ch3.pdf` loc=`Page 34/51 (part 2)` score=0.493 · `ch3.pdf` loc=`Page 49/51` score=0.00155 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.0013 · `ch3.pdf` loc=`Page 8/51` score=0.0013 · …(+1 more)

#### Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)

📎 cite · • 3 bullets · 𝑓 17 latex · 🌐 mixed · 📏 378 字 · ⏱ 95302ms

> 按前向算法第一步，初始时刻的前向概率为  
> $$\alpha_1(i)=\pi_i\,b_i(o_1)$$  
> 这里 $o_1=\text{干透}$。根据给定参数，状态顺序取“晴、阴、雨”，则
>
> - $\alpha_1(\text{晴})=0.63\times 0.60=0.378$
> - $\alpha_1(\text{阴})=0.17\times 0.25=0.0425$
> - $\alpha_1(\text{雨})=0.20\times 0.05=0.01$
>
> 所以  
> $$\alpha_1=(0.378,\;0.0425,\;0.01)$$  
> 这是由初始状态概率 $\pi$ 和发射概率 $b_i(o_1)$ 直接相乘得到的。 [Source: ch3.pdf, Page 8/51] [Source: ch3.pdf, Page 18/51]

**引用**：`ch4.pdf` loc=`Page 21/64` score=0.502 · `ch3.pdf` loc=`Page 34/51 (part 2)` score=0.493 · `ch3.pdf` loc=`Page 49/51` score=0.00155 · `ch3.pdf` loc=`Page 18/51 (part 1)` score=0.0013 · `ch3.pdf` loc=`Page 8/51` score=0.0013 · …(+1 more)

---

## 3. 风格观察

### 3.1 重要前提：RAFT CoT preamble 已被 server 端 strip

指标表里 4 路线 **CoT preamble 率全部 = 0%**，这**不**代表 Qwen-RAFT 不输出三段套话。`nano_notebooklm/ai/qwen_raft_backend.py` 的 `_strip_raft_preamble` 在响应到达前端之前已经把「先分析问题要点 / 引用原文关键内容 / 给出最终结论」头部段落剥掉，只保留实质答案。所以这份报告里 RAFT 的文风比模型 raw output **干净得多**——前面 session 里观察到的「答案前两段都是套话」是 raw 模型行为，线上产品已经把这层显式 mask 掉了。

### 3.2 自动总体对比

- **答案长度排序**：**Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** (560 字) > **GPT-bare (GPT-5.5, no RAG)** (473 字) > **Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** (402 字) > **GPT-RAGKG (GPT-5.5 + graphrag/RAG)** (357 字)
- **cite 率排序**：**Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** (100%) > **Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** (100%) > **GPT-RAGKG (GPT-5.5 + graphrag/RAG)** (96%) > **GPT-bare (GPT-5.5, no RAG)** (0%)
- **LaTeX 覆盖排序**：**GPT-bare (GPT-5.5, no RAG)** (75%) > **GPT-RAGKG (GPT-5.5 + graphrag/RAG)** (42%) > **Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** (42%) > **Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** (25%)
- **平均延迟排序（高 → 低）**：**Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** (102.7s) > **Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** (58.9s) > **GPT-bare (GPT-5.5, no RAG)** (49.1s) > **GPT-RAGKG (GPT-5.5 + graphrag/RAG)** (46.1s)

### 3.3 各路线风格画像

**GPT-bare (GPT-5.5, no RAG)** (99 题样本)：
  - 长度：均 473 字 / 中位 459 / p90 803
  - cite=0%，preamble=0%，分点=77%，LaTeX=75%，中英混合=87%
  - 延迟：均 49105ms / 中位 27230ms

**GPT-RAGKG (GPT-5.5 + graphrag/RAG)** (99 题样本)：
  - 长度：均 357 字 / 中位 315 / p90 600
  - cite=96%，preamble=0%，分点=55%，LaTeX=42%，中英混合=98%
  - 延迟：均 46138ms / 中位 31201ms

**Qwen-Base (Qwen2.5-7B-Instruct, 4-bit + RAG)** (26 题样本)：
  - 长度：均 402 字 / 中位 418 / p90 640
  - cite=100%，preamble=0%，分点=65%，LaTeX=42%，中英混合=100%
  - 延迟：均 58925ms / 中位 37632ms

**Qwen-RAFT (Qwen2.5-7B-RAFT, 4-bit + RAG)** (20 题样本)：
  - 长度：均 560 字 / 中位 550 / p90 783
  - cite=100%，preamble=0%，分点=85%，LaTeX=25%，中英混合=100%
  - 延迟：均 102740ms / 中位 97516ms

### 3.4 定性观察（基于 5 题样例）

- **GPT-bare**：完全无 cite（没接 RAG，靠通用知识），分点最多（77%）+ LaTeX 最积极（75%），**最像教科书自答**。但对课程独有内容（如课件特定章节划分、特定例子）会幻觉。
- **GPT-RAGKG**：cite 率 96%（每条论断都标 `[Source: chX.pdf, Page Y/Z]`），**最简洁**（均 357 字，p90 600 字），分点反而最少（55%）— 因为 cite 标记让段落变 prose-like。**最像有依据的助教回答**。
- **Qwen-Base**：cite 率 100%（接 RAG 之后忠实引用），文风类似 GPT-RAGKG 但**多用'例如/比如'扩展**，**最像课堂手册答案**。
- **Qwen-RAFT**：尽管 server 已 strip 三段 preamble，剩余内容**仍是 4 路线中最长**（均 560 字），且 LaTeX 覆盖最低（25%）— 印证之前观察「RAFT 对公式不敏感」。RAFT 的训练目标是「基于上下文做扩展性回答」，所以即便剥掉头部套话，正文还是会**比 base 更详细 / 更举例 / 更接近教学风格**。**延迟显著高**（均 103s vs Base 59s）。

### 3.5 建议的产品定位文案

> 🤖 **Codex (GPT-5.5 + 图谱检索)**：适合需要标准教学化、可追溯引用的概念解释与公式推导。
>
> 🎓 **Qwen-RAFT**：适合需要详细展开、举例丰富的概念性解答；公式与数值题不推荐。
>
> 🐧 **Qwen-Base**：与 RAFT 同等覆盖但更简洁，适合速答与课件复习。
