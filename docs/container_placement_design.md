# 第 6 步:容器放置(冰箱 / 柜子 / 抽屉)设计

> 本文档自包含,供后续实现(自己做或交给新会话)。前置:第 1-5 步已完成
> (manifest 名词/动词四段结构 + 两阶段 LLM,见 docs/manifest_redesign.md /
> docs/two_stage_llm_design.md)。**本步给 fridge / upper_cabinet / drawer 加"放入"能力。**

## 动机

study setup 需要:**水果/牛奶 → 冰箱、杯子/碗 → 水池、condiment → 柜子**。
当前只有 `sink` 和三段 `counter` 的 `place` 非 null,**fridge / cabinet / drawer 全部 `place=null`**
(只能开关,不能放入)。所以 study 三类任务里两类跑不了。本步补齐容器放置。

## 核心事实(已用代码 + 场景几何确认)

1. **现有 `gen_place`([skill_generators.py:546](../src/mujoco_skills/skills/skill_generators.py:546))是"自顶向下"**:
   ready(举着)→ 目标XY正上方 → 竖直下降 → 松爪 → 收回。对开放台面(sink/counter)成立,
   对**门后有遮挡的容器不成立**(无法从正上方竖直插入)。

2. **各容器内部都有可碰撞几何**(RRT 可做碰撞检测),但形状不同:

   | 容器 | 内部 body(用于算放置点) | 形状 | 接近方向 |
   |------|--------------------------|------|---------|
   | 抽屉 `drawer_left` | `stack_2_right_group_1_3_inner_box` (id 131) | 开口向上的盒子(壁高 ~0.086) | **top(自顶向下)** |
   | 抽屉 `drawer_right`  | `stack_4_right_group_1_2_inner_box` (id 154) | 同上 | **top** |
   | 柜子 `upper_cabinet` | `hingecabinet_2_right_group_1_level1_main` (id 112) / `level2_main` (id 113) | 两层薄搁板,z≈**1.84** | **front(水平伸入)** |
   | 冰箱 `fridge` | `fridgefrenchdoor_main_group_1_Body001_Clear` (id 62,含两块 collidable 搁板) | french 门 + 纵深 | **front(水平伸入,最深)** |

   > `layout042_sorting` 已按物理方位修正：`drawer_left`=stack_2(id131)、
   > `drawer_right`=stack_4(id154)。授权内部点时仍应同时核对 facility 与 body id。

3. **关键推论**:抽屉打开后是"开口向上的盒子" → **现有 top-down `gen_place` 直接适用,不需要 RRT**。
   真正需要新运动规划(水平伸入)的只有 **柜子 + 冰箱**。

4. **机械臂物理上够得到**三者内部(用户已肉眼确认);柜子搁板在 z≈1.84 偏高,真做时 IK 给确定答案。

## 实现策略:增量两段

**Phase A —— 抽屉切片(top-down,不碰 RRT)**:用现有 `gen_place` 打通"容器放置"的**整条管线**
(能力接进 manifest / compile、standoff、内部点计算、Open→place→Close 时序)。低成本、低风险。

**Phase B —— RRT 做柜子 + 冰箱(水平伸入)**:管线已由 Phase A 验证,本段只专注运动规划本身。

---

## Phase A:抽屉放置(top-down)

### A1. manifest / scene_table 的 `place` 字段扩展

给 `place` 段区分"平面"和"容器",并携带运动策略所需信息:

```jsonc
// 平面(现状,sink/counter)——保持
"sink": { ..., "place": { "kind": "surface", "access": "top",
                          "surface_body": "sink_island_group_1_main",
                          "override": { "inset": [0.12,0.08], "z_offset": 0.05 } } }

// 抽屉(新增)
"drawer_left": {
  ..., "articulation": { ... },      // 已有
  "place": {
    "kind": "container",
    "access": "top",                 // 抽屉 = 自顶向下
    "interior_body": "stack_4_right_group_1_2_inner_box",
    "requires_open": "OpenDrawer"    // 放入前必须先开(precondition)
  }
}
```

- 手写部分进 `<scene>.scene_table.json`(`interior_body` / `requires_open` / `access`);
  `place` 的存在即"能放入",与决策"能力=字段在不在"一致。
- `access` 决定运动策略:`"top"` → `gen_place`;`"front"` → Phase B 的 RRT 生成器。

### A2. 内部放置点计算(不手写坐标)

新增一个函数,从 `interior_body` 的几何算放置点:
- 取该 body 的**可碰撞底板 geom**(抽屉:geom727/891 那块 type=6 box),中心 XY = body 的世界 XY;
- z:射线向下打到底板上表面(复用 `rig.surface_z` 思路),或底板顶面 = 板中心 z + 板半高;
- 多物体时用现有 `distribute_slots` 在底板范围内分槽。

即扩展现有 `dest_point`([:718](../src/mujoco_skills/skills/skill_generators.py:718)):当 facility 的 place 是 container
时,target body 换成 `interior_body`,其余(射线求 z、分槽)复用。

### A3. standoff(站位)

抽屉的 `standoff` 目前为 null。**复用 OpenDrawer* 轨迹自带的入场位**——`_skill_entry_standoff`
([:753](../src/mujoco_skills/skills/skill_generators.py:753))已能从一条 articulation 轨迹提取 {standoff_xy, face_xy}。
build_skills_manifest 时,对有 `requires_open` 的容器,把对应 Open* 轨迹的入场位写进它的 `standoff`,
顺带消除"容器 can_navigate=false 导致 navigate 不过去"的问题。

### A4. 时序(Open → place → Close)

