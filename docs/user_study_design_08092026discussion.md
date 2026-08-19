现在可以把整篇论文的逻辑压成非常清楚的三块：**framing = progressive co-authoring vs. one-shot autonomous planning；baseline = EMOS-like one-shot planning attempt；tasks = 同一个目标逐步加入 coordination preferences，观察用户如何重构计划。**

## 1. Framing：从 one-shot delegation 到 progressive co-authoring

顶层不要把论文 framed 成：

> 一个带 timeline 和 3D editing 的 LLM multi-robot planning interface。

更合适的是：

> **Existing LLM-based multi-robot planners largely treat human input as an upfront task specification, after which task decomposition, allocation, and coordination are handled autonomously. We explore a different interaction paradigm in which users can progressively co-author an evolving multi-robot plan.**

这里真正的对立不是：

**LLM vs. manual programming**

而是：

**one-shot autonomous planning**
vs.
**progressive human–AI co-authoring**

像 EMOS 这样的系统，虽然内部有 central planner、robot agents、reflection、reassignment，但这些 iteration 是 **agent–agent interaction**；从用户这一侧看，interaction abstraction 仍然基本是：

> Task (T) → autonomous planning/allocation/execution.

因此你的 gap 可以写成：

> Existing systems increasingly support sophisticated deliberation **among agents**, but provide comparatively limited support for deliberation **between the human and the evolving multi-robot plan**.

你的系统则把 planning 从一次性的 delegation 变成：

> **specify → inspect → revise → reconcile → inspect again**

而且核心不是要求用户自己完成所有 planning，而是：

> **Users progressively take ownership of the coordination decisions that matter to them while continuing to delegate the remaining decisions to AI.**

这是目前我觉得最强的 conceptual positioning。

### 一个很合适的 research question

可以先围绕：

> **How can users progressively refine multi-robot coordination after an initial task has been delegated to an autonomous planner?**

更偏 evaluation 的版本是：

> **How does progressive plan co-authoring affect users' ability to understand and revise multi-robot coordination compared with one-shot autonomous planning?**

---

# 2. 系统在这个 framing 下是什么

你的系统不是“更聪明的 planner”，而是一个：

> **persistent co-authoring workspace around an evolving multi-robot plan.**

初始 high-level instruction 仍然可以交给 AI 自动：

* decomposition；
* task assignment；
* ordering；
* facility handling；
* waiting / coordination。

但是生成以后，plan 不再只是 planner 的内部结果，而成为一个持续存在的 interaction object。

用户可以逐步接管：

* robot assignment；
* task ordering；
* route；
* waypoint；
* standoff pose；
* target placement；

同时没有明确修改的部分继续由 automation 决定。

而且一旦用户明确做出决定，这些内容会成为后续 resolver 必须尊重的约束；自动修复只能使用剩余的自由度。

所以这里可以形成一个很统一的概念：

> **progressive specification + selective authorship + preserved commitments**

三者是连起来的：

**Progressive specification**

用户不需要一开始想到所有 preference。

↓

**Selective authorship**

看到 AI plan 后，只修改自己关心的部分。

↓

**Preserved commitments**

后续 automation 不会把这些明确决定悄悄改掉。

这比单纯强调 timeline / chat / 3D editing 强得多。

---

# 3. Baseline：EMOS-like one-shot autonomous planning

我们现在已经基本收敛成：

> **One-shot autonomous planning baseline inspired by the interaction paradigm of systems such as EMOS.**

这里最好不要说“we compare against EMOS”，因为你并不是复现 EMOS architecture。

你比较的是它所代表的一种 interaction model：

> **human provides task (T) → autonomous planner decomposes, allocates, coordinates, and executes.**

## Baseline 用户需要输入什么？

要求用户**枚举 desired task outcomes**，但不要求用户提前完成 robot assignment。

例如 Sorting：

> Put the apple and banana in the fridge, and put the bowl and cup in the sink.

这是合理的。

不要要求：

> Robot 1 handles apple and banana; Robot 2 handles bowl and cup.

因为 allocation 本来就应该是 autonomous planner 的职责。

同样，也不要要求用户枚举：

> navigate → open → pick → close...

那就过度接近 programming 了。

所以 baseline specification 粒度是：

> **object/subgoal-level enumeration**

而不是：

> atomic execution script

或者：

> explicit multi-robot allocation.

---

# 4. “one-shot” 的准确含义

这里非常重要。

不是：

> 一个 participant 整个实验只能 prompt 一次。

而是：

> **each planning attempt is one-shot.**

也就是说 baseline 可以经历多个 scenario revision，但每次 requirement 改变以后，用户需要提交一个**新的、完整的 task specification**，然后 planner 从头生成一个新的 solution。

例如：

### Attempt 1

[
T_1
]

> Apple/banana → fridge; cup/bowl → sink.

生成 plan。

---

