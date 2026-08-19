# `propose_plan.actions` 删除方案与 authoring 终止故障交接

## 1. 文档目的

本文记录 sorting 场景中“新增任务成功，但带 before/after 的新增任务失败”问题的根因，并给出删除 `propose_plan.actions` 的建议实现方案，供后续会话直接修改代码。

本文只讨论语义 authoring 阶段，不修改 `compile_plan`、decompose、调度器或 MuJoCo 执行层。

## 2. 用户可见问题

初始计划包含：

- robot0 把 `apple_1` 放进 `fridge`，并共享 fridge 的 open/close session；
- robot1 把 `banana_1` 放进 `sink`。

用户随后要求：

```text
make robot 1 move banana 2 to the fridge after
robot1 · banana_1 -> sink (plan task t1)
```

观察结果：

- 直接新增 `banana_2 -> fridge` 可以成功；
- 新增后再要求它位于 `banana_1 -> sink` 之前或之后，会显示：

```text
authoring loop ended without a valid propose_plan call
```

`revise_order` 的 session 处理已经具备以下行为：

- 同机器人、顺序已经满足时，before/after 是成功的 no-op；
- 跨越 container session 边界时，fridge 的 open/move/close 作为一个整体移动；
- 不能为了满足外部排序而只抽出共享 fridge session 中的一条 move。

因此目前剩余故障不是 `revise_order` 本身，而是 mutation 完成后的最终提交协议。

## 3. 当前 authoring 数据流

相关文件：

- `src/mujoco_skills/orchestrator/authoring.py`
- `src/mujoco_skills/orchestrator/schema.py`
- `src/mujoco_skills/orchestrator/prompt.py`
- `src/mujoco_skills/orchestrator/loop.py`
- `tests/test_authoring.py`

当前流程：

```text
current_plan
    |
    v
_AuthoringExecutor.working_actions  <--- authoritative mutable state
    ^             ^             ^
    |             |             |
 augment       reassign     revise_order
    |
    v
LLM 再通过 propose_plan(actions=[完整最终计划], message, reason)
回传一份完整 actions 副本
    |
    v
executor 将副本与 working_actions 做严格逐项比较
    |
    +-- 完全相同：生成 AuthoringResult
    |
    +-- 不同：返回 {"error": "..."}
```

`working_actions` 已经是服务端的权威状态。`augment`、`reassign` 和 `revise_order` 都通过确定性代码修改它。模型在 `propose_plan` 中再次提交完整 `actions`，实际形成了第二份、可能过期的最终状态。

## 4. 根因

### 4.1 `actions` 是重复状态，且容易与 mutation 后状态不一致

`_AuthoringExecutor.execute("propose_plan")` 当前要求模型回传的 actions：

1. 数量与 `working_actions` 完全相同；
2. id 顺序完全相同；
3. 每个 action 的 `_action_key` 完全相同；
4. 依赖关系满足校验。

带排序的新增任务通常需要：

```text
augment -> reassign（可选）-> revise_order -> propose_plan
```

`revise_order` 可能移动整个 fridge session，并更新跨机器人 `after` 依赖。模型如果在最终 `propose_plan` 中仍回显修改前的顺序、自己推测的顺序，或额外手写 `after`，就会与 executor 中已经正确更新的 `working_actions` 不一致，proposal 被拒绝。

直接新增更容易成功，是因为它需要模型镜像的变化较简单；这不代表当前提交协议可靠。

### 4.2 失败的 terminal tool 仍会终止 loop

`loop.run()` 当前只检查工具名称是否属于 `terminal_tools`：

```python
if terminal_tools and call.name in terminal_tools:
    return msgs
```

它不检查工具结果是否包含 `error`。因此：

1. 模型调用 `propose_plan`；
2. executor 返回 `{ "error": ... }`；
3. loop 因工具名是 terminal 立即退出；
4. `executor.result` 仍为 `None`；
5. force-tool recovery 可能再次提交相同的错误 payload；
6. 最终只向 UI 暴露统一错误：`authoring loop ended without a valid propose_plan call`。

这也掩盖了真正的 proposal mismatch 信息。

