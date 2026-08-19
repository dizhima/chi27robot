# 项目重构 + Python LLM 编排接入 方案

> 本文档自包含,供在**新会话/新 agent** 中执行。分两部分:
> **A. 目录重构**(先做,独立会话完成)→ **B. LLM 编排接入**(重构后回来做)。
>
> 决策已定:
> - 编排循环用 **Python**。
> - provider 层要**可切换抽象**,当前只有 **OpenAI API key**,先实现 OpenAI,预留 Claude/Gemini。
> - `mujoco_react/` 前端**合并进主仓库**(删其嵌套 `.git`)。
> - 实验代码统一收进 `experiments/`。

---

## 0. 现状诊断(重构前的事实,便于校验)

三类东西混在一个平面上:

| 类别 | 现在位置 | 性质 |
|------|---------|------|
| 生产管线(要被 LLM 编排) | `tools/` | skill_generators / skill_service / extract / build_manifest / calibrate / bake |
| 前端 + 后端桥 | `mujoco_react/` | **自己是独立 git repo(嵌套)** |
| 实验/原型 | `franka_pick/`, `simple_warehouse/`, `test_warehouse/`, `vuer_test/`, 根目录 `stretch_*.py/.xml` | 一次性探索 |

两个结构性硬伤:

1. **`mujoco_react/` 是嵌套 git 仓库**(非 submodule),外层 repo 追踪不到其内容。
2. **`tools/` 不是 Python 包**:无 `__init__.py`;`skill_service.py` 用 `sys.path.insert` 硬塞路径来 `import skill_generators`。

### Git 现状(重要,执行前已核实)

- 主仓库 `mujoco-skill-playground/` → `origin: https://github.com/dizhima/coro.git`,1 commit,`tools/`/`mujoco_react/` 等大多 **untracked**。
- `mujoco_react/` → **同一个** `origin: dizhima/coro.git`,1 commit(已推送),**19 个未提交工作区改动**。
- 结论:`coro` 本就该是唯一 repo,现被切成两个嵌套仓库。合并是收拢,不是分裂。
- 合并的历史损失 = mujoco_react 那 1 个 commit(同远程,基本无影响);**19 个未提交改动是工作区文件,删 `.git` 不影响文件本身**,合并后可一次性 commit 进 coro。

### 现有 skill 函数入口(已确认可 import,包 tool 成本极低)

`tools/skill_generators.py`:
- `compile_plan(scene_xml, plans, standoffs_path, tracks_dir, manifest_path)` — 约 :996
- `standoff_for_point(rig, target_xy, working_q=None, ...)` — 约 :674
- `get_rig(scene_xml, robot, ready=None, island=None)` — 约 :792
- `load_ready(tracks_dir)` — 约 :448
- `write_generated_tracks(result, tracks_dir)` — 约 :804

`tools/skill_service.py`:warm-load 后暴露 HTTP(默认 :8899):
- `POST /compile_plan {plan}` → schedule + warnings + 落盘 tracks
- `POST /standoff {target_xy}` → 可行站位
- `GET /manifest` → 词表 / `GET /health`

`tools/llm_tools.json`:已按 **OpenAI function 格式** 写好 3 个 tool(`get_manifest` / `standoff_for_point` / `compile_plan`),参数 schema 通用,仅外层 `{type:"function", function:{...}}` 是 OpenAI 专属。

### 当前"LLM"形态 = Codex CLI 终端(要被替换的东西)

`mujoco_react/backend/server.js` 把 **Codex CLI 作为 PTY 子进程** spawn(`command = "codex"`,约 :22),通过 SSE 把终端流推给前端 "Codex CLI" 面板,人肉驱动 `$cosim` skill。**全仓库 grep `openai/anthropic/genai/langchain` 零命中——没有任何程序化 tool-calling 循环。** B 部分要新建的就是它,并替换这个 Codex 子进程。

---

## A. 目录重构

### 目标结构

```
mujoco-skill-playground/          # 单一 git repo,origin=dizhima/coro.git
├── pyproject.toml                # 声明 src/ 包
├── src/
│   └── mujoco_skills/            # ← tools/ 升级成正式包
│       ├── __init__.py
│       ├── skills/               # skill_generators.py(compile_plan / standoff / rig)
│       ├── pipeline/             # extract_skill_tracks / build_skills_manifest / calibrate_standoffs / bake_study_init / generate_livingroom_scene
│       ├── service/
│       │   ├── skill_service.py  # HTTP 执行层
│       │   └── llm_tools.json    # tool schema 跟执行层走
│       └── orchestrator/         # ★ B 部分新建:编排循环 + provider 抽象
│           ├── __init__.py
│           ├── loop.py
│           ├── schema.py
│           └── providers/
├── frontend/                     # ← mujoco_react/frontend
├── backend/                      # ← mujoco_react/backend(codex 子进程将被 orchestrator 替换)
├── experiments/                  # franka_pick / simple_warehouse / test_warehouse / vuer_test / stretch/
├── assets/                       # 不动
└── docs/                         # 不动
```

