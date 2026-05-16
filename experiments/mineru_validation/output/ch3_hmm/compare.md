# Compare: ch3_hmm

## PyMuPDF baseline

```
# PyMuPDF baseline: ch3_hmm
_pages: 1..12 of 51_

## Page 1

1
第二章 隐马尔科夫模型
自然语言处理

## Page 2

• 马尔科夫模型（Markov Model）
– 最早由Andrei A.Markov（切比雪夫Chebyshev的一个学生）于1913年提出，它的
最原始目的是为了语言上的应用
• 隐马尔科夫模型（Hidden Markov Model，HMM）
– 其数学思想是由Baum等人于1966~1970提出
– 从某种意义上讲，HMM本身是一个马尔科夫过程的概率函数
– 在20世纪70年代被CMU的Baker以及IBM的Jelinek等人应用在了语音处理上
– 之后被广泛应用在了汉语自动分词、词性标注、统计机器翻译等很多方面
3.0
引言

## Page 3

马尔科夫模型
3.1
隐马尔科夫模型
3.2
目录
Contents
3
HMM的三个基本问题
3.3

## Page 4

•
Markov，1913
•
随机过程
– 又称“随机函数”，是随时间而随机变化的过程
•
马尔科夫模型描述了一类重要的随机过程
)
,
,
|
(
2
1

k
t
i
t
j
t
s
q
s
q
s
q
P





的状态
，
，
，
的概率取决于其在时间
处于状态
则系统在时间
，
为一随机变量序列，
一状态。
将从某一状态转移到另
随着时间的推移，系统
个状态
设系统有
1
2
1
,
,2,1
}
,
,
,
{
},
,
,
,
{
2
1
2
1





t
s
t
T
t
S
q
q
q
q
Q
s
s
s
S
N
j
t
T
N




3.1
马尔科夫模型

## Page 5

• 一阶马尔可夫过程
– 如果系统在t时间的状态只与其在时间t-1的状态相关，则该随机过
程称为一阶马尔可夫过程
• 马尔可夫模型
– 独立于时间t的随机过程
)
|
(
)
,
,
|
(
1
2
1
i
t
j
t
k
t
i
t
j
t
s
q
s
q
P
s
q
s
q
s
q
P














N
j
ij
ij
ij
a
a
a
1
1
)
2
0
)1
须满足以下条件
称为状态转移概率
ij
a
)
,
1(,
)
|
(
1
N
j
i
a
s
q
s
q
P
ij
i
t
j
t






3.1
马尔科夫模型

## Page 6

•
状态：晴朗，多云，下雨
•
状态转移概率矩阵
•
初始概率矩阵
3.1
马尔科夫模型

## Page 7

)
,
A
,S
(


状态集合


}
{s
S
i
状态转移概率矩阵


}
{a
A
ij
初始概率矩阵


}
{
i


{ 晴朗，多云，下雨 }
3.1
马尔科夫模型

## Page 8

}
s,
s,
s
{
3
2
1
a
v
n
S
















4.0
2.0
4.0
2.0
3.0
5.0
2.0
5.0
3.0
)
a(
a
v
n
a
v
n
A
ij
]
2.0
2.0
6.0
[
}
{
i
a
v
n



)
|
(
)
|
(
)
|
(
)
(
)
,
,
,
(
3
4
2
3
1
2
1
4
3
2
1
a
q
n
q
P
v
q
a
q
P
n
q
v
q
P
n
q
P
n
q
a
q
v
q
n
q
P















4
2
0.0
4.0
2.0
5.0
6.0





3.1
马尔科夫模型

## Page 9

马尔科夫模型
3.1
隐马尔科夫模型
3.2
目录
Contents
9
HMM的三个基本问题
3.3

## Page 10

3.2
隐马尔科夫模型

## Page 11

)
states
hidden 
(
S
状态集合

)
,
B
,
A
,
V
,S
(


集合
输出符号
观察值
)
(

V
转移概率矩阵


}
{a
A
ij
发射概率矩阵


)
( ik
b
B
初始概率矩阵


}
{
i


为了简单，有时也将其记为 
)
,
B
,
A
(


3.2
隐马尔科夫模型

## Page 12

马尔科夫模型
3.1
隐马尔科夫模型
3.2
目录
Contents
12
HMM的三个基本问题
3.3
3.2.1 计算观察值序列的概率
3.2.2 确定最优状态序列
3.2.3 HMM的参数估计

```

## MinerU output

