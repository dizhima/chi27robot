# Backend

场景打开（XML → MJB）、轨迹列表、前端右侧 Codex 终端桥接，并**自动托管 skill/plan 服务**（`src/mujoco_skills/service/skill_service.py`）。

默认监听：**http://127.0.0.1:8787**（并自动在 **:8899** 拉起 skill_service）

## 启动

首次：

```powershell
cd backend
npm install
npm run dev
```

之后直接：

```powershell
cd backend
npm run dev
```

成功时终端会打印类似：

```
Codex bridge listening on http://127.0.0.1:8787
Starting skill_service (mujoco 3.10.0) on http://127.0.0.1:8899 ...
```

健康检查：`http://127.0.0.1:8787/api/health`（后端）、`http://127.0.0.1:8899/health`（skill_service）

## skill_service（自动托管）

前端的 `compile_plan` / `standoff` 由常驻的 `src/mujoco_skills/service/skill_service.py` 提供（它一次性预加载 MuJoCo 模型与 rig，随后毫秒级响应）。**后端启动时会自动 `uv run` 拉起它**，退出时（Ctrl+C）连带子进程一起清理，崩溃会自动重启（10s 内连崩 5 次则放弃并提示）。

- 因此**不需要**再手动另开 skill_service——`npm run dev` 一条命令即可。
- 前端直连 `http://127.0.0.1:8899`（`VITE_SKILL_SERVICE_URL` 可覆盖）；warm 需几秒，期间 `compile_plan` 会短暂报错属正常。
- ⚠️ 别再手动另跑一个 skill_service，否则第二个会撞 8899 端口 → 连崩 → 自动放弃。要手动管理时用 `SKILL_SERVICE_AUTOSTART=0` 关掉自动启动。

## 依赖

| 依赖 | 用途 |
|------|------|
| Node.js 18+ | 运行 `server.js` |
| [uv](https://docs.astral.sh/uv/) | 编译 `.mjb`（`compile_scene_mjb.py`）+ 拉起 `skill_service.py`；须在 PATH |
| MuJoCo **3.10.0** | 须与前端 `@mujoco/mujoco` WASM 版本一致；skill_service 也用此版本 |
| `codex`（可选） | 前端 Terminal 面板里启动 Codex CLI；没有也能开场景 |

## 和前端一起用

另开一个终端：

```powershell
cd frontend
npm run dev
```

前端默认连 `http://127.0.0.1:8787`（`VITE_CODEX_BACKEND_URL` 覆盖）与 `http://127.0.0.1:8899`（`VITE_SKILL_SERVICE_URL` 覆盖）。skill_service 已由后端自动托管，无需单开。

## 常用环境变量（可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `CODEX_BACKEND_HOST` | `127.0.0.1` | 监听地址 |
| `CODEX_BACKEND_PORT` | `8787` | 端口 |
| `MUJOCO_REACT_PUBLIC_DIR` | `../frontend/public` | 场景与轨迹根目录 |
| `MUJOCO_REACT_SCENE_PATH` | `public/assets/robocasa/layout042_study.xml` | 初始场景；skill_service 与 orchestrator 会从同一场景名派生 manifest、standoffs、tracks |
| `CODEX_COMMAND` | `codex` | 终端里启动的 CLI |
| `CODEX_WORKSPACE` | 仓库根目录 `mujoco-skill-playground` | Codex 工作目录 |
| `SKILL_SERVICE_PORT` | `8899` | 自动托管的 skill_service 端口 |
| `SKILL_SERVICE_AUTOSTART` | `1` | 设 `0` 关闭自动启动（改为手动运行 skill_service） |

## 主要接口

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/scene/current` | 当前场景会话 |
| POST | `/api/scene/open` | 打开场景（XML 会自动编译/复用 `.mjb`） |
| GET | `/api/trajectories` | 列出 `public/trajectories/` |
| GET | `/api/terminal/events` | 终端 SSE |
| POST | `/api/terminal/input` | 向 Codex PTY 写输入 |

> `compile_plan` / `standoff` / `manifest` 不在本后端，而在 skill_service（**:8899**），前端直连。

`/api/scene/open` 会同步重启 `:8899` 与 `:8900`，并等待两者的健康检查确认新场景后才返回成功；启动失败时会恢复前一个 active scene，避免 viewer 和控制服务使用不同布局。
>
> Codex 只有在前端 Terminal 面板（`/debug`）连上 `/api/terminal/events` 时才**惰性启动**；启动后端本身、以及只用精简页 `/`，都不会拉起 Codex（不耗 token）。
