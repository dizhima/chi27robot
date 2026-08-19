# 场景改动:把 milk_1 换成第二个苹果 apple_2

> 供 codex 执行。原因:milk 的夹取有问题,换成苹果(苹果夹取已验证 OK)。
> 复用 apple_1 已有的 mesh/材质,不引入新资产。**在场景 XML 里原地替换 milk 刚体**,
> 以保持 qpos 顺序不变(keyframe 只需改一处)。

## 已确认的两个决策(照此执行,不要偏离)

1. **standoff 直接复用 milk 的**:apple_2 与 milk 同 xy,站位只取决于地面 xy,故相同。
   做法 = 把 `standoffs.json` 里 `targets.milk_1` **键改名为 `apple_2`,值原样不动**。
   **不要重跑 calibrate_standoffs**。
2. **apple_2 的 z 用 apple_1 的 z = `0.952953554998`**(两者同在 island 台面)。
   apple_2 的 quat 用 apple_1 的 quat。xy 用 milk 原来的 xy。

apple_2 最终位姿(body pos/quat 与 keyframe 必须一致):
```
pos  = 3.01656147753  -3.15099232218  0.952953554998
quat = -0.420032342589  -0.0344613662635  -0.0375682567433  0.906076084829
```

## 关键约束

- **原地替换**:把 `frontend/public/assets/robocasa/layout042_study.xml` 第 2817 行的
  `<body name="milk_1_main">…</body>` 整块**就地**改写成 `apple_2_main`,**不要删掉再在别处新增**
  ——那会改变 body 顺序、打乱 keyframe 的 qpos 对齐。
- apple_2 的几何**镜像 apple_1**(复用同一批 apple mesh/材质),但所有名字用 `apple_2_` 前缀且唯一。
- nq 保持 125(仍是一个 free-joint 物体)。

---

## A. 场景 XML `frontend/public/assets/robocasa/layout042_study.xml`

### A1. 删除 milk 资产(469–481 行附近)
删掉 `<asset>` 里所有 `milk_1_*` 的 `<mesh>`/`<texture>`/`<material>` 声明(视觉 + 碰撞 mesh、
image0 纹理、Materiale_tappo / Opaco 两个材质)。apple mesh(`apple_1_*`)已存在,apple_2 直接复用。

### A2. 原地改写刚体(2817–2829 行)
把 `milk_1_main` 整块替换为下面的 `apple_2_main`(几何 = apple_1 的那 16 个 geom:1 视觉 +
1 reg_bbox + 14 碰撞;引用 apple mesh/材质;名字全部 `apple_2_`;末尾 free joint + site):

```xml
<body name="apple_2_main" pos="3.01656147753 -3.15099232218 0.952953554998" quat="-0.420032342589 -0.0344613662635 -0.0375682567433 0.906076084829">
    <geom solimp="0.998 0.998 0.001" solref="0.001 2" density="100" friction="0.95 0.3 0.1" type="mesh" mesh="apple_1_model_normalized_0_vis" conaffinity="0" contype="0" group="1" material="apple_1_defaultMat.011" name="apple_2_g0" />
    <geom name="apple_2_reg_bbox" type="box" pos="3.387824999996625e-06 5.9302124999979786e-06 -1.9322212500000885e-05" size="0.034102200825000005 0.03393852446250001 0.033983490937500006" group="1" conaffinity="0" contype="0" rgba="0.0 1.0 0.0 0.0" solref="0.001 2" solimp="0.998 0.998 0.001" density="100" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_0"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g2"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_1"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g3"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_2"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g4"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_3"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g5"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_4"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g6"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_5"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g7"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_6"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g8"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_7"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g9"  solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_8"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g10" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_9"  type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g11" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_10" type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g12" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_11" type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g13" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_12" type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g14" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
    <geom mesh="apple_1_apple_collision_mesh_13" type="mesh" group="0" rgba="1 0 0 0" density="100" name="apple_2_g15" solref="0.001 2" solimp="0.998 0.998 0.001" friction="0.95 0.3 0.1" />
  <joint type="free" damping="0.0005" name="apple_2_joint0" /><site pos="0.0 0.0 0.0" size="0.002 0.002 0.002" rgba="0 0 0 0" type="sphere" group="0" name="apple_2_default_site" /></body>
```

> 校对:apple_1 的 body 块(2830 行起)是权威模板——若上面与实际 apple_1 的 geom 属性有出入,
> 以 apple_1 为准,仅把前缀 `apple_1`→`apple_2`、pos/quat 换成上面的值。

