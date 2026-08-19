# 前端集成 Checkpoint:多轮 stage1 → 语义任务列表

> 供后续实现(自己做或交给 codex/sonnet)。**只 scope 到"自然语言(多轮)→ 语义任务列表(可编辑/确认)"这一层。**
> 展开器(语义任务 → 步骤级 AuthoredPlan)及其之后(编译/纠错/执行、stage2 用 LLM 还是手动)
> **故意留白**——待用户看到 UI 后再定。背景:memory `orchestrator-architecture`、docs/two_stage_llm_design.md。

## 目标与边界

**做**:把两阶段管线的 stage1(NL 接地)接到前端,升级成**多轮会话**,产出并实时更新 UI 里的语义任务列表。
```
用户在 clarifying 阶段的聊天框输入 NL
  ⇅ 多轮:每句 → /ground(带历史+当前任务列表) → 更新后的完整语义任务列表 + 一句回复
  UI 同时可直接编辑任务列表(删/改/重分配)
  满意 → 确认(进入下一层:展开器,本文档不含)
```

**不做(留白)**:展开器、stage2(LLM/代码/手动的选择)、warning 消解接线、执行/回放接线、流式、counters 的 surface place 接线。

## 已确认的决策

1. **多轮**:stage1 从单发升级为会话式(用户觉得单发交互不自然)。
2. **每轮全量替换**:模型每轮重发**完整**当前任务列表(非增量 delta),UI 直接替换;另返回一句 assistant 聊天回复。
3. **无状态端点**:前端(React reducer)持有 `messages` + 当前 `tasks`,每轮一起 POST;服务端只接地这一轮。
4. **A/B 一致性(关键)**:每轮接地都把 **UI 当前的任务列表(含用户手动编辑)当作 ground truth** 喂给模型,模型基于它增改。这样"NL 多轮改(A)"和"UI 直接编辑(B)"永不漂移。
5. 架构走**选项 B**(见 orchestrator-architecture memory 讨论):LLM 只用在 stage1;展开与纠错留到 checkpoint 之后。

## 后端:新增 orchestrator HTTP 服务

stage1 需要 OpenAI + truststore + .env,**不需要 mujoco**;为保持分层(skill_service=几何执行,orchestrator=LLM),**新起一个小服务**,不要塞进 skill_service。

- 位置建议:`src/mujoco_skills/orchestrator/service.py`,`python -m mujoco_skills.orchestrator.service`。
- 端口:`ORCHESTRATOR_PORT`(默认如 8900),与 skill_service(8899)并列。
- 启动时:`load_dotenv()` + `truststore.inject_into_ssl()`(照抄 orchestrator/__main__.py 的处理)。
- manifest 来源:直接读磁盘(与 skill_service 相同路径解析,经 `MUJOCO_REACT_PUBLIC_DIR`),避免跨服务启动依赖。
- 前端**直接调它**(和现在直接调 skill_service /compile_plan 一样);`backend/server.js` 可像 auto-spawn skill_service 那样 auto-spawn 它(参考 server.js 的 skill_service supervisor 段)。

### 端点契约(无状态)

`POST /ground`
```jsonc
// 请求
{
  "messages": [ {"role":"user","content":"把杯子放水池"},
                {"role":"assistant","content":"好的,已加两个杯子…"},
                {"role":"user","content":"牛奶也放冰箱"} ],   // 完整对话转录(NL 层)
  "current_tasks": [ {"id":"t0","action":"move","object":"mug_1","dest":"sink"}, ... ] // UI 当前列表(ground truth)
}
// 响应
{
  "tasks": [ {"id":"t0","action":"move","object":"mug_1","dest":"sink"}, ... ],  // 全量更新后的列表
  "message": "已加上:苹果→冰箱。当前 3 个任务。",                                  // 给聊天框
  "reason": null    // 当 tasks 为空时必填:什么没接上、有什么可用
}
```
- `GET /health` 便于前端探活(照 skill_service 风格)。
- 错误(连不上 OpenAI 等基础设施)才 5xx;"接不了地"是正常结果 → tasks 空 + reason。

## stage1 代码改动

`stage1.ground` 从单发升级(向后兼容 CLI):

```python
@dataclass
class GroundResult:
    tasks: list[SemanticTask]
    message: str            # assistant 聊天回复
    reason: str | None      # tasks 空时说明

def ground(messages: list[dict], current_tasks: list[SemanticTask],
           provider: LLMProvider, manifest: dict) -> GroundResult:
    ...
```
- prompt 组装:`system = STAGE1_PROMPT`(需补规则,见下) → 一段结构化上下文(manifest 的 objects/facilities 摘要,已有 `_manifest_context` + **当前 current_tasks 序列化**作为 ground truth)→ 逐条追加 `messages` 的对话转录 → 强制 `propose_semantic_tasks`。
- `propose_semantic_tasks` 的 schema(schema.py `SEMANTIC_TASKS_SCHEMA`)**新增 `message` 字段**(assistant 回复);`tasks` 仍全量;`reason` 仍在空时必填。
- CLI(`__main__.py`)适配:`ground(messages=[{"role":"user","content":request}], current_tasks=[], ...)`,取 `result.tasks`。单发是多轮的退化情形。

