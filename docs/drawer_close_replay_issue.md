# Open/Close 抽屉回放问题 —— 已解决(reposition 桥接方案)

> 本文档记录容器放置(Phase A,抽屉)物理跟随功能里一个关于 Close 回放机器人站位的行为问题。
> 背景/前置修复历史见 [docs/phase_a_status.md](phase_a_status.md)(mug 漂浮 bug 的诊断与三轮修复)。
> **状态:已解决**,解决方案见下方「最终解决方案」一节;下面的「完整背景链条」保留作为历史脉络。

## 最终解决方案(reposition 桥接,已验证)

**根因(用户诊断,已确认)**:关抽屉时机器人是从 `place` 结束时的站位(放杯子的 standoff)开始回放的,而不是 Close demo 本该站的位置(= OpenDrawer demo 的站位)。两套站位差约 0.5m。之前 `_replay_with_resting_object` 的 anchor+taper 会把底盘从 place 站位一路线性拖到 demo 站位——那条 ~0.5m 的斜向滑移,就是"动作奇怪"的真正来源。之前尝试的三种纯回放内处理都不对:

- **冻结**(v1):机器人整段不动(用户否决)。
- **taper 漂移**(v2):底盘从 place 位斜拖到 demo 位(用户看到的怪动作)。
- **绝对值回放**(v3):动作对,但 place→close 接缝处底盘+手臂瞬移 ~0.5m。

问题本质是**结构性的**:place 姿态和 close 起始姿态之间缺一个过渡步骤,任何"回放内"的处理都补不了。

**方案(用户选定:close 前插入 navigate 回位)**:在空手回放一个预录制技能前,先生成一段桥接运动把机器人送到该技能 demo 的起始姿态,然后再回放。这样回放开始时 `q` 已经精确等于 demo 首帧,`_replay_with_resting_object` 的 anchor 修正自动退化成恒等(无需再改 `_target`),底盘零漂移。

实现:
- `_skill_entry_pose(rig, q_cur, track)`([skill_generators.py:1085](../src/mujoco_skills/skills/skill_generators.py:1085) 附近):把 track 首帧值写到 `q_cur` 的副本上(未被 track 脚本化的关节——如静止物体的 free joint——保持当前姿态),得到"回放接管时机器人本该在的整套姿态"。
- `gen_reposition_for_skill(rig, q_start, target_q, label)`([skill_generators.py:1101](../src/mujoco_skills/skills/skill_generators.py:1101) 附近):底盘绕中岛走到 demo 站位(手臂收起),最后一段把手臂/手指伸到 demo 入场姿态。空手专用(`obj=None`)。
- `compile_robot` articulation 分支([skill_generators.py:1342](../src/mujoco_skills/skills/skill_generators.py:1342) 附近):门控 `held is None`(只在空手时插;持杯开抽屉的 Open 步骤因此完全不受影响)且底盘落差 >0.05m 时,插入一个 bridge item(`order=i-0.5`,排在 close 前),并把 `q` 推进到 demo 首帧。bridge item 带 `generated_track=True`,由 `write_generated_tracks` 落盘成 `reposition_<skill>.track.json`。

**验证结果**(离线直接 `sg.compile_plan`,绕开端口坑,plan = 前端 SAMPLE_PLAN_DRAWER):

| 项 | 结果 |
|------|------|
| 步骤序列 | place(order 4) → **reposition(4.5, 3.4s)** → close(5) |
| bridge 底盘 | forward -0.536→0.006, side 0.654→0.087(从 place 走回 close 站位) |
| **close 回放底盘 range** | **0.0000**(不再漂移,稳稳站在正确位置) |
| close 回放手臂 joint1 range | 0.83(真实推门动作) |
| 杯子 | 每帧最大位移 1.8cm,无弹跳,随抽屉平滑带动 |

> 复现验证脚本(离线,不依赖 HTTP 服务):见下方「如何复现/测试」的第 6 段。

