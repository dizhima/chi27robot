# Pipeline:agentic authoring 循环(语义)→ decompose(代码)→ compile

> 供 codex 实现、Claude 验证。**后端 pipeline + CLI 可验;不含前端 UI 接线。**
> 这版取代早先"stage1 force_tool + augment force_tool 两个强制步"的设计:把 grounding 与 augment
> 合并进一个 **agentic tool-calling 循环**(路 Y),augment 变成循环里的**代码工具**。
> 背景见 memory `orchestrator-architecture`。

## 设计原则(从老 stage2 大循环学到的)

老 stage2 循环乱,不是因为工具多,而是**循环伸手进了几何/编译层**(工具里有 compile_plan、在循环里反复
重编步骤级 plan)。所以铁律:

> **authoring 循环的工具全是"语义级"的(augment / propose_plan / 以后的 find_objects…);
> `decompose` 和 `compile` 永远在循环外。** 守住这条,几个工具都不会乱。

## 三层数据流

```
① authoring 循环(agentic,LLM,多轮)   ← 复用 loop.py + provider.chat
     NL ⇄ 模型:接地成 move → 需要时调 augment(代码)补全 → 调 propose_plan 收尾
     产出:AugmentedAction[](= 给用户看/确认的 task plan)
        │  用户确认(确认的是这个增补后的 plan)
② decompose(确定性代码,循环外)
     每个 AugmentedAction → step 模板,一动作一个 AuthoredPlan task,step 打 group=action.id
        │
   compile_plan(现有 skill_service,WorldState 全局)→ schedule + warnings → 回放
```

- **替换**:`__main__` 从"ground(force_tool) → confirm → augment(force_tool) → decompose"改成
  "authoring 循环 → confirm → decompose → compile"。
- ① 里 augment 从"强制 LLM 步"降级为**循环内的代码工具**;grounding + 编排由循环的 `provider.chat` 承担。

## ① authoring 循环

用现成 [loop.py](../src/mujoco_skills/orchestrator/loop.py)(通用 chat→tool_calls→执行→回填循环),挂上
**窄工具集** + AUTHORING_PROMPT。

### 工具集(全语义级,循环内)
1. **`augment(actions)` → AugmentedAction[]**(**确定性代码**,复用/改造现 `decompose` 无关的
   augment 规则逻辑):对传入的动作/move,按规则补全——
   - 每个进容器的 move,若 `facilities[dest].place.requires_open` 非空 → 该 session 一次 `open` 在前;
     进同一容器的多个 move 合成一个 session,**共享一次 open**;有 close 技能则**一次 close** 收尾。
   - `serves`:move 填其陈述意图 id;共享 open/close 填 null。
   - 机器人:同一 session 同一 robot(保守;多机器人并行留后续 ⑤)。
   - 只产 move/open/close;**不产 go_to**(go_to 作为类型接进 schema、③ 能分解,但本步不生成)。
   - **纯函数、可单测**(相同输入→相同输出)。
2. **`propose_plan(actions, message)`**(循环收尾,可用 `force_tool` 收口):参数 = 最终
   `AugmentedAction[]` + 一句 message;这就是**呈现给用户 review/确认的 task plan**。

> 以后往这个工具集加 `find_objects` / `validate` / 答疑 —— 就是 point-3"终极助手"的成长点。
> 但**永远不要**把 `compile_plan`/`decompose` 放进这个循环。

### 状态与多轮
- `current_plan`(当前的 `AugmentedAction[]`,含用户手改)作为**权威状态**喂进上下文;
  AUTHORING_PROMPT 要求"把用户最新请求应用到 current_plan,只改提到的,其余(含手改)不动"。
- manifest 摘要(objects/facilities + 能力位)嵌进上下文(小)。
- 多轮:每轮追加 NL → 循环跑 → 更新 current_plan → propose_plan 呈现。手改 + 再迭代自动一致(靠
  current_plan 当权威),无需任何"冻结/防覆盖"逻辑。