### 阶段 0 — 兜底(不可省)

```bash
# 1) 全量快照到 scratchpad(路径按你当前会话的 scratchpad 调整)
tar czf "$SCRATCH/pre_restructure_snapshot.tgz" \
  --exclude='**/node_modules' --exclude='**/.venv' --exclude='**/__pycache__' \
  --exclude='**/dist' .

# 2) 记录 mujoco_react 的 19 个未提交改动清单(仅记录,文件不动)
cd mujoco_react && git status --porcelain > "$SCRATCH/mujoco_react_uncommitted.txt" && cd ..
```

### 阶段 1 — 解嵌套 git

```bash
# 前端文件全部保留,只删其独立 repo 身份
rm -rf mujoco_react/.git
git status   # 现在应能看到 mujoco_react/ 全部内容
```

### 阶段 2 — 目录搬迁

用 `git mv`(若文件已 tracked)或普通 `mv` + `git add`:

```bash
mkdir -p experiments/stretch src

mv mujoco_react/frontend frontend
mv mujoco_react/backend  backend
# mujoco_react/ 里若还有 public/assets 等前端资产,一并并入 frontend/ 对应位置
rmdir mujoco_react 2>/dev/null || echo "mujoco_react 还有残留,手动检查"

mv franka_pick simple_warehouse test_warehouse vuer_test experiments/
mv stretch_cup_to_tray.py stretch_manipulation*.xml stretch_manipulation_views experiments/stretch/
```

> ⚠️ 前端 build 配置(vite 根目录 / `MUJOCO_REACT_PUBLIC_DIR` 等环境变量默认值)可能引用旧路径,阶段 4 验证时一并修。

### 阶段 3 — Python 包化

```bash
mkdir -p src/mujoco_skills/{skills,pipeline,service,orchestrator/providers}

mv tools/skill_generators.py                     src/mujoco_skills/skills/
mv tools/extract_skill_tracks.py \
   tools/build_skills_manifest.py \
   tools/calibrate_standoffs.py \
   tools/bake_study_init.py \
   tools/generate_livingroom_scene.py            src/mujoco_skills/pipeline/
mv tools/skill_service.py tools/llm_tools.json   src/mujoco_skills/service/
rm -rf tools/__pycache__ && rmdir tools 2>/dev/null || true

# 每个包目录加 __init__.py
touch src/mujoco_skills/__init__.py \
      src/mujoco_skills/skills/__init__.py \
      src/mujoco_skills/pipeline/__init__.py \
      src/mujoco_skills/service/__init__.py \
      src/mujoco_skills/orchestrator/__init__.py \
      src/mujoco_skills/orchestrator/providers/__init__.py
```

`pyproject.toml` 增加包声明(用 hatchling 或 setuptools;示例 setuptools):

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

**代码内需要改的引用:**

1. `src/mujoco_skills/service/skill_service.py`:删掉
   ```python
   sys.path.insert(0, str(Path(__file__).resolve().parent))
   import skill_generators as sg
   ```
   改为
   ```python
   from mujoco_skills.skills import skill_generators as sg
   ```
   同时 `MANIFEST`/`SCENE`/`TRACKS` 等路径基于 `MUJOCO_REACT_PUBLIC_DIR`,确认 `parents[N]` 层级随目录深度变化后仍指向正确的 `frontend/public`。

2. `backend/server.js`:
   - `skillServiceScript` 现指向 `tools/skill_service.py`,改为 `src/mujoco_skills/service/skill_service.py`,
     且启动方式改为模块:`uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service`
     (需先 `uv pip install -e .` 或在 `uv run` 中让包可见)。
   - `frontendPublicDir` 默认 `../frontend/public` 相对 backend 位置,确认迁移后仍成立(backend 现在在仓库根下的 `backend/`)。

### 阶段 4 — 验证

```bash
uv pip install -e .                              # 让 mujoco_skills 可 import
uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service &
curl -s http://127.0.0.1:8899/health             # 期望 {"ok":true,...}
curl -s http://127.0.0.1:8899/manifest | head    # 词表可读

cd frontend && npm run dev                        # 前端能起
# 或从 backend 启动脚本走一遍(它会 autostart skill_service)
```

全绿后:`git add -A && git commit`(把前端 + 19 个改动 + 新结构一并纳入 coro)。