---

## (历史)当初判定为未解决时的状态

**用户当时明确判定:机器人在 Close 抽屉时应该播放"倒放 Open"的真实录制动作(底盘走位 + 手臂动作),让机器人整个 Close 过程中完全静止是错误的行为。**

也就是说,曾经引入的"机器人自身关节冻结、只有抽屉滑轨关节动"这个修复方向被判定为**不对**。这个"冻结"逻辑后来先被改成对所有关节统一 anchor+taper(暴露出上面的 ~0.5m 斜移),最终由 reposition 桥接方案彻底解决。

## 完整背景链条

### 1. 起点问题:关抽屉后杯子漂浮在空中

物体放置一直是运动学附着(kinematic attach),`place` 释放后物体位置被冻结成固定世界坐标。`OpenDrawer`/`CloseDrawer_stack4` 这类预录制回放轨迹的 channels 里不知道有物体存在,于是关抽屉时抽屉几何缩回去了,物体坐标没人更新——视觉上漂浮。

**修法(已验证有效,这部分应该保留)**:窄范围物理——只有"已放置释放的物体"变成真实动力学体(`_settle_object_physically` 落定 + `_replay_with_resting_object` 让物体在容器移动时被真实接触力推动)。详见 phase_a_status.md 3.2-3.4。

### 2. 站位问题:机器人站在抽屉侧面而非正前方

已修复,改用 `standoff_for_point` 重新求解正对抽屉的站位,不再复用 Open/Close 录制demo里(为了够到把手而设计的、偏一侧的)站位。这部分**没有争议,应该保留**。

### 3. Close/Open 两段demo起始点不一致 → 改成"Close = 倒放 Open"

`OpenDrawer` 结束于滑轨值 `-0.426`,但 `CloseDrawer_stack4` 自己录制的起始帧假设是 `-0.299`,直接回放绝对值会让容器几何瞬间"跳位",物体（有物理仿真后）被瞬间嵌入几何里,接触求解器只能剧烈弹开消解。

用户提出:既然 `OpenDrawer` 自己的首帧几乎精确是 0(闭合)、末帧精确是当前值,不如让 **Close = OpenDrawer 轨迹的时间倒放**,两端天然精确对齐,不需要任何"锚定+衰减"式的调和逻辑。

**这个方向本身没有争议、应该保留**。实现:
- `_reverse_track(track, skill=None)`([skill_generators.py:1019](../src/mujoco_skills/skills/skill_generators.py:1019)):纯数组倒放(channels/phase 整体反转,`time` 用反转后的帧间隔重新累加),`skill` 参数用于把 `meta.skill` 重新标注成 Close 的名字(避免播放器显示成 "OpenDrawer")。
- `compile_plan`([skill_generators.py:1384](../src/mujoco_skills/skills/skill_generators.py:1384) 附近):从 manifest 的 `facilities[x].articulation.skills` 建一条 `close技能名 → open技能名` 的映射(`close_to_open`),并且**另一个并行会话**又加了一个 `reverse_always` 集合(从 joint 名含 "slide" 判断是滑动式容器,如两个抽屉),让抽屉的 Close **一律**倒放 Open(不再局限于"有跟随物体时才倒放")——这个改动已经落地,且**已经把磁盘上的 `CloseDrawer_stack4.track.json`/`CloseDrawer.track.json` 覆盖成了倒放版本**(不是运行时临时生成,是真的重写了这两个共享文件)。
- `compile_robot`([skill_generators.py:1148](../src/mujoco_skills/skills/skill_generators.py:1148)):articulation 分支(约 1243 行起)据此选择加载 Open 的轨迹倒放,还是加载原始 Close 文件。

**这一整块(纯倒放机制本身)测试良好,应该保留**——已验证磁盘上两个 Close 文件的帧数/首尾帧值都和对应 Open 精确匹配。

### 4. 【已解决】倒放后机器人自身关节要不要动 / 从哪个站位开始

