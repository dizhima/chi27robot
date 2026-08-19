# Human-Agent Multi-Robot Workflow

## Working Theme

Human-agent co-authoring workspace for multi-robot task planning and simulation.

At this stage, the target venue is intentionally undecided. The workflow should be developed first as a concrete user interaction and system pipeline, then later framed toward CHI, ICRA, or another venue.

## User-Proposed Workflow

### Multi-Robot Planning

- Initial scene editing
  - Add, delete, or move scene elements such as tables, cubes, cabinets, bins, conveyors, and robots.
- Overall goal to subtask allocation
  - Check whether existing robots in the scene can achieve the overall goal.
  - Decide whether extra robots are needed.
  - Verify required capabilities, such as:
    - manipulation for arm robots
    - navigation for mobile robots
    - inspection for drones
  - Verify reachability and feasibility, for example whether a mobile robot cannot use stairs.
  - Determine whether alternative robot-team compositions exist, such as:
    - two arms plus one mobile base
    - two mobile manipulators
  - Decide what subtask each robot should perform.
- Subtask schedule
  - Determine an execution schedule for subtasks.
  - Identify which subtasks can run in parallel, such as sorting tasks in the same workspace.
  - Identify which subtasks must be sequential, such as handoff tasks.

### Robot Execution

- Execute simulation based on the subtask schedule.
- Each robot type should have fixed fine-grained action recipes.
  - Arm pick-and-place should be decomposed into:
    - perception
    - approach
    - grasp
    - transport
    - placement
  - Mobile robot navigation should:
    - avoid obstacles and objects
    - keep safe distance
    - optionally follow routes authored by the user, such as a floor-plan sketch

## Proposed Interaction Flow

The current interaction flow to implement incrementally:

```text
用户自然语言输入目标
-> 前端显示 LLM 解析出来的 scene/task/subtasks
-> 用户可以修改
-> 用户点 Confirm
-> 后端 cosim 生成 plan + script + trajectory + report
-> 前端自动加载 trajectory
-> 用户看 viewer + metrics
-> 用户反馈“这里撞了/没夹住/换路线”
-> cosim 修改 plan/waypoints/recipe
```

## Notes For Implementation Planning

- The frontend should eventually make the parsed scene, task, subtasks, schedule, trajectory, and validation report visible and editable.
- The backend `cosim` workflow should eventually produce structured artifacts, not only free-form code or terminal output.
- The first implementation goal is to make the above loop concrete for one or two representative tasks before deciding the final research framing.