---

## B. LLM 编排接入(重构后回来做)

### 原则

- **不用 LangChain**:skill 管线已结构化,框架抽象层会污染它、调试更绕。自己封一层薄 `LLMProvider` 抽象即可。
- 替换对象是 `backend/server.js` 里的 **Codex CLI 子进程**,换成 Python 编排循环。
- 工具执行层(`skill_service` HTTP)**完全不用动**,编排循环只需 HTTP 调它。

### 分层

```
前端 ──> Orchestrator 循环 (provider 无关)
          │  ├─ LLMProvider 接口
          │  │    ├─ OpenAIProvider   ← 现在实现(用现有 key)
          │  │    ├─ ClaudeProvider   ← 留空
          │  │    └─ GeminiProvider   ← 留空
          │  └─ 收到 tool_call ──HTTP──> skill_service:8899  (已存在)
          ▼
        读 service/llm_tools.json 拿 tool schema
```

### 关键设计点(可切换的成败处)

1. **中立数据结构**:编排循环内部用自定义的 `Message` / `ToolCall`,不直接传各家 SDK 的原生对象;翻译只在 Provider 适配器里发生。
2. **一份中立 tool schema**:把 `llm_tools.json` 拆成"核心 JSON Schema(参数)" + "provider 包装生成器"。
   - OpenAI:`{type:"function", function:{name, description, parameters}}`
   - Claude:`{name, description, input_schema}`
   - Gemini:`functionDeclarations: [{name, description, parameters}]`
3. **抹平三处最易分叉的差异**:
   - **工具结果回填的消息角色**:OpenAI `role:"tool"`;Claude `tool_result` content block;Gemini `functionResponse`。
   - **多工具并行**:适配器统一返回 `list[ToolCall]`。
   - **结束条件**:统一成"本轮是否还有 tool_call",别依赖各家 `finish_reason` 字面值。
4. **配置切换**:`PROVIDER=openai` 环境变量 + 工厂函数返回对应实现。当前只实现 OpenAI,其余留 `NotImplementedError`。

### 建议接口签名(实现前先定型)

```python
# src/mujoco_skills/orchestrator/schema.py
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict          # 纯 JSON Schema

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class Message:
    role: str                 # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None   # role=="tool" 时回填

# src/mujoco_skills/orchestrator/providers/base.py
class LLMProvider(Protocol):
    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message: ...
    # 返回一条 assistant 消息:要么带 content(结束),要么带 tool_calls(继续)
```

编排循环骨架:

```python
# src/mujoco_skills/orchestrator/loop.py
def run(user_msg: str, provider: LLMProvider, tools: list[ToolSpec], execute) -> list[Message]:
    msgs = [Message("system", SYSTEM_PROMPT), Message("user", user_msg)]
    while True:
        reply = provider.chat(msgs, tools)
        msgs.append(reply)
        if not reply.tool_calls:
            return msgs                      # 结束
        for call in reply.tool_calls:        # execute = HTTP 打到 skill_service
            result = execute(call.name, call.arguments)
            msgs.append(Message("tool", content=json.dumps(result), tool_call_id=call.id))
```

`execute(name, args)` = 把 `get_manifest/standoff_for_point/compile_plan` 映射到 skill_service 的
`GET /manifest` / `POST /standoff` / `POST /compile_plan`。

### 前后端接线

- 编排循环放 `src/mujoco_skills/orchestrator/`,对外开一个 HTTP 端点(可复用 skill_service 的 http.server 风格,或单开一个 orchestrator service)。
- `backend/server.js`:移除/停用 Codex PTY 子进程那套(`startCodex`/`getCodexArgs`/terminal SSE),或保留为可选调试通道;前端 authoring 面板从"Continue in Codex CLI"改为调编排端点。
- 前端 `AuthoringPanel.tsx` 的 `clarifying` / `intent_review` 阶段接编排循环的流式输出。

### 模型档次建议

- 现在:OpenAI 推理档模型跑规划 / tool calling。
- 将来启用"可切换"时,tool-calling agent 场景推荐把 **Claude(Opus/Sonnet)** 作为主力候选;Gemini 的百万级长上下文适合"一次性塞大量场景/XML"的场景。
- **Codex 不适合当运行时编排后端**(它偏"自动写代码/改仓库"的编码 agent),仅作为当前人肉共创的过渡形态。

---

## 执行顺序小结

1. 新会话:做 A(阶段 0→1→2→3→4),全绿后 commit。
2. 回到本方向:做 B(定接口 → 实现 OpenAIProvider → 编排循环 → 接线 → 替换 Codex 子进程)。