> 最终结论见顶部「最终解决方案」。下面保留当初的分析脉络。


`OpenDrawer` 这段录制demo本身包含"走过去→伸手→拉开"的完整动作,倒放后自然对应"推回去→收手→走开"。但如果**原样播放**倒放轨迹里机器人自己的关节(底盘 forward/side/yaw + 手臂 7 关节 + 手指),会出现:

- 底盘从 `place` 步骤结束时的站位(即 `navigate(drawer_left)` 算出的、面向抽屉正前方的新站位),**移动**到 `OpenDrawer` 自己录制demo**最开始走过去之前**的那个起始站位——这两个站位是**用两套完全不同的机制算出来的**(新站位用 `standoff_for_point`;`OpenDrawer` 自己的起始站位是原始 robocasa demo 录制时随便站的位置),彼此没有任何关联,所以看起来像是"关门时机器人无缘无故走去了别的地方"。

**我(上一轮)的应对**:把"抽屉滑轨等 fixture 关节"和"机器人自身关节"分开处理——只有 fixture 关节走"锚定到当前值→线性衰减收敛到 0(完全关闭)"的修正,机器人自身的底盘/手臂/手指在整个 Close 过程中**冻结在 `place` 结束时的姿态**(位置见 [skill_generators.py:909](../src/mujoco_skills/skills/skill_generators.py:909) `_replay_with_resting_object`,尤其 `_target()` 内部函数约 980 行:`if jn not in fixture_names: return anchor[jn]`)。

**用户反馈:这个"冻结"是完全错误的行为——机器人应该真实播放录制的动作(底盘走位+手臂动作),不应该静止不动。**

## 需要新会话解决的核心张力

当初的矛盾:place 站位 ≠ close demo 站位(差约 0.5m),而 place→close 之间没有过渡步骤:

- **如果让机器人自身关节原样播放倒放轨迹(v3)**:底盘/手臂在 place→close 接缝处瞬移到 demo 起始姿态。
- **anchor+taper(v2)**:底盘从 place 位斜拖到 demo 位(用户看到的怪动作)。
- **冻结机器人自身关节(v1)**:动作"死板"、不真实(用户否决)。

**最终采用**:在 close 前插入一段 reposition 桥接(见顶部「最终解决方案」),让机器人先走回 close demo 站位、手臂伸到入场姿态,再回放。这样接缝连续、close 回放底盘零漂移,且 `_apply_track_last_frame` 天然把结束姿态往下线程给后续步骤——顶部方向清单里的 Option 1 + Option 2 的融合(用桥接把二者接上,而不是二选一)。

## 现状代码位置速查

| 内容 | 位置 |
|------|------|
| 物理跟随核心(落定+接触仿真) | `_settle_object_physically`([:835](../src/mujoco_skills/skills/skill_generators.py:835))、`_replay_with_resting_object`([:909](../src/mujoco_skills/skills/skill_generators.py:909)) |
| 倒放机制 | `_reverse_track`([:1019](../src/mujoco_skills/skills/skill_generators.py:1019)) |
| **reposition 桥接(本次解决方案)** | `_skill_entry_pose`([:1085](../src/mujoco_skills/skills/skill_generators.py:1085) 附近)、`gen_reposition_for_skill`([:1101](../src/mujoco_skills/skills/skill_generators.py:1101) 附近);接入点在 `compile_robot` articulation 分支的 `held is None` 门控处 |
| `_replay_with_resting_object` 内的 `_target()` | 已统一成对所有关节 anchor+taper(不再区分 fixture / 机器人关节);有了 reposition 后 anchor 恒等,taper 退化成绝对回放 |
| 是否倒放的判定 + 状态线程 | `compile_robot`([:1148](../src/mujoco_skills/skills/skill_generators.py:1148)),articulation 分支约 1320-1400 行 |
| `close_to_open` / `reverse_always` 映射构建 | `compile_plan`([:1384](../src/mujoco_skills/skills/skill_generators.py:1384) 附近) |
| 磁盘上已被覆盖的文件 | `frontend/public/trajectories/layout042_study/tracks/robot{0,1}/CloseDrawer.track.json`、`CloseDrawer_stack4.track.json`(倒放版本);`reposition_<skill>.track.json`、`CloseDrawer_stack4_with_mug_1.track.json`(compile 时按 plan 重新生成) |
| 抽屉正前方站位修复(无争议,已确认) | `build_skills_manifest.py` 的 A3 段落,改用 `standoff_for_point` |