## 5. 结论：`actions` 是否冗余

对“确定最终计划内容”这项职责而言，`propose_plan.actions` 是冗余的。最终 actions 应直接来自服务端 `working_actions`，模型不应拥有第二条任意改写或完整镜像计划的路径。

但该字段目前还兼职承担三个职责，不能只从 schema 中机械删除：

1. **pin overlay 传递**：模型目前可在最终 action 副本上增加 `place_at_pin`；
2. **空计划/无法接地表达**：使用 `actions=[]` 配合非空 `reason`；
3. **完整计划一致性屏障**：阻止模型绕过 mutation tools 偷渡 action 修改。

删除字段时必须分别迁移这三个职责。

## 6. 建议目标设计

### 6.1 最终工具只负责提交，不负责重述计划

可以先保留工具名 `propose_plan` 以降低迁移面，也可以后续将其重命名为语义更准确的 `finalize_plan`。建议最终参数为：

```json
{
  "status": "committed",
  "message": "...",
  "reason": null
}
```

无法接地时：

```json
{
  "status": "ungroundable",
  "message": "场景中没有电视。",
  "reason": "找不到电视；可用对象不包含电视。"
}
```

推荐约束：

- `status=committed` 时 `reason` 必须为 `null`；
- `status=ungroundable` 时 `reason` 必须为非空字符串；
- finalizer 不接受 `actions`，也不解析模型生成的 actions；
- 成功时 `AuthoringResult.actions = list(executor.working_actions)`；
- 提交前仍运行服务端最终 invariant 校验。

如果希望做最小兼容改动，可以第一阶段保持：

```json
{
  "message": "...",
  "reason": null
}
```

并由 `reason is None` 推断 committed；但显式 `status` 能更清楚地区分“成功 no-op”“无法接地”和未来可能加入的其他结果。

### 6.2 所有计划修改只能经过确定性 mutation tools

删除 actions 后必须继续维持以下安全边界：

- 新增或改变目的地：`augment`；
- 改机器人：`reassign`；
- 改顺序或依赖：`revise_order`；
- pin：迁移到独立、确定性的 mutation 接口；
- finalizer：只校验和提交，绝不修改 `working_actions`。

这样删除完整 actions 回显不会削弱“禁止模型偷渡计划修改”的保护，反而让该边界更明确。

### 6.3 pin 必须先迁移

当前 `place_at_pin` 被有意排除在 `_action_key` 外，并允许模型在 `propose_plan.actions` 中作为当轮 overlay 添加。删除 actions 后，推荐增加窄工具，例如：

```json
set_place_pin({
  "action_id": "move_mug_1_sink",
  "pin": "p1"
})
```

executor 应验证：

- action 存在且 `op == "move"`；
- pin 出现在本轮 `scene_refs`；
- pin 绑定 facility 与 action.dest 相同；
- 更新后的 `place_at_pin` 直接写入 `working_actions` 和 `action_by_id`；
- 已从 prior turn 携带的 pin 无需本轮 scene ref 再验证；
- 清除 pin 如有需求，应使用显式 `pin: null` 或独立 clear 操作。

也可以把 `place_at_pin` 放入 `augment` 的 move intent，但这只能覆盖新 action，不能覆盖给既有 move 追加/修改 pin 的情况，因此独立工具更完整。

### 6.4 只有成功 finalizer 才能终止 authoring loop

必须修复 `loop.run()` 的 terminal 判定。最低要求：

```python
is_error = isinstance(result, dict) and bool(result.get("error"))
if terminal_tools and call.name in terminal_tools and not is_error:
    return msgs
```

更稳妥的接口是让 executor 返回带明确状态的结构，避免通用 loop 猜测任意字典：

```json
{
  "ok": true,
  "terminal": true,
  "actions": ["server-side finalized result"],
  "message": "..."
}
```

失败的 finalizer 结果必须写回模型上下文，让模型有机会纠正 `message/status/reason` 或先完成缺失的 mutation。force-tool recovery 也不能在存在未处理 mutation error 时盲目提交旧状态。

## 7. 必须防范的风险

