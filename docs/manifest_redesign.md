# Manifest 重构 + 两阶段 LLM 设计

> 本文档汇总讨论确定的设计,作为实现依据。**尚未写任何实现代码。**
> 上游背景见 [[orchestrator-architecture]] / restructure_and_llm_plan.md。

## 动机:分清"名词"与"动词"

当前系统把两类东西混为一谈,导致多个具体问题:

- **数据污染**:`compile_plan` 运行时把 `navigate_*`/`pick_*`/`place_*` 轨迹写进
  `trajectories/<scene>/tracks/`,而 `build_skills_manifest` 无差别扫描该目录,于是这些
  **运行时生成物泄漏进 manifest 的 `skills`**,被当成固定技能。证据:`robot1/` 下残留
  `navigate_mug_2`/`pick_mug_2`/`place_mug_2_sink`(某次搬 mug_2 的残留),`robot0/` 却没有
  —— manifest 内容因此**不确定**,取决于上次 compile 跑了什么。
- **facility 写死**:`build_skills_manifest.py` 顶部的 `FACILITIES` dict(6 项)绑死
  layout042_study 的 body 名,换场景完全失效。
- **facility 角色被拆散在三张表**:`FACILITIES`(名字/body/关节)、`standoffs.json`(站位)、
  `PLACEMENT_REGIONS`(放置区,**只有 sink 一项**)。同一 facility 在不同表里存在性不一致 ——
  例如 `island` 在 `named_facilities` 里有、在 `standoffs` 里没有,导致 navigate 到 island 时
  `standoffs[tgt]` 抛 `KeyError: 'island'`(每次首编译必现的那个错)。
- **placement 无法枚举**:能放东西的表面远不止 sink —— island、多段 counter
  (`counter_1_left/main/right_group_1_main`)等。手写枚举每个台面不可行。

核心重构:**名词(objects / facilities)可枚举、随场景固定;动词(navigate / pick / place)
是参数化 op,不枚举实例;只有 RoboCasa 回放技能(Open/Close*)才是固定 `skills`。**

## 一、文件布局:输入 vs 输出分离

```
frontend/public/assets/robocasa/
├── layout042_study.xml                    # 场景本体
└── layout042_study.scene_table.json       # ★新:手写场景表(输入)

frontend/public/trajectories/layout042_study/
├── skills_manifest.json                   # build_skills_manifest 生成(输出)
├── standoffs.json                         # 生成
└── tracks/                                # 提取(真技能) + 运行时生成(见"污染分离")
```

- **手写场景表 = 场景 XML 同目录的 `<scene>.scene_table.json`**,跟着场景走。复用 backend 已有的
  sidecar 约定(`server.js` 已在 sceneDir 找 `scene_manifest.json`/`task_plan.json`),但**命名带
  场景前缀**避免大平铺目录撞名。
- **生成的 manifest 仍在 `trajectories/<scene>/`**。手写输入**不放** trajectories,避免重蹈"输入输出
  混放 + 被 build 扫描污染"。

数据流:
```
<scene>.scene_table.json (手写: facilities/objects/命名台面)
        │
        ▼
build_skills_manifest.py ──读场景表 + 加载 XML 算 world_pos / 派生技能──▶
        │
        ▼
trajectories/<scene>/skills_manifest.json ──get_manifest──▶ LLM
```
`build_skills_manifest.py:42` 的写死 `FACILITIES` 搬进场景表;脚本改为通用:
`--scene-table <path>`(默认从场景 XML 路径推 `<scene>.scene_table.json`)。

## 二、手写场景表 `<scene>.scene_table.json` 结构

只放**无法从模型自动派生**的语义信息(名字、body 绑定、角色语义、特殊 override)。
`world_pos`、放置区几何等**能算的都不写**。