### A3. 更新 keyframe(3096 行,`study_init` 的 qpos)
qpos 里 milk 的那 7 个自由关节值(紧接在机器人关节之后、apple_1 之前的这一段):
```
3.016561478 -3.150992322 1.010959373 0.4206927099 4.406136921e-05 -2.034026711e-05 0.9072031975
```
替换为 apple_2 的 7 值(= 上面的 pos + quat):
```
3.01656147753 -3.15099232218 0.952953554998 -0.420032342589 -0.0344613662635 -0.0375682567433 0.906076084829
```
其余 qpos 一律不动;总长(nq=125)不变。

### A4. 重编 .mjb
删除旧的 `layout042_study.mjb`(若存在),让它重新编译;或用 backend 的 compile_scene_mjb。
backend 打开场景时也会在 xml 比 mjb 新时自动重编。

## B. `frontend/public/assets/robocasa/layout042_study.scene_table.json`
`objects` 里把 `milk_1` 整条替换成:
```json
"apple_2": { "label": "apple", "body": "apple_2_main", "home_facility": "island", "pickable": true }
```

## C. `frontend/public/trajectories/layout042_study/standoffs.json`
把 `targets.milk_1` **键改名为 `apple_2`,值原样保留**(standoff 与 xy 绑定,apple_2 同 xy)。
不要重跑 calibrate_standoffs。

## D. `src/mujoco_skills/pipeline/calibrate_standoffs.py`
第 33 行 `"milk_1": "milk_1_main",` → `"apple_2": "apple_2_main",`
(仅为将来整体重算时键名正确;本次不运行它。)

## E. `frontend/src/plan/samplePlan.ts`
133–147 行的示例 plan 用了 `milk_1`(/debug 演示)。把其中 `milk_1` 全改为 `apple_2`,
`label` 相关文案 milk→apple;若那段是牛奶专用的 pre-place 检查、apple 用不到,可整段删除。

## F. `src/mujoco_skills/skills/skill_generators.py`(清理,可选)
删掉第 750 行死条目 `"milk_1": 0.03,`(牛奶横向夹取专用参数,已无对象)。不删也无害。

---

## 重建流水线(改完必做,顺序)

1. **重编 .mjb**(A4)。
2. **重建 manifest**(objects 会从 scene_table 拾取 apple_2 并算 world_pos):
   ```
   uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.build_skills_manifest \
     --scene frontend/public/assets/robocasa/layout042_study.xml \
     --tracks-dir frontend/public/trajectories/layout042_study/tracks \
     --standoffs frontend/public/trajectories/layout042_study/standoffs.json \
     --output frontend/public/trajectories/layout042_study/skills_manifest.json
   ```
3. **重启 skill_service**(缓存 rig,不重启不生效)。

> tracks/ 里若有旧的 `*milk_1*` 生成轨迹(navigate/pick/place),无需处理:它们被 manifest 的
> 污染过滤排除,且按 plan 重新生成。

## 自测(codex 能做的)

- 场景能加载:`uv run --with mujoco==3.10.0 python -c "import mujoco; m=mujoco.MjModel.from_xml_path('frontend/public/assets/robocasa/layout042_study.xml'); print('nq', m.nq)"`(期望 nq=125,无重名/缺 mesh 报错)。
- `apple_2_main` body 存在;`milk_1_main` 及所有 `milk_1_*` mesh/材质已不存在。
- 重建后 manifest `objects` 含 `apple_2`(label "apple"、world_pos 已算)、不含 `milk_1`。
- standoffs.json `targets` 含 `apple_2`、不含 `milk_1`。
- 端到端(起 service 后):`uv run python -m mujoco_skills.orchestrator --yes "把苹果都放到水池里"`
  应接地出多个 apple 任务(apple_1 + apple_2),compile 无 warning。

## 需要用户肉眼验收(codex 做不了)

- /debug 回放确认 **apple_2 稳稳放在 island 台面上、不浮空/不陷入**(用了 apple_1 的 z,理论上一致,但请目视)。

## 环境提醒

- 改了 xml / skill_generators / build_skills_manifest 后**必须重启 skill_service**。
- **Windows 端口坑**:`pkill -f skill_service` 杀不掉 uv 孙进程;用
  `Get-NetTCPConnection -LocalPort 8899 -State Listen` 拿 PID、`taskkill /F /T /PID`,
  可能要循环几次直到查询为空,否则会连到旧进程跑旧场景。
- 需要 `OPENAI_API_KEY`(.env)+ 公司 TLS(truststore,已在 orchestrator __main__.py)才跑端到端。

## 完成后报告
改了哪些文件、自测各项结果(nq、manifest objects、standoffs targets、端到端 plan)、
以及有没有动到 milk 之外的东西(不应该)。