### 7.1 mutation 失败后误提交旧计划

例如 `revise_order` 返回 error，模型随后调用无 actions 的 finalizer。如果 finalizer 只取当前 `working_actions`，它可能把未排序的旧状态作为成功结果提交。

建议 executor 跟踪本轮工具结果：

- `unresolved_error`：最近是否存在尚未纠正的 mutation error；
- `mutation_log`：每次 mutation 的类型、目标、changed/no-op/error；
- finalizer 在 `unresolved_error` 存在时拒绝 committed；
- 合法 no-op（例如顺序本来已满足）记录为 success/no-op，而不是 error。

### 7.2 一轮中的部分修改被提交

例如 augment 成功、reassign 成功、revise_order 失败。当前 mutation 会逐步留在 `working_actions` 中。若产品语义要求“一条自然语言请求原子完成”，应：

- 在 turn 开始保存 `initial_actions` 快照；
- 只有 final validation 成功才发布结果；
- 无法完成整条请求时回滚，或明确返回 partial result 并要求用户确认。

推荐默认采用整轮事务语义，避免 UI 声称失败但下一轮 current_plan 已混入部分变更。

### 7.3 message 与真实修改不一致

模型仍可能在 `message` 中声称“banana_2 已移到 banana_1 后面”，但 mutation 实际是 no-op 或失败。

建议至少保留结构化 `mutation_log` 并用它校验/生成回显。更强的方案是服务端从 `initial_actions` 与 `working_actions` diff 生成事实摘要，模型只提供自然语言润色。

### 7.4 空计划语义混淆

删除 `actions=[]` 后必须区分：

- current_plan 本来为空，且请求无法接地；
- current_plan 非空，本轮请求无法接地，应保留旧计划；
- 用户明确要求清空计划；
- 请求已满足，因此是成功 no-op。

不要用 `reason` 是否为空同时承载所有含义。显式 `status`，以及必要时独立的 clear mutation，可避免歧义。

### 7.5 并发/过期计划

如果 authoring 期间 UI 或其他请求修改了 current plan，executor 的快照可能过期。建议 finalizer 校验输入 plan revision/turn id，使用 optimistic concurrency，避免旧 turn 覆盖新计划。

### 7.6 可观测性下降

删除模型回传 actions 后，不再能比较“模型认为的最终计划”和服务端计划。但这份比较本身不是正确性来源。应改为记录：

- initial plan revision/hash；
- 每次 mutation 参数和结果；
- final working plan revision/hash；
- finalizer 的 status/reason；
- 原始失败原因，UI 不应只显示统一错误。

## 8. 推荐实施顺序

### Phase A：先修 terminal error 行为

1. 修改 `loop.run()`：terminal tool 只有执行成功才终止。
2. 确保失败结果继续反馈给模型。
3. 保留最终真实 error，避免只抛统一错误。
4. 为“失败 propose_plan 不终止，下一次正确 propose_plan 成功”添加测试。

这一步可独立降低当前故障的不可诊断性。

### Phase B：迁移 pin mutation

1. 新增 `set_place_pin` schema/tool/executor 分支；
2. 更新 prompt 的 pin 规则；
3. 把现有 pin acceptance/rejection/carried-pin 测试迁移到新工具；
4. 确认 follow-up turn 不要求重新提供已持久化 pin。

### Phase C：删除 `propose_plan.actions`

1. 修改 `PROPOSE_PLAN_SCHEMA`，删除 `actions`，加入 `status`（推荐）；
2. 修改 tool description 和 `AUTHORING_PROMPT`，禁止模型回显完整计划；
3. 删除 `_parse_plan_actions()` 在 finalizer 路径上的使用；如果没有其他调用，再评估是否删除函数；
4. 删除 unit-test-only 的 `allowed_action_keys` proposal 注入兼容路径；测试应通过真实 mutation tools 构造 working state；
5. finalizer 从 `working_actions` 构造 `AuthoringResult`；
6. 提交前运行 dependency/order/manifest 等最终校验；
7. 更新 force-tool closing prompt。

### Phase D：加入 turn 事务与 mutation ledger