实验员增加 requirement：

> Now organize the work by object category.

### Attempt 2

用户重新提交完整 specification：

[
T_2 =
T_1 +
\text{category-based allocation}
]

系统重新 plan。

---

再增加：

> Keep that assignment, but make the food tasks finish before tableware.

### Attempt 3

用户又形成新的完整 (T_3)。

所以 baseline 是：

> **re-specify → regenerate**

而不是：

> **revise the existing artifact**

---

# 5. Ours 对应的是 evolving-plan interaction

同样收到相同的新 requirement。

但用户不是重新定义整个 task。

流程是：

### Initial

[
P_1
]

AI 生成第一版 plan。

↓

### Revision 1

用户只修改 allocation。

[
P_1 \rightarrow P_2
]

其他 object-goal relationships 保留。

↓

### Revision 2

再修改 ordering。

[
P_2 \rightarrow P_3
]

allocation 继续保留。

↓

### Revision 3

再添加 spatial constraint。

[
P_3 \rightarrow P_4
]

之前的 semantic decisions 仍然存在。

所以真正的实验 manipulation 是：

> **complete re-specification of an autonomous plan**
>
> vs.
>
> **incremental revision of a persistent plan**

这非常干净。

---

# 6. Task 1：Kitchen Island Sorting

基本 setup：

岛台上放两类物体。

例如：

**Food**

* apple
* banana
* orange

**Tableware**

* cup
* bowl
* plate

目标：

> Food → fridge
> Tableware → sink

这个 task 最适合研究：

> **task allocation strategies**

因为 semantic outcome 不变，但有很多 valid multi-robot solutions。

### Initial task

> Put the apple, banana, and orange in the fridge, and put the cup, bowl, and plate in the sink.

两个 condition 都从完全一样的信息开始。

planner 自动形成 initial assignment。

---

### Variant / Revision 1：distance-based allocation

给用户新的 preference：

> Reorganize the task so that each robot handles the objects closer to its starting position.

这测试：

> allocation revision.

Baseline：

重新写完整 specification。

Ours：

修改当前 assignment，或者通过 language 在现有 plan 上 revision。

---

### Variant / Revision 2：category-based allocation

之后再改变 preference：

> Now have one robot handle all food items and the other handle all tableware.

关键是：

**最终 task outcome 完全没变。**

变的是用户希望 multi-robot team **how** to accomplish it。

这特别符合你的 framing：

> successful autonomous completion is not equivalent to satisfying the user's preferred coordination strategy.

---

### Variant / Revision 3：ordering

再增加：

> Keep the current category-based assignment, but have the food tasks completed before the tableware tasks.

这时候 complexity 开始累积：

* destination 不能丢；
* category assignment 不能丢；
* 新 ordering 要加入。

这里非常适合观察 baseline 的 re-specification burden 和 ours 的 incremental revision。

---

# 7. Task 2：Breakfast Preparation

第二个 task 不再是很多类似物体，而是：

> **不同 source facilities + temporal coordination**

例如需要准备：

* milk：fridge
* cereal：cabinet 1
* bowl：cabinet 2
* spoon：drawer

全部放到 kitchen island 的 breakfast area。

Initial instruction：

> Bring the milk from the fridge, cereal from Cabinet 1, bowl from Cabinet 2, and spoon from the drawer to the breakfast area on the island.

这个任务的优势是 naturally involves：

* several facilities；
* open/close operations；
* route overlap；
* temporal ordering；
* shared spatial resources。

---

### Variant / Revision 1：allocation strategy

例如：

> Divide the task based on where the items are stored: have each robot handle items closer to its side of the kitchen.

或者也可以设计成明确一点：

> Have one robot handle the fridge and nearby storage, and the other handle the remaining cabinets.

主要仍然测试 allocation。

---

### Variant / Revision 2：precedence

再增加：

> Keep the current assignment, but make sure the bowl and spoon arrive before the cereal and milk.

这个非常适合测试：

> task ordering / precedence constraint.

而且用户不应该需要重新指定 milk 从哪里来、cereal 放哪里——那些都是之前已经决定好的。

---

### Variant / Revision 3：coordination constraint

可以再加入一个真正 multi-robot-specific constraint：

> Keep the current task assignment and ordering, but do not let both robots use the shared back corridor at the same time.

这会触发 coordination repair。

Ours 中非常自然：

用户提出 constraint →

system 尝试：

* reorder；
* wait；
* reroute；

同时保留之前明确的 assignment / ordering。

Baseline 则需要把整个 accumulated specification 再表达成一次完整 task。

---

# 8. 两个任务的角色应该区分开

我会让它们分别承担不同的实验意义，而不是两个 task 都测一模一样的东西。