### STAGE1_PROMPT 要补的规则
- 你会收到"当前任务列表(current_tasks)"作为**权威现状**;基于它增/删/改,而不是从零重建也不是只看自己上一轮的输出。
- 每轮输出**完整**的最新任务列表(不是差异)。
- `message` 用一句话说清这轮做了什么(便于用户在聊天里跟进)。
- 其余规则不变(只 `action:"move"`;dest 必须 can_place;"都/all" 展开;接不了地 → 空 tasks + reason;不臆造、不猜坐标)。

## 类型/词表映射(前端)

后端 `SemanticTask{id, action:"move", object, dest}` ↔ UI `ObjectGoalTask{id, type:"object_goal", object:SceneRef, relation, target:SceneRef, assignee}`([frontend/src/authoring/types.ts](../frontend/src/authoring/types.ts)):

| UI 字段 | 从哪来 |
|---------|--------|
| `object: SceneRef{name, bodyId}` | `SemanticTask.object`(name);bodyId 从 manifest `objects[name].body` 解析(拿不到可先置 0/占位,UI 不依赖它做接地) |
| `relation: "inside"｜"on"` | 由 manifest `facilities[dest].place.kind` 推:`container`→`inside`、`surface`→`on` |
| `target: SceneRef{name, bodyId}` | dest facility(name + `facilities[dest].body`) |
| `assignee: robot_a｜robot_b` | **默认值**:轮流分配(task 0→robot_a、task 1→robot_b…)或全 robot_a;用户在 UI 重分配 |

- **机器人命名**:后端 `robot0/robot1` ↔ UI `robot_a/robot_b`。集中在一处映射函数,别散落。
- `SemanticTask` 目前无 assignee;分配是**前端职责**(stage1 不管),这也是把它作为默认值的原因。

## UI 接线(clarifying 阶段)

现有 authoring 有 `clarifying → intent_review → plan_review → plan_confirmed` 和 `ObjectGoalTask[]`、删/改/重分配 action。改动:

- **clarifying 阶段** = 聊天框 + 实时任务列表。新增 reducer action(示意):
  - `send_message(text)`:把 user 消息追加进 `state.messages` → 调 `/ground`(带 `messages` + 当前 `tasks` 映射回 `SemanticTask`)→ 用返回的 `tasks` **替换** `state.tasks`、把 `message` 追加为 assistant 消息。
  - 现有 `delete_task`/`reassign_task`/`edit target` 继续直接改 `state.tasks`;**下一次** `send_message` 会把改过的列表当 current_tasks 发出去(A/B 一致)。
- **取代 Codex PTY**:这个 NL 输入流原来指向 backend 的 codex 终端([server.js](../backend/server.js) 的 `/api/terminal/*`);改为指向 `/ground`。codex 终端可保留为调试通道或移除。
- **确认**:用户满意后 confirm,离开 clarifying,进入下一层(展开器——本文档不含,先只需一个占位/桩)。
- 流式:先不做(聊天回复一次性返回);以后接前端流式时再增量加(不推翻本设计)。

## 验收

后端:
- `POST /ground` 单轮:一句 NL → 合理 tasks + message。
- 多轮:发两轮(第二轮"牛奶也放冰箱"),第二轮请求带上第一轮的 messages + current_tasks → 返回**含新增项的全量列表**。
- A/B 一致:current_tasks 里删掉一个任务后再发一轮 NL("再把 X 放 Y")→ 返回列表同时反映"删除"和"新增"。
- 接不了地("把电视放冰箱"):tasks 空 + reason/message 说明。
- CLI `python -m mujoco_skills.orchestrator --yes "把杯子都放到水池里"` 仍工作(单发退化)。

前端:
- clarifying 输入 NL → 任务列表出现;再输入 refine → 列表更新;手动删一个再 NL → 一致不漂移;confirm 能进入下一阶段(桩)。

## 环境提醒
- 新 orchestrator 服务需 `OPENAI_API_KEY`(.env)+ truststore(公司 TLS);启动即注入。
- **Windows 端口坑**:同样适用于新服务端口——`Get-NetTCPConnection -LocalPort <port> -State Listen` 拿 PID、`taskkill /F /T`,循环到空,否则连到旧进程。
- 改了 orchestrator 服务代码后重启该服务。

## 完成后报告
新增/改动文件、`/ground` 三项验收(单轮/多轮/AB 一致)结果、CLI 是否仍工作、前端 clarifying 演示、以及有没有触及展开器及之后(不应该)。