1. 保存 initial snapshot；
2. 记录 mutation success/no-op/error；
3. committed finalizer 拒绝 unresolved error；
4. 根据产品决定失败时 rollback 或明确 partial 状态；
5. 用 ledger/diff 约束最终 message。

如果需要尽快解决线上排序失败，A+B+C 是最低完整修复；D 是建议同时完成的可靠性加固。

## 9. 测试与验收标准

### 9.1 核心回归

1. 初始计划：apple_1 -> fridge，banana_1 -> sink。
2. 新增 banana_2 -> fridge。
3. 指定 banana_2 在 banana_1 之后：成功，整个 fridge session 位于 banana_1 后。
4. 指定 banana_2 在 banana_1 之前且原顺序已满足：成功 no-op。
5. UI 不出现 `authoring loop ended without a valid propose_plan call`。

### 9.2 finalizer 安全性

- finalizer schema 不接受 `actions`；
- 模型无法通过 finalizer 新增、删除、改机器人、改顺序或改依赖；
- 未调用 mutation tool 时，finalizer 只能提交未变化的 working state；
- mutation error 未解决时，committed finalizer 被拒绝且 loop 不终止；
- 合法 no-op 可成功 finalize；
- 最终 `AuthoringResult.actions` 与 executor `working_actions` 相同。

### 9.3 pin 回归

- 新 move 可以绑定本轮已知且 facility 匹配的 pin；
- 未知 pin 被拒绝；
- facility 不匹配被拒绝；
- 非 move action 绑定 pin 被拒绝；
- prior-turn pin 在没有本轮 scene ref 时仍被保留；
- 既有未绑定 move 不能凭空绑定一个未在本轮出现的 pin。

### 9.4 空计划和无法接地

- 空 current_plan + 无法接地：返回 ungroundable，actions 仍为空；
- 非空 current_plan + 无法接地：返回 ungroundable，但保留现有 actions；
- 已满足请求：返回 committed/no-op，不应误判 ungroundable；
- 明确清空请求与无法接地拥有不同 mutation/status 语义。

### 9.5 建议运行的测试

至少运行：

```text
tests/test_authoring.py
tests/test_compound_turn.py
tests/test_conversation_stream.py
tests/test_phase4_classifier_explain_routing.py
```

同时搜索并迁移所有构造 `propose_plan(actions=...)` 的 mock、fixture、prompt 文档和 provider tests。

## 10. 需要更新的文档/兼容面

除代码和测试外，仓库中以下文档仍描述 `propose_plan(actions, message)` 或完整 plan 回显，应在实现后同步：

- `docs/pipeline_augment_decompose.md`
- `docs/phase0_unified_conversation_contracts.md`
- `docs/phase2_spec_ndjson_author_progress.md`
- `docs/unified_conversation_author_resolver_design.md`
- `frontend/src/plan/authorPlan.ts` 中相关注释

工具名如果从 `propose_plan` 改成 `finalize_plan`，还需同步 conversation progress stage、stream tests 和 UI 文案。为减少一次性改动，建议第一版先保留工具名，只改变参数和 executor 语义；稳定后再单独重命名。

## 11. 非目标

本次不应顺带修改：

- shared facility 的物理碰撞判定；
- sink/fridge placement slots；
- 调度器对 facility interval 的共享判断；
- decompose/compile 输出；
- 播放速度 UI；
- MuJoCo 执行和轨迹。

## 12. 最终决策摘要

删除 `propose_plan.actions` 是合理方向，因为 `working_actions` 已是服务端权威状态。正确修复不是单独删除 schema 字段，而是完成以下闭环：

```text
所有修改只经确定性 tools
        |
        v
server-side working_actions
        |
        +-- final validation
        +-- unresolved-error / transaction check
        +-- revision check
        |
        v
无 actions 参数的 successful finalizer
        |
        v
AuthoringResult(actions=working_actions)
```

同时必须迁移 pin overlay、显式表达 ungroundable/no-op，并保证失败的 terminal tool 不会终止 loop。这样才能从根本上消除“mutation 已成功，但模型回显的完整 actions 不一致，导致整轮失败”的问题。