```
## 自然语言处理第二章 隐马尔科夫模型

## 马尔科夫模型 (Markov Model)

一最早由AndreiA.Markov，（切比雪夫Chebyshev的一个学生）于1913年提出，它的最原始目的是为了语言上的应用

● 隐马尔科夫模型(Hidden Markov Model，HMM)

－其数学思想是由Baum等人於1966\~1970提出

一从某种意义上讲，HMM本身是一个马尔科夫过程的概率函数

在20世纪70年代被CMU的Baker以及IBM的Jelinek等人应用在了语音处理上

之后被广泛应用在了汉语自动分词、词性标注、统计机器翻译等很多方面

## 目录 Contents

3.1 马尔科夫模型  
3.2 隐马尔科夫模型  
3.3 HMM的三个基本问题

![](images/2258c6f58b08e0e52e53efbc4f88c5c5957d21e2c6da5c85c7bd65759c3bf575.jpg)

• Markov, 1913

• 随机过程

又称“随机函数”，是随时间而随机变化的过程

• 马尔科夫模型描述了一类重要的随机过程

设系统有N个状态 ${ \cal S } = \{ s _ { 1 } , s _ { 2 } , \cdots , s _ { N } \}$

随着时间的推移，系统将从某一状态转移到另一状态。

$\mathcal { Q } = \{ q _ { 1 } , q _ { 2 } , \cdots , q _ { T } \}$ 为一随机变量序列， $\textstyle q _ { t } \in S , t = 1 , 2 , \cdots , T$

则系统在时间t处于状态 $\dot { s } _ { j }$ 的概率取决于其在时间1,2,…，t-1的状态

$$
P ( q _ { t } = s _ { j } \mid q _ { t - 1 } = s _ { i } , q _ { t - 2 } = s _ { k } , \cdots )
$$

## • 一阶马尔可夫过程

如果系统在t时间的状态只与其在时间t-1的状态相关，则该随机过程称为一阶马尔可夫过程

$$
P ( q _ { t } = s _ { j } \mid q _ { t - 1 } = s _ { i } , q _ { t - 2 } = s _ { k } , \cdots ) \approx P ( q _ { t } = s _ { j } \mid q _ { t - 1 } = s _ { i } )
$$

• 马尔可夫模型

独立于时间t的随机过程

$$
P ( q _ { t } = s _ { j } \mid q _ { t - 1 } = s _ { i } ) = a _ { i j } , ( 1 \leq i , j \leq N )
$$

$a _ { i j }$ 称为状态转移概率

$a _ { i j }$ 须满足以下条件

$$
a _ { i j } \geq 0
$$

$$
\sum _ { j = 1 } ^ { N } a _ { i j } = 1
$$

## 天气变化的例子

![](images/9668ef07acd5b3cf70f2f778384099d0b145aaeece33cc7cd216ade6118d0175.jpg)

● 状态：晴朗，多云，下雨

• 状态转移概率矩阵

<table><tr><td colspan="4">今天</td></tr><tr><td>晴朗</td><td>晴朗</td><td>多云</td><td>下雨</td></tr><tr><td>昨天多云</td><td>0.50</td><td>0.375</td><td>0.125</td></tr><tr><td></td><td>0.25</td><td>0.125</td><td>0.625</td></tr><tr><td>下雨</td><td>0.25</td><td>0.375</td><td>0.375</td></tr></table>

• 初始概率矩阵

<table><tr><td>晴朗</td><td>多云</td><td>下雨</td></tr><tr><td>（0.63</td><td>0.17</td><td>0.20）</td></tr></table>

## 马尔科夫模型的形式化定义

$$
\mu = ( \mathrm { S } , \mathrm { A } , \pi )
$$

S ={s₁}→状态集合

A= {aj}→状态转移概率矩阵

$\pi = \left\{ \pi _ { \mathrm { i } } \right\}$ →初始概率矩阵

{晴朗，多云，下雨}

$$
\begin{array} { r l } & { \frac { A _ { \widehat { \mathbf { A } } } } { B } \mathbb { A } } \\ { \frac { A _ { \widehat { \mathbf { A } } } } { B } \mathbb { H } } & { \left( \begin{array} { l l l } { \mathbb { H } _ { \widehat { \mathbf { B } } } ^ { \pm } \mathbb { H } } & { \widehat { \mathbf { S } } \widehat { \mathbf { z } } } & { \widehat { \mathbb { F } } \overline { { \mathbb { H } } } } \\ { 0 . 5 0 } & { 0 . 3 7 5 } & { 0 . 1 2 5 } \\ { 0 . 2 5 } & { 0 . 1 2 5 } & { 0 . 6 2 5 } \\ { 0 . 2 5 } & { 0 . 3 7 5 } & { 0 . 3 7 5 } \end{array} \right) } \end{array}
$$

晴朗 多云 下雨（0.63 0.17 0.20）

马尔科夫模型在NLP中的应用举例

$$
S = \{ \mathbf { s } _ { 1 } = n , \mathbf { s } _ { 2 } = \nu , \mathbf { s } _ { 3 } = a \}
$$

$$
\overset { n } { \underset { { A = \left( \mathbf { a } _ { i j } \right) = \nu } } { \sum } } \overset { n } { \underset { { \nu } } { \sum } } \overset { \nu } { \underset { { \left( \mathbf { 0 . 3 } \right. \begin{array} { l } { { 0 . 5 } } \end{array} } } { \sum } } 0 . 2 
$$

$$
\pi = \{ \pi _ { \mathrm { i } } \} = { \begin{array} { c c c } { n } & { \quad \nu } & { \quad a } \\ { [ \phantom { { \ e q e m e m } } 0 . 6 } & { 0 . 2 } & { \quad 0 . 2 } \end{array} ] }
$$

$$
\begin{array} { r l } & { P ( q _ { 1 } = n , q _ { 2 } = \nu , q _ { 3 } = a , q _ { 4 } = n ) } \\ & { = P ( q _ { 1 } = n ) \times P ( q _ { 2 } = \nu \mid q _ { 1 } = n ) \times P ( q _ { 3 } = a \mid q _ { 2 } = \nu ) \times P ( q _ { 4 } = n \mid q _ { 3 } = a ) } \\ & { = 0 . 6 \times 0 . 5 \times 0 . 2 \times 0 . 4 } \\ & { = 0 . 0 2 4 } \end{array}
$$

## 目录 Contents

3.1 马尔科夫模型  
3.2 隐马尔科夫模型  
3.3 HMM的三个基本问题

![](images/d060817c9f23928126251d50e6ee4a301b29e0f61d66fceb8c38420936cdc19e.jpg)

![](images/f199e2f3d0e95e592b2c29a5bd57484d6408c06aec87452aeb39ce5bc79812cf.jpg)

## HMM的形式化定义

$$
\mu = ( \mathrm { S } , \mathrm { V } , \mathrm { A } , \mathrm { B } , \pi )
$$

S →状态集合(hidden states)

V→观察值(输出符号)集合

$\mathrm { A } = \{ \mathfrak { a } _ { \mathrm { i j } } \}$ →转移概率矩阵

$$
B = \left( { b _ { i k } } \right)
$$

发射概率矩阵

$$
\pi = \{ \pi _ { \mathrm { i } } \} 
$$

初始概率矩阵

![](images/df3a61e0c6dc6fc42deda52dabb61d425f1d806928cafc7fb4240d156cb484c0.jpg)

晴朗（0.63多云0.17下雨0.20）

为了简单，有时也将其记为 $\mu = ( \mathbf { A } , \mathbf { B } , \pi )$

![](images/a0f9c14072814d9a54530e469b9856ba6a38d4e03f901d6e323519d28161e6f5.jpg)

<table><tr><td rowspan=2 colspan=2>转移概率A</td><td rowspan=1 colspan=3>today</td></tr><tr><td rowspan=1 colspan=1>晴</td><td rowspan=1 colspan=1>阴</td><td rowspan=1 colspan=1>雨</td></tr><tr><td rowspan=3 colspan=1>yesterday</td><td rowspan=1 colspan=1>晴</td><td rowspan=1 colspan=1>0.50</td><td rowspan=1 colspan=1>0.375</td><td rowspan=1 colspan=1>0.125</td></tr><tr><td rowspan=1 colspan=1>阴</td><td rowspan=1 colspan=1>0.25</td><td rowspan=1 colspan=1>0.125</td><td rowspan=1 colspan=1>0.625</td></tr><tr><td rowspan=1 colspan=1>雨</td><td rowspan=1 colspan=1>0.25</td><td rowspan=1 colspan=1>0.375</td><td rowspan=1 colspan=1>0.375</td></tr></table>

<table><tr><td rowspan=2 colspan=2>发射概率B</td><td rowspan=1 colspan=4>显式状式</td></tr><tr><td rowspan=1 colspan=1>干透</td><td rowspan=1 colspan=1>较干</td><td rowspan=1 colspan=1>潮湿</td><td rowspan=1 colspan=1>湿透</td></tr><tr><td rowspan=3 colspan=1>隐式状态</td><td rowspan=1 colspan=1>晴</td><td rowspan=1 colspan=1>0.60</td><td rowspan=1 colspan=1>0.20</td><td rowspan=1 colspan=1>0.15</td><td rowspan=1 colspan=1>0.05</td></tr><tr><td rowspan=1 colspan=1>阴</td><td rowspan=1 colspan=1>0.25</td><td rowspan=1 colspan=1>0.25</td><td rowspan=1 colspan=1>0.25</td><td rowspan=1 colspan=1>0.25</td></tr><tr><td rowspan=1 colspan=1>雨</td><td rowspan=1 colspan=1>0.05</td><td rowspan=1 colspan=1>0.10</td><td rowspan=1 colspan=1>0.35</td><td rowspan=1 colspan=1>0.50</td></tr></table>

3.1 马尔科夫模型   
3.2 隐马尔科夫模型   
3.3 HMM的三个基本问题   
3.2.1计算观察值序列的概率   
3.2.2 确定最优状态序列   
3.2.3 HMM的参数估计

## 目录 Contents

![](images/84ca365f65b749d0f7cfee9e8f8f077f3c64769620c35273463d58627be5b866.jpg)
```