```jsonc
{
  "facilities": {
    "island":  { "label": "kitchen island", "body": "island_island_group_1_main" },
    "sink":    { "label": "sink", "body": "sink_island_group_1_main",
                 "place": { "surface_body": "sink_island_group_1_main",
                            "override": { "inset": [0.12, 0.08], "z_offset": 0.05 } } },
    "counter_left":  { "label": "left counter",  "body": "counter_1_left_group_1_main",
                       "place": { "surface_body": "counter_1_left_group_1_main" } },
    "counter_mid":   { "label": "center counter","body": "counter_1_main_group_1_main",
                       "place": { "surface_body": "counter_1_main_group_1_main" } },
    "counter_right": { "label": "right counter", "body": "counter_1_right_group_1_main",
                       "place": { "surface_body": "counter_1_right_group_1_main" } },
    "fridge":  { "label": "refrigerator", "body": "fridgefrenchdoor_main_group_1_main",
                 "articulation": {
                   "joints": ["fridgefrenchdoor_main_group_1_fridge_left_door_joint",
                              "fridgefrenchdoor_main_group_1_fridge_right_door_joint"],
                   "initial_state": "closed" } },
    "upper_cabinet": { "label": "upper cabinet", "body": "hingecabinet_2_right_group_1_main",
                 "articulation": {
                   "joints": ["hingecabinet_2_right_group_1_leftdoorhinge",
                              "hingecabinet_2_right_group_1_rightdoorhinge"],
                   "initial_state": "open" } },
    "drawer_left": { "label": "left drawer", "body": "stack_2_right_group_1_3_main",
                 "articulation": { "joints": ["stack_2_right_group_1_3_slidejoint"],
                                   "initial_state": "closed" } },
    "drawer_right":  { "label": "right drawer", "body": "stack_4_right_group_1_2_main",
                 "articulation": { "joints": ["stack_4_right_group_1_2_slidejoint"],
                                   "initial_state": "closed" } }
  },
  "objects": {
    "mug_1": { "label": "mug", "body": "mug_1_main", "home_facility": "island", "pickable": true },
    "mug_2": { "label": "mug", "body": "mug_2_main", "home_facility": "island", "pickable": true }
  }
}
```

说明:
- **命名台面是语义筛选**,不枚举所有 geom;一个语义名可映射一个 body(以后可扩展到多 body)。
  当前这套命名**只适用于 study scene**,通用化以后再说。
- `place` 段的有无 = **能否放置**;`override` 仅用于几何不足以自动确定的特殊区(sink 盆内)。
  普通台面无 `override`,放置区从 geom 计算。
- `articulation` 段的有无 = **能否开关**;`initial_state` 手写(cabinet 起始为 open)。
- **不写 `roles` 字段**(见决策 3),**不写 `world_pos`**(生成时算)。

## 三、生成的 `skills_manifest.json` 目标结构(四段 + skills)

```jsonc
{
  "scene": {
    "xml": "assets/robocasa/layout042_study.xml",   // 修掉当前残留的 "mujoco_react/" 前缀
    "nq": 125,
    "init_keyframe": "study_init"
  },

  "objects": {                                       // 名词 · 可枚举 · 含位置
    "mug_1": { "label": "mug", "body": "mug_1_main",
               "home_facility": "island", "world_pos": [x,y,z], "pickable": true },
    "mug_2": { "label": "mug", "body": "mug_2_main",
               "home_facility": "island", "world_pos": [x,y,z], "pickable": true }
  },

  "facilities": {                                    // 名词 · 命名锚点 · 能力=字段在不在
    "island": { "label": "kitchen island", "body": "...", "world_pos": [x,y,z],
                "standoff": null, "place": null, "articulation": null },
    "sink":   { "label": "sink", "body": "...", "world_pos": [x,y,z],
                "standoff": { "standoff_xy": [..], "face_xy": [..] },
                "place": { "surface_body": "...", "region": { /* 计算得到 or override */ } },
                "articulation": null },
    "fridge": { "label": "refrigerator", "body": "...", "world_pos": [x,y,z],
                "standoff": { ... }, "place": null,
                "articulation": { "joints": [...], "initial_state": "closed",
                                  "skills": { "open": "OpenFridge", "close": "CloseFridge" } } }
    // counter_* / drawer_* / upper_cabinet 同理
  },

  "ops": {                                           // 动词 · 参数化 · 不枚举实例
    "navigate": { "generated": true,
                  "params": { "target": "facility where standoff != null" } },
    "pick":     { "generated": true,
                  "params": { "object": "objects where pickable == true" } },
    "place":    { "generated": true,
                  "params": { "object": "a currently-held object",
                              "dest": "facility where place != null" } }
  },

  "skills": [                                         // 只留真·回放技能(Open/Close*)
    { "name": "OpenFridge", "facility": "fridge",
      "precondition": { "facility_state": "closed" }, "effect": { "facility_state": "open" },
      "robots": ["robot0","robot1"], "provenance": "dataset", "tracks": { ... } }
    // ... CloseFridge / OpenDrawer / CloseDrawer / OpenDrawer_stack2 / CloseDrawer_stack4 / CloseCabinet
  ],

  "notes": { ... }
}
```

