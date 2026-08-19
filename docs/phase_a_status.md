# Phase A(抽屉容器放置)实现状态总结

> 对应设计文档:docs/container_placement_design.md(Phase A 一节)。
> 本文档记录 Phase A 完成后又修的两个问题(站位方向、物理跟随),供后续会话/方案讨论参考。
> Phase B(柜子+冰箱,水平伸入,RRT)**完全未触及**。

---

## 一、Phase A 原始范围(已完成)

按设计文档 A1-A5 实现,给 `drawer_left`/`drawer_right` 加上"放入"能力,不碰 RRT/冰箱/柜子。

| 项 | 内容 | 落地位置 |
|----|------|----------|
| A1 | `scene_table.json` 给两个抽屉加 `place` 段(`kind:"container"`, `access:"top"`, `interior_body`, `requires_open`) | [layout042_study.scene_table.json](../frontend/public/assets/robocasa/layout042_study.scene_table.json) |
| A2 | `dest_point` 扩展:容器时用 `interior_body` 的可碰撞底板几何算放置点(复用射线求 z + `distribute_slots`) | `skill_generators.py` `_container_dest_point`/`_interior_floor_geom` |
| A3 | `build_skills_manifest` 给容器回填 `standoff`,**同时持久化写回 `standoffs.json`**(不只是写进 manifest——否则 `compile_robot` 运行时读的是独立的 `standoffs.json`,manifest 里有不代表 compile 时能用,这是第一轮就踩到、也是"island KeyError"那类问题的同款根因) | `build_skills_manifest.py` |
| A4/A5 | `compile_robot` 的 place 分支按 `access` 选生成器(目前只有 `"top"` → 现有 `gen_place`);`STAGE2_PROMPT` 补"放入容器=开→导航→放→(可选)关"规则 | `skill_generators.py`、`orchestrator/prompt.py` |

**A6 验收**(当时全部通过):重建 manifest 后 drawer_left/right 的 `place`/`standoff` 非 null;端到端 `"把一个杯子放进 drawer_left"` compile 无 warning;sink 回归无问题。

---

## 二、后续修的问题 1:站位方向不对(站在侧面,不是正前方)

**现象**:用户截图发现机器人开/关抽屉、放置时站在柜体侧面,不是正对抽屉。

**根因**:A3 最初复用了 `_skill_entry_standoff`(从 OpenDrawer/CloseDrawer 录制demo里提取机器人站位)——但那个demo录制时机器人是为了够到抽屉**把手**才站的位置,不保证正对抽屉中心(实测偏了 0.57m)。这个函数的本意是"navigate 紧接着要回放的 articulation 技能时别跳位",不是"给放置任务找一个正对的好站位"——两个不同的用途被我搞混了。

**修法**:改用和 mug/sink 同款的通用求解器 `standoff_for_point`——针对"抽屉打开状态"下的几何清空/可达性重新搜索一个站位,而不是复用demo的站位。`drawer_left` 现在 Y 精确对齐抽屉中心,正对开口距离 0.75m;`drawer_right` 因两个抽屉紧邻,直线正前方被挡,求解器给出一个带角度但真实可行的清空点(诚实的几何约束结果,不是 bug)。

**改动位置**:`build_skills_manifest.py` 的 A3 段落(import 换成 `standoff_for_point`,不再用 `_skill_entry_standoff`)。

---

## 三、后续修的问题 2:关抽屉后杯子"漂浮"(不跟着抽屉走)

### 3.1 根因诊断

抓取/搬运一直是**运动学附着**(物体自由关节被直接写死到夹爪算出来的位置,不涉及真实动力学)。`place` 释放后,物体的位置被冻结成一个固定世界坐标常量;而 `OpenDrawer`/`CloseDrawer_stack4` 这类**预录制回放**轨迹的 channels 里根本不知道有物体存在。于是关抽屉时,抽屉几何缩回去了,物体的坐标却没人更新——视觉上就是"漂浮在半空"。这个问题只有目的地本身**还会动**(容器)才会暴露,sink/台面这种静态目的地永远不会触发。

### 3.2 采用方案(用户明确选择"窄范围")

- 抓取/搬运阶段**完全不变**,仍是运动学附着(这部分一直工作良好,不打滑不失败)。
- **只有"已放置、释放的物体"变成真实动力学体**:
  - **放置瞬间**:不再只是估算一个高度就冻结,而是真的用 `mj_step` 让物体在重力+接触下落定到容器底板上。
  - **后续 Open/Close**:机器人和容器滑轨关节严格按录制轨迹回放(位置+速度都精确复现,接触求解器能感知真实开合速度),只有物体的自由关节真正参与动力学积分——被移动的容器几何通过接触力"推着走"。

### 3.3 实现中连续挖出的四个真实 bug(全部已修)

1. **落定仿真用了错误的基准状态**:最初把整个场景状态重置成默认的 `qpos0`(抽屉=关闭),但物体的释放位置是按"抽屉已打开"算的——两者不匹配,物体直接自由落体穿过底板摔到房间地板。**修法**:落定仿真改用当前真实的 `q`(已经反映"抽屉打开"状态),不用 `qpos0`。