### 终止 / 护栏
- `max_iters` 护栏(loop.py 已有)。
- 收尾条件:模型调用 `propose_plan`(或不再调工具)。
- 接不了地:propose_plan 返回空 actions + reason(什么没接上、有什么可用),不进 ②。

## ② decompose(确定性代码,循环外,基本不变)

每个 AugmentedAction 按 op 套模板(现有 `decompose.py`),step 的 `group`=action.id,一动作一个
AuthoredPlan task:

| op | step 模板(默认末尾 reset) |
|----|----------------------------|
| `move(object,dest)` | navigate(object) → pick(object)[bottle 类:grasp_mode="horizontal",return_to_ready=true] → navigate(dest) → place(object,dest) → reset |
| `open(facility)` | navigate(facility) → `…skills.open` → reset |
| `close(facility)` | reset → navigate(facility) → `…skills.close` → reset |
| `go_to(target/waypoints)` | navigate(waypoints)(仅导航,无抓放) |

reset 是各模板默认收尾(非 op);place 的 kind/access 仍归 compile。

## CLI(`__main__.py`)
`authoring 循环 → 打印 plan / 确认(--yes 跳过)→ decompose → execute("compile_plan", plan)`,打印
schedule/warnings。不再有独立的 force_tool augment 步;augment 只作为循环内工具被模型调用。

## Schema
- `AugmentedAction`(已有,不变):`{id, robot, op:move|open|close|go_to, object?, dest?, facility?, target?, waypoints?, serves?}`。
- `SemanticTask`(move-only)退成 augment 工具的内部输入/可弃用;对外状态是 `AugmentedAction[]`。

## 验收(codex 自测 + Claude 复验)

先清 :8899 端口起新 skill_service。CLI 四类(`--yes`):
1. **study 四类 0 warning**:杯子→水池 / 杯子→drawer_left / 调味瓶→柜子(horizontal grasp) /
   双苹果→冰箱(同 robot 一个 session)。每类:循环产出合理 `AugmentedAction[]`(能看到自动补的
   open/close)、decompose、compile **0 warning**。
2. **augment 是代码、可单测且确定**:直接调 augment 代码工具,相同输入→相同输出。
3. **task↔step 对齐**:每个 step 的 `group` = 某 AugmentedAction id;甘特 task 数 = 动作数。
4. **③ decompose 确定性**:同一 `AugmentedAction[]` 跑两次结果完全一致。
5. **多轮 + 手改一致**:第二轮请求带上 current_plan(含一处手改,如删掉 close)+ 一句新 NL,
   循环只改提到的、保留手改。
6. **接不了地**:propose_plan 空 actions + reason,不进 ②。
7. **循环工具边界**:authoring 循环的工具集里**没有** compile_plan/decompose(代码审查确认)。

## 明确不做(留后续)
- ⑤ step 级 augment(自动插 go_to/wait 去冲突)、多机器人并行同 session、warning 自动消解——不做。
  go_to 仅作类型 + ③ 可分解,① 不生成。
- 前端 UI 接线——不在本文档(见 frontend_stage1_checkpoint.md,其"confirm→stage2 loop"已过时,
  应改为"confirm→decompose+compile")。
- ① 里放 find_objects/答疑等更多工具——本次只 augment + propose_plan,留缝。

## 环境提醒
- ① 用 LLM:需 OPENAI_API_KEY(.env)+ 公司 TLS truststore(启动即注入,照 orchestrator/__main__.py)。
- **Windows 端口坑**:pkill 杀不掉 uv 孙进程;`Get-NetTCPConnection -LocalPort 8899 -State Listen` 拿
  PID、`taskkill /F /T /PID`,循环到空;改服务/skill_generators 后必重启 skill_service。
- 优先离线 `sg.compile_plan` / 直接调 augment、decompose 复现调试,绕开端口坑。

## 完成后报告
新增/改动文件;四类 study 用例的 AugmentedAction[] + compile warning 数;augment 确定性 + ③ 确定性;
多轮+手改一致性;循环工具边界(无 compile/decompose);CLI 是否仍工作;确认没做 ⑤/前端/多机器人并行。
