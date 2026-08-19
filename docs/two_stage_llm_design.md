# 第 5 步:两阶段 LLM 流程 设计

> 本文档自包含,供**新 Sonnet 会话**实现。前置(第 1-4 步)已完成并验收:
> manifest 已是名词/动词四段结构(见 docs/manifest_redesign.md),orchestrator 骨架已跑通
> (见 memory `orchestrator-architecture`)。**本步只做 CLI、非流式、接地方案 A。**

## 目标

把当前"自然语言 → 直接 compile"的**单阶段**流程,拆成**两阶段 + 中间人工确认**:

```
自然语言目标
  │  ① 语义接地(A: LLM 直读 manifest)
  ▼
语义任务列表 [{object, dest}, ...]      ← 打印给用户确认 / 可拒绝
  │  ② 技能分解(LLM: 展开 navigate-pick-navigate-place + compile + 消解 warnings)
  ▼
schedule
```

**为什么两阶段**:语义错(认错物体)和分解错(步骤/冲突)是两类问题;先在便宜的语义层让用户纠偏,
不必等 compile 完才发现物体搞错。也和前端已有的 `intent_review → 语义任务 → confirm → 执行`
阶段对齐([AuthoringPanel.tsx](../frontend/src/authoring/AuthoringPanel.tsx))。

## 已锁定的决策(不要再改)

1. **接地用方案 A**:阶段①用 LLM 直读 manifest(objects 6 个 + facilities 9 个,很小,直接进上下文)。
2. **给 B 留缝**:阶段①做成"输入 NL → 输出结构化语义任务"的**黑盒**。将来加 `find_objects` 几何
   检索工具时,只在**阶段①内部**插入,不动阶段②和整体流程。**接口先定死,实现先用 A。**
3. **无混合实体**:objects / facilities 两段分类保持,不合并。
4. **CLI + 非流式**;人工确认在 CLI 内联完成(见下)。

## 数据结构(先定型)

在 `orchestrator/schema.py` 增加:

```python
@dataclass
class SemanticTask:
    id: str                 # 稳定 id,供确认/编辑/依赖引用
    action: str             # 先只支持 "move"(以后可加 "open"/"close" 等 articulation)
    object: str             # objects 段里的 name,如 "mug_1"
    dest: str               # facilities 段里 place != null 的 name,如 "sink"
```

阶段①的输出 = `list[SemanticTask]`。对应的 JSON Schema(供强制结构化输出):

```jsonc
{ "type":"object", "properties": { "tasks": { "type":"array", "items": {
    "type":"object",
    "properties": {
      "action": {"type":"string","enum":["move"]},
      "object": {"type":"string"},
      "dest":   {"type":"string"}
    }, "required":["action","object","dest"], "additionalProperties": false
}}}, "required":["tasks"], "additionalProperties": false }
```

## 阶段①:语义接地(新增 `orchestrator/stage1.py`)

**签名(这就是给 B 留的缝——保持不变)**:
```python
def ground(user_msg: str, provider: LLMProvider, manifest: dict) -> list[SemanticTask]:
    ...
```

**A 版实现**:
- 一次 LLM 调用,把 manifest 的 `objects`(label / home_facility / world_pos)和 `facilities`
  (name / label / 能力字段)**直接嵌进 system prompt 上下文**(数据小,免一次 get_manifest 往返)。
- 用**强制结构化输出**拿 `{tasks:[...]}`:定义一个工具 `propose_semantic_tasks`(schema 同上),
  强制模型调用它;复用现有 provider 的 tool-calling + schema 校验路径(见 `schema.to_openai`)。
- 阶段①的 system prompt(新增到 `prompt.py`,如 `STAGE1_PROMPT`)要求:
  - 只用 manifest 里存在的 object/facility 名;把用户词映射到 `label`(如"杯子"→label "mug"
    → mug_1/mug_2);
  - "都/all" 要展开成多个 task;
  - **无法接地就返回空 tasks + 一句原因**(不要瞎编物体/坐标)——阶段①负责"能不能做"的判定。

**将来 B**:在 `ground()` 内部改为给模型一个 `find_objects(label=?, on=?, near=?)` 工具做几何/关系
检索,再产出 tasks。`ground()` 的签名和返回类型不变,`__main__`/阶段② **零改动**。

## 人工确认(在 `__main__.py` 内联)