2. **两段demo起始状态不一致导致穿模弹开**:`OpenDrawer` 结束于滑轨值 `-0.426`,但 `CloseDrawer_stack4` 自己录制的起始帧假设是 `-0.299`,差 `0.127`。直接回放绝对值会让容器几何在这一步开始瞬间"瞬移",物体被瞬间嵌入几何里,接触求解器只能用一次剧烈弹开消解(即用户看到的"杯子突然跳出去")。

   **第一版修法(已废弃)**:脚本化关节(机器人自身关节+容器滑轨关节)统一改成"锚定到当前真实值起步,按录制轨迹的相对增量运动,期间用一个随时间从 1 线性衰减到 0 的修正量把起点差值逐渐吸收掉"。这是一个数据层面的调和/近似,不是对"两段demo为何不一致"本身的物理还原,能工作但比较拼凑。

   **最终修法(倒放 Open,替代上面的调和方案)**:用户提出——`OpenDrawer` 自己的首帧是 `-0.00011`(几乎精确闭合),末帧是 `-0.42625`(正好是 `q` 当前值)。如果把 Close 直接实现成 `OpenDrawer` 轨迹的**时间倒放**,两端天然精确对齐,完全不需要任何修正/调和逻辑。实现:新增 `_reverse_track`(纯数组倒放:channels/phase 整体反转,`time` 用反转后的帧间隔重新累加,不涉及任何物理计算,~20 行);`compile_plan` 从 manifest 的 `articulation.skills` 建一条 `close技能名→open技能名` 的映射;`compile_robot` 的 articulation 分支里,只要该 facility 有跟随物体、且能找到对应的 open 技能,就加载 open 的轨迹倒放使用,而不是加载独立录制的 close 文件。验证:抽屉平滑关到 `0.0`,物体平滑跟随到位后随地板停止而停止,无跳变无 NaN,sink 回归无影响,总改动量约 40 行,比调和方案更干净且不需要"诚实说明"这类权衡免责声明。

3. **【真正导致"看起来还是在漂浮"的那个 bug】算出来的跟随轨迹从未真正送到前端**:`write_generated_tracks` 只按 label 前缀(`navigate_`/`pick_`/`place_`)决定要不要把编译结果写回磁盘,`CloseDrawer_stack4` 不匹配这个前缀——物理仿真本身是对的,但前端拿到的 `track_url` 一直指向磁盘上原封不动、不知道物体存在的老 demo 文件。**修法**:给"带跟随物体"的容器回放轨迹一个独立文件名(如 `CloseDrawer_stack4_with_mug_1.track.json`),不覆盖所有任务共享的原始 demo 文件(避免污染其他不涉及物体的复用场景);`write_generated_tracks` 加显式标记识别这类"被物理仿真改过"的轨迹也要写盘。

4. **(调试过程中的操作性坑,非代码 bug)**:反复用后台方式重启 `skill_service` 导致 8899 端口上同时有**好几个僵尸进程**在监听,请求随机落到不同进程上,造成"直接调 Python 函数验证是对的,走 HTTP 测又不对"的诡异不一致。已清理干净并确认单一进程。**这是本次会话的操作失误,值得记住:每次改完 `skill_generators.py`/`build_skills_manifest.py` 后重启服务,必须先确认端口上真的只剩 0 个监听者,再起新的。**

### 3.4 最终验证

- 抽屉滑轨值从头到尾平滑连续,精确关到 `0.0`(完全闭合)。
- 物体 x 坐标随抽屉平滑跟随移动,z 全程稳定(无掉落/穿模/爆炸/NaN)。
- sink 放置回归测试:无 warning,行为与改动前一致。
- 端到端(orchestrator + 浏览器 `/debug` 面板)确认杯子视觉上真的跟着抽屉一起收进去。

### 3.5 已知的诚实说明/待观察点

- 物体的移动量比容器底板本身的位移量略小(观察到轻微"打滑"),这在真实摩擦物理下是合理现象,但**没有针对"贴不贴后壁"这类精细行为做进一步调参**——目前的验收标准是"稳定、不失控、行为可信",不是"精确复刻某个理想轨迹"。
- 倒放 Open 得到的 Close 动作,时间节奏(节拍/快慢分布)与 Open 完全对称——即"先快速拉开、后段平稳"倒过来就是"先平稳、后段快速推关"。这和原始 `CloseDrawer_stack4` demo 自己的节奏未必一致,只是本次验收下来看是平滑、合理的;**只对 drawer_left/drawer_right 生效**(仅当该 facility 有跟随物体、且 manifest 里有对应的 open 技能名时才会触发倒放替换;没有跟随物体的普通 Close 仍走原始录制文件,行为不变)。
- 两段demo(Open/Close)本身为何不一致(是否是提取脚本的问题),仍未深挖——只是现在已经不需要关心这个问题了,因为倒放方案直接绕开了它,不再依赖 Close 自己的录制起点。

---

## 四、涉及的文件清单

| 文件 | 改动类型 |
|------|----------|
| `frontend/public/assets/robocasa/layout042_study.scene_table.json` | Phase A 新增 place 段 |
| `src/mujoco_skills/pipeline/build_skills_manifest.py` | Phase A 容器 place 输出 + A3 standoff 回填;后续改用 `standoff_for_point` |
| `src/mujoco_skills/skills/skill_generators.py` | Phase A `dest_point`/`compile_robot` place 分支;后续新增 `_settle_object_physically`/`_replay_with_resting_object`/`_lerp_track_channels`/`_extend_track_with_settle`,`compile_robot` 增加 `resting` 状态追踪、`generated_track` 标记,`write_generated_tracks` 识别该标记 |
| `src/mujoco_skills/orchestrator/prompt.py` | Phase A `STAGE2_PROMPT` 补容器放置规则 |
| `frontend/public/trajectories/layout042_study/standoffs.json` | 数据文件,回填 drawer_left/right 的 standoff(受 A3 修复影响,已从侧面站位改成正前方) |

`loop.py` / `providers/` / `stage1.py` / 两阶段流程本身,以及 Phase B(fridge/upper_cabinet 的容器放置、RRT)**全程未触及**。