## 如何复现/测试

```bash
# 1. 确认端口干净(Windows 下反复重启容易留下僵尸进程,同一端口多个监听会导致
#    "直接调 Python 对、走 HTTP 又不对"的诡异不一致 —— 每次重启前务必确认 0 个监听者):
#    PowerShell: Get-NetTCPConnection -LocalPort 8899 -State Listen
#    如有则 taskkill /F /T /PID <pid>,重复直到查询为空

# 2. 起 skill_service
uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service

# 3. 真实端到端(另一个终端,需 .env 里的 OPENAI_API_KEY):
uv run python -m mujoco_skills.orchestrator --yes "把一个杯子放进 drawer_left"

# 4. 检查生成的 track:期望(reposition 方案生效后)
#    - CloseDrawer_..._with_mug_1 的底盘 range ≈ 0(机器人稳稳站在 close 站位,不再漂移)
#    - CloseDrawer_..._with_mug_1 的手臂 robot0_joint1 range 明显 > 0(真实推门动作)
#    - reposition_CloseDrawer_stack4.track.json 存在,底盘 first≈place 站位、last≈close 站位
uv run python -c "
import json
d = json.loads(open('frontend/public/trajectories/layout042_study/tracks/robot0/CloseDrawer_stack4_with_mug_1.track.json',encoding='utf-8').read())
for jn in ['mobilebase0_joint_mobile_forward','mobilebase0_joint_mobile_side','mobilebase0_joint_mobile_yaw','robot0_joint1']:
    ch = d['channels'][jn]
    vals = [v[0] for v in ch]
    print(jn, 'range=', max(vals)-min(vals))
"

# 5. 前端可视化:frontend dev server + /debug 页面,"Compile drawer plan" 按钮
#    (frontend/src/plan/samplePlan.ts 的 SAMPLE_PLAN_DRAWER 常量)。
#    注意:改了 skill_generators.py 后必须重启 skill_service,前端才会拿到新逻辑。

# 6. 离线验证(推荐,绕开端口坑,不依赖 HTTP 服务,直接调 sg.compile_plan):
#    调 compile_plan 后检查 items 里是否出现 reposition_* item(order=close-0.5)、
#    close 回放底盘 range 是否为 0、手臂是否在动、mug 每帧位移是否平滑(无弹跳)。
#    这次会话用的验证脚本逻辑:构造 SAMPLE_PLAN_DRAWER 等价 plan -> compile_plan ->
#    遍历 result['items'] 打印 (order,duration,label) 与各通道 range。
```

## 环境提醒

- `OPENAI_API_KEY` 从 `.env` 读;公司网络 TLS 需 `truststore.inject_into_ssl()`(已在 orchestrator `__main__.py`)。
- **Windows 端口坑(反复踩过,请务必注意)**:`pkill -f skill_service` 杀不掉 uv 的孙进程;必须用 `Get-NetTCPConnection -LocalPort 8899 -State Listen` 查真实 PID 后 `taskkill /F /T /PID`,而且**可能需要循环多次**才能清干净(这次会话里一次性积累过 3-4 个僵尸进程同时监听同一端口)。重启服务前务必确认查询结果为空,否则验证结果会随机忽真忽假。
- 改了 `skill_generators.py`/`build_skills_manifest.py` 后必须重启 `skill_service` 才生效(它启动时会缓存 rig)。