**能力全部由字段在不在表达,单一真相**:
- 能 navigate ⇔ `facilities[f].standoff != null`
- 能 place ⇔ `facilities[f].place != null`
- 能开关 ⇔ `facilities[f].articulation != null`
- 物体起点 ⇔ `objects[o].home_facility`(取代旧 `source_surface` role)

## 四、确定的设计决策

1. **名词/动词分层**:objects+facilities 枚举;navigate/pick/place 是参数化 `ops`,不烘焙实例。
2. **`objects` 场景侧手写声明 + 含位置**;去掉 `valid_destinations`(可行性交给 ops+facilities)。
3. **删除 `facilities.roles`**:代码从不消费它(仅出现在注释),能力已由字段结构表达,冗余且易漂移。
4. **placement 计算化,不枚举**:能放 = 有 `place` 段;放置区默认从 `surface_body` 的 geom 顶面 +
   `surface_z` 射线计算(机制已存在于 `dest_point`),仅特殊区写 `override`。
5. **facility 三表合一**:`standoff` / `place` / `articulation` 三个可空段收进 facility 自身,
   消除存在性不一致(含 `'island'` KeyError 类脆弱点)。
6. **污染分离**:`build_skills_manifest` 只收真·技能。判据 = track `meta` 有 `source`/`base_start`
   (dataset 提取)或 `fixture_joints` 非空;`navigate_*`/`pick_*`/`place_*` 生成物一律排除。
   (可进一步让 `write_generated_tracks` 写到单独目录,如 `tracks/_generated/`,双保险。)
7. **场景表位置**:`<scene>.scene_table.json`,与场景 XML 同目录(输入);生成的 manifest 在
   `trajectories/<scene>/`(输出)。

## 五、两阶段 LLM 处理逻辑(方向,后续实现)

```
自然语言目标
  │  ① 语义接地(只用名词层: objects[label/home_facility/world_pos] + facilities)
  │     例:"把桌上的杯子都放到水池" → 场景无"桌子",匹配岛台上的 mug_1/mug_2;"水池"→ sink
  ▼
语义任务 tasks: [move mug_1→sink, move mug_2→sink]   ← 先给用户确认(便宜层纠偏)
  │  ② 技能分解(动词层: ops 约束 + facilities 可行性 + skills)
  │     每个 task → navigate(obj) - pick(obj) - navigate(dest) - place(obj,dest)
  ▼
skill 级 steps → compile_plan → schedule
```

- 与现有前端 `intent_review → 语义 task 列表 → confirm → 执行` 阶段吻合
  ([AuthoringPanel.tsx](../frontend/src/authoring/AuthoringPanel.tsx))。
- 两阶段建议为**两次独立 LLM 调用**,中间插入人工确认(天然断点);orchestrator loop 需支持
  "产出中间结构 → 暂停 → 再续"。
- 语义任务用 schema 强约束结构(如 `{task, object, dest}`),既供前端渲染又作为阶段②输入。

### 待定(未决,留到实现前再定)
- **阶段①的场景检索谁做**:LLM 直接读 manifest(简单)vs 提供 `find_objects(near=, type=)` 检索
  工具(更稳、能处理"桌上的""左边的"空间/同义指代)。倾向后者但成本更高。
- 命名台面的通用化(当前仅 study scene 适用)。

## 六、实现顺序(建议)

1. 写 `layout042_study.scene_table.json`(手写,照决策 2/3)。
2. 改 `build_skills_manifest.py`:读场景表(替代写死 FACILITIES)、输出四段结构、按决策 6 过滤污染、
   objects/facilities 的 `world_pos` 用现成的 body 世界坐标计算、place `region` 计算化。
3. 重建 manifest,核对 `get_manifest` 输出。
4. 更新 orchestrator 的 `prompt.py` / `llm_tools.json` 以对齐新词表(名词/动词)。
5. (后续)实现两阶段 LLM 流程。