- 由**规划器(阶段② LLM)**排序:`Open<drawer>` → `place(obj, drawer)` → 可选 `Close<drawer>`。
- STAGE2_PROMPT 已有"放入闭合容器要先开"的规则([prompt.py](../src/mujoco_skills/orchestrator/prompt.py) Articulation 段),
  实现后需补充:放入 = Open → navigate(容器) → place → (可选)Close,且 place 的 dest 是容器名。
- compile 侧:place 步的 dest 是 container facility 时,走 A2 的内部点 + A1 的 access 分支;
  articulation 步仍是回放轨迹(现状不变)。

### A5. 接线改动点

| 文件 | 改动 |
|------|------|
| `<scene>.scene_table.json` | 给 drawer_left/right 加 `place`(container/top/interior_body/requires_open) |
| `build_skills_manifest.py` | 输出容器 `place`;对容器从 Open* 轨迹回填 `standoff` |
| `skill_generators.py` `dest_point` | 容器时用 `interior_body` 求点 |
| `skill_generators.py` `compile_robot` place 分支 | 按 `access` 选生成器(top → gen_place) |
| STAGE2_PROMPT | 补"放入容器 = 开→导航→放→(可选)关"的展开规则 |

### A6. Phase A 验收

- `"把杯子放进左抽屉"`(或对应 manifest 名)→ 阶段①接地(drawer_left 现在 can_place=true)→
  阶段② 生成 OpenDrawer → navigate → place → CloseDrawer,compile 无 warning,mug 落在抽屉内底板上。
- 回归:sink/counter 的 `surface` 放置仍照旧(access=top 复用同一 gen_place,不回归)。

---

## Phase B:柜子 + 冰箱(RRT 水平伸入)

仅当 `place.access == "front"` 时启用。**做 Phase B 前先跑一个只读检查**:确认机械臂 geom 的
`contype/conaffinity` 与容器 geom 会互相碰撞(否则 RRT 的碰撞检测形同虚设)——用查内部搁板同样的方式验。

### B1. 运动规划

- **关节空间 RRT-Connect**,MuJoCo 当 state-validity checker:设 arm qpos → `mj_forward` →
  检查臂 geom + **手里物体 geom** 与容器 geom 的接触(有接触 = 非法)。
- **起点**:navigate 到容器 standoff 后的 ready(举着物体)位形。
- **目标**:对内部搁板上方一点做 IK,采样多个 IK 种子,**丢弃处于碰撞的解**;取第一个可行解为 goal。
- **可复现性**:RNG **播种子**(study 要可复现)。
- **平滑**:RRT 原始路径做 shortcut 平滑。
- **产出**:关节路径 → `build_track`(接受任意数量 waypoint)→ 与 gen_place 相同的 track 格式。

### B2. 新增生成器

`gen_place_reachin(rig, q_start, obj_name, interior_body, off, ...)`:封装 B1,签名与 `gen_place`
对齐(输入举着的位形 + 目标 body,输出 track + 末位形),使 `compile_robot` 的 place 分支只需按
`access` 选 `gen_place` 或 `gen_place_reachin`。

### B3. scene_table 增量

```jsonc
"upper_cabinet": { ..., "place": { "kind":"container", "access":"front",
                                   "interior_body":"hingecabinet_2_right_group_1_level1_main",
                                   "requires_open":"OpenCabinet" } },
"fridge":        { ..., "place": { "kind":"container", "access":"front",
                                   "interior_body":"fridgefrenchdoor_main_group_1_Body001_Clear",
                                   "requires_open":"OpenFridge" } }
```

- 注意 `upper_cabinet` 有两层(id 112/113);先用 level1,是否暴露"哪层"给 LLM 以后再说。
- 冰箱内部 id 62 含两块搁板;先落在下层的一块,后续可细化选层。
- **注意 notes**:manifest 现有说明 upper_cabinet 起始为 open 且**没有 OpenCabinet 技能**
  (只有 CloseCabinet)。所以柜子放入的 `requires_open` 语义要按"初始已开"处理,或补 Open 技能;
  实现前先核对这条 notes。

### B4. Phase B 风险(诚实)

- 受限空间 IK 目标可能全在碰撞 → 需多采样 / 放宽目标点;
- 机械臂/物体碰撞分组若没配好,RRT 检测失效(B 前置检查要拦住);
- RRT 非确定性,须播种子;原始路径抖,须平滑;
- 冰箱纵深最大、french 门,最可能需要中间 waypoint 引导(先出门框再进搁板)。

---

## 与既有设计的一致性

- 仍遵守"能力 = 字段在不在":容器可放入 ⇔ `place != null`;`access` 只是放置**动作策略**的内部标记。
- 名词/动词分层不变:place 仍是参数化 op,只是新增一个"容器/front"的生成分支(RRT),与"平面/top"
  (gen_place)并列。articulation(Open/Close)仍是回放技能,不变。
- 两阶段流程不变:阶段①因 drawer/cabinet/fridge 的 can_place 变 true 而能接地这些目的地;
  阶段②多出"开→放→关"的展开,由 STAGE2_PROMPT 规则驱动。

## 建议实现顺序

1. Phase A(抽屉):A1→A2→A3→A4→A5,按 A6 验收。**先把管线跑通。**
2. Phase B 前置只读检查(臂/容器碰撞分组)。
3. Phase B(柜子,较浅)→ 冰箱(最深)。
4. 重建 manifest,更新 STAGE2_PROMPT,端到端验证 study 三类任务全部可跑。

## 环境提醒

- 端到端需起 skill_service;`OPENAI_API_KEY` 从 `.env`;公司网络 TLS 需 truststore(已在 CLI)。
- **Windows**:`pkill -f skill_service` 杀不掉 uv 孙进程;按端口
  `Get-NetTCPConnection -LocalPort 8899` 拿 PID 再 `taskkill /F /T`,否则连到旧进程跑旧代码。