|                      | Sorting                    | Breakfast               |
| -------------------- | -------------------------- | ----------------------- |
| Task character       | repeated parallel subtasks | heterogeneous retrieval |
| 主要 interaction       | allocation                 | ordering + coordination |
| distance strategy    | 强                          | 中                       |
| category strategy    | 强                          | 弱                       |
| precedence           | 中                          | 强                       |
| facility interaction | 中                          | 强                       |
| spatial contention   | 中                          | 强                       |
| conceptual focus     | who does what              | who does what + when    |

这样 reviewer 会感觉两个 scenario 是有设计目的的，而不是为了 counterbalancing 随便换皮。

---

# 9. Experimental flow 最好体现“requirements progressively emerge”

这是整个实验现在最漂亮的地方。

每个 participant 都经历：

### Phase 1 — Initial delegation

给 basic outcome specification。

两个系统都生成 valid plan。

这里我甚至**不期待 ours 有明显优势**。

---

### Phase 2 — Reallocation

用户收到新的 coordination preference。

例如：

> switch to category-based assignment.

开始产生差异。

---

### Phase 3 — Accumulating preference

在保持前面 requirement 的情况下，再增加 ordering。

例如：

> keep that assignment, but change the execution order.

---

### Phase 4 — Coordination constraint

再增加一个不能简单通过 semantic assignment 表达的问题：

> shared corridor / route preference / spatial requirement.

这时候用户实际上是在不断构建：

[
C_1
\rightarrow
C_1+C_2
\rightarrow
C_1+C_2+C_3
]

Baseline 必须不断重新 encode 这个 growing specification。

Ours 则不断 modify one persistent plan。

---

# 10. 这时真正应该测什么

最核心的不是“机器人 execution speed”。

因为 backend planner 可以一样。

真正测：

### Revision performance

* success rate
* completion time
* number of attempts
* number of prompts / edits

### Preservation

这个现在尤其重要：

> **Did the new revision accidentally violate an earlier requirement?**

例如：

新增 ordering 后：

* previous assignment 是否保持；
* destination 是否保持；
* previous route preference 是否保持。

可以定义：

> **prior-constraint preservation rate**

这几乎直接对应你的 conceptual contribution。

---

### Plan understanding

尤其 ours 有 timeline：

* Which robot handles X?
* Which task happens first?
* Where does Robot 1 wait?
* Are both robots using the same facility?

不过这里要意识到：

如果 baseline 没 timeline，comprehension superiority 本身会比较 obvious。

所以我会把 comprehension 当 secondary outcome，而不是全文最主要的 quantitative claim。

---

### Subjective

比较值得问的是：

* perceived control
* confidence that the plan reflects my intent
* ease of making revisions
* difficulty keeping track of prior requirements
* predictability
* mental effort

SUS 可以有，但不应该成为 headline。

---

# 11. 我觉得最重要的 potential finding

我们现在其实已经能预想到一个很合理、而且不要求 ours 全面胜出的 story：

### Initial delegation

两种方式可能差不多。

甚至 baseline 更简单。

### First revision

差异开始出现。

### Second / third revision

随着 requirements 累积：

> baseline specification burden 增长。

而 ours：

> user only needs to author the changed decision.

因此一个很强的 potential conclusion 是：

> **The benefit of co-authoring did not primarily emerge when users first delegated a task, but as they progressively introduced and revised coordination preferences.**

我认为这个结果会非常符合 HCI，而不是系统 benchmark。

---

# 12. 最后把整个论文 story 压成一句流程

### Prior paradigm

**Specify complete task → autonomous planning → execution**

以 EMOS-like systems 为代表。

↓

### HCI gap

真实用户的 coordination preferences 不一定在任务开始前全部形成。

它们可能是在：

> seeing an allocation, inspecting a schedule, or observing an execution

之后才出现。

↓

### Interaction challenge

如果 plan 只是 autonomous system 的内部产物，用户只能：

> mentally reconstruct current state + re-specify the task.

↓

### Our idea

**Make the multi-robot plan a persistent co-authoring artifact.**

用户可以：

> delegate broadly → inspect → selectively take ownership → preserve those decisions → continue delegating the rest.

↓

### Evaluation

使用两个逐步变化的 multi-robot household tasks，比较：

> **one-shot re-specification**
>
> vs.
>
> **progressive plan revision**

并观察随着 coordination requirements 累积，两者在 revision、constraint preservation、understanding 和 perceived control 上有何不同。

---

如果现在让我给这个版本起一个内部 working framing，我会先叫：

> **Progressive Co-Authoring of Multi-Robot Plans**

而不是把 “LLM Agents” 放在最前面。

因为现在真正有 HCI 味道的核心已经不是“大模型帮人控制多个机器人”，而是：

> **把原本一次性委托给 autonomous planner 的 multi-robot task，变成一个用户可以随着需求出现而逐步取得和保留局部 authorship 的过程。**

这个 framing 和你现在两个 task + EMOS-like baseline 已经基本形成闭环了。