`ground()` 拿到 tasks 后:
- 打印语义任务列表给用户看(带序号,如 `1. move mug_1 -> sink`);
- 若 tasks 为空 → 打印阶段①给的原因,结束(不进阶段②);
- 提示确认:`[Enter]=确认 / e=编辑 / q=取消`。
  - 确认 → 进阶段②;
  - 取消 → 退出;
  - 编辑 → 最小实现:允许用户删除某几条(输入要删的序号)。完整的自然语言改写可留到前端阶段。

> 这是当前 one-shot CLI 里唯一的交互点。实现成"跑一次命令 → 中途一个确认 prompt → 出结果",
> 不要拆成两条子命令(除非你觉得更顺;若拆,`plan` 出 tasks JSON、`compile` 吃确认后的 tasks)。

## 阶段②:技能分解(复用现有 loop)

输入 = 确认后的 `list[SemanticTask]`。做的事和**当前单阶段 orchestrator 几乎一样**,只是种子从
原始 NL 变成结构化任务:

- 把每个 `move(object, dest)` 展开为步骤模板:`navigate(object) → pick(object) → navigate(dest)
  → place(object, dest)`(顺序见 manifest_redesign / 现有 prompt 的 "Authoring a move" 规则)。
- 多个 task 可跨 robot0/robot1 并行 → 由 LLM 分配机器人 + 处理 `after` 依赖 + 消解 compile 的
  `warnings`(现有 loop 已支持,`skill_client.execute` 打 compile_plan)。
- 复用现有 `loop.run(...)` + `providers` + `skill_client`;阶段②的 system prompt = 现有 prompt.py
  内容(改名 `STAGE2_PROMPT`,保留"先 get_manifest / 只语义步骤 / 消解 warnings"等规则)。

实现上两种做法任选(推荐前者):
- **(推荐)** 把确认后的 tasks 序列化成一句明确指令喂给现有 `loop.run`(如"执行以下已确认语义任务:
  move mug_1→sink; move mug_2→sink。按 navigate-pick-navigate-place 展开并消解冲突。"),让阶段②
  LLM 负责展开 + 分配 + compile;
- 或在代码里**确定性展开**成 compile_plan 的 `plan` 结构,只让 LLM 做机器人分配/冲突消解。更可控
  但更多代码。

## 文件改动清单

| 文件 | 改动 |
|------|------|
| `orchestrator/schema.py` | +`SemanticTask` dataclass + 其 JSON Schema |
| `orchestrator/stage1.py`(新) | `ground(user_msg, provider, manifest) -> list[SemanticTask]`(A 版) |
| `orchestrator/prompt.py` | +`STAGE1_PROMPT`;现有内容改名 `STAGE2_PROMPT` |
| `orchestrator/__main__.py` | 编排:get_manifest → ground → 打印+确认 → 阶段② loop |
| (不动) `loop.py` / `providers/` / `skill_client.py` | 阶段②直接复用 |

## 验收点(实现完让 Sonnet 自测)

1. **可行 + 复数**:`"把杯子都放到水池里"` → 阶段①产出 **2 条** task(mug_1→sink、mug_2→sink),
   打印确认;确认后阶段②编译出无 warning 的 schedule(理想情况下两台机器人并行)。
2. **不可行**:`"把水果放进冰箱"` → 阶段①判定无法接地(没有 place-to-fridge 能力)→ 打印原因、
   **不进阶段②**。注:apple_1 是 object 且存在,但 fridge 的 `place` 为 null,所以是"目的地无放置
   能力",阶段①要能说清这点。
3. **取消路径**:确认时输入 q → 干净退出,不调 compile。
4. **留缝检查**:`ground()` 签名是 `(user_msg, provider, manifest) -> list[SemanticTask]`,阶段②
   完全不知道接地是怎么做的(黑盒)。

## 明确不做(本步范围外)

- 不接前端(仍 CLI)、不做流式。
- 不实现 `find_objects`(B)——只保证 `ground()` 的接口能容纳它。
- 不做完整的自然语言编辑(确认阶段只需支持删除/取消)。
- articulation 任务(open/close 冰箱等)先不纳入语义任务;`action` 先只有 `"move"`。

## 环境提醒(给 Sonnet)

- 需 `OPENAI_API_KEY`(从 `.env` 读)+ 公司网络 TLS 要 `truststore.inject_into_ssl()`(已在
  `__main__.py`,见 memory `corporate-tls-truststore`)。
- 端到端要先起 skill_service:
  `uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service`
- **Windows 坑**:`pkill -f skill_service` 杀不掉 uv 的孙进程;要按端口清:
  `Get-NetTCPConnection -LocalPort 8899` 拿 PID 再 `taskkill /F /T /PID <pid>`,否则会连到旧进程。
