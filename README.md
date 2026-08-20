# MuJoCo Multi-Robot Skill Playground

This project is a local multi-robot task-authoring and playback system built around
MuJoCo, RoboCasa scenes, React, and Python skill services.

The repository contains the source code. Large scenes, meshes, trajectories, and
study checkpoints are intentionally transferred separately and are not stored in
Git.

## Components

| Component | Location | Default port | Purpose |
| --- | --- | ---: | --- |
| Web UI | `frontend/` | 5173 | MuJoCo viewer, task authoring, plan review, and playback |
| Node bridge | `backend/` | 8787 | Scene switching, trajectory serving, checkpoints, and optional Codex terminal |
| Skill service | `src/mujoco_skills/service/` | 8899 | Loads the active MuJoCo scene and compiles plans into robot tracks |
| Orchestrator | `src/mujoco_skills/orchestrator/` | 8900 | LLM-assisted authoring and grounding |

Starting the Node backend also starts the skill service and orchestrator. They do
not normally need separate terminals. See `backend/README.md` for backend-specific
environment variables and API details.

## Prerequisites

- Windows PowerShell (the documented commands below use PowerShell)
- Node.js `^20.19.0` or `>=22.12.0`; Node.js 22 LTS or 24 is recommended
- npm
- [uv](https://docs.astral.sh/uv/)
- Git
- An OpenAI API key for AI authoring; basic scene viewing does not require one
- Codex CLI only if the `/debug` terminal panel is needed

The Python project requires Python 3.14 or newer. `uv` installs and manages the
project environment from `pyproject.toml` and `uv.lock`.

## First-time setup

### 1. Clone the source repository

```powershell
git clone <repository-url> mujoco-skill-playground
cd mujoco-skill-playground
```

### 2. Restore the runtime assets

Extract the separately transferred asset archive so that the resulting directory
is exactly:

```text
mujoco-skill-playground/
└── frontend/
    └── public/
        ├── assets/
        └── trajectories/
```

Do not accidentally create `frontend/public/public/` while extracting.

For the default `layout042_study` system, verify at least these files and
directories exist:

```text
frontend/public/
├── assets/robocasa/
│   ├── layout042_study.xml
│   ├── layout042_study.mjb
│   ├── layout042_study.scene_table.json
│   ├── layout042_study.navgrid.json
│   ├── layout042_study.navgrid.npz
│   ├── robocasa_assets/
│   └── robosuite_assets/
└── trajectories/layout042_study/
    ├── skills_manifest.json
    ├── standoffs.json
    └── tracks/
        ├── robot0/
        └── robot1/
```

`layout042_study.xml` directly references both `robocasa_assets/` and
`robosuite_assets/`, so copying only the XML or MJB is not sufficient for all
Python services.

The following data is not required for a clean first launch of the default study:

- `frontend/public/trajectories/layout042_study/tracks/_generated/`: generated
  plan cache; the backend clears it on startup and the skill service recreates it
- `tracks/_backups/`: development backups
- raw demonstration folders directly under
  `trajectories/layout042_study/robot0/` and `robot1/`: only needed when rebuilding
  canonical tracks
- `candidate_previews/`, preview images, and the duplicate `robocasa.zip`
- `frontend/public/legacy/layout042_sorting_candidates/`: archived candidate
  scenes used only by the corresponding development render scripts
- old warehouse, indoor, Franka, Fetch, Stretch, and non-RoboCasa assets when those
  scenes are no longer used
- `frontend/public/legacy/`: retired non-RoboCasa assets kept locally for reference;
  omit this directory from the default runtime archive
- trajectories for layouts other than the active study

If another scene is selected, it needs the same combination of scene XML/MJB,
referenced mesh and texture directories, scene metadata, and its matching
`frontend/public/trajectories/<scene-name>/` runtime files.

### 3. Install dependencies

From the repository root:

```powershell
uv sync

cd backend
npm ci

cd ..\frontend
npm ci

cd ..
```

Use `npm ci` for migration and clean setup because both Node projects include
lockfiles.

### 4. Configure environment variables

Create the local root configuration:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY` if AI authoring is required. Never commit
the real `.env` file.

Create the optional frontend configuration:

```powershell
Copy-Item frontend/.env.example frontend/.env
```

The defaults already point to ports 8787, 8899, and 8900, so endpoint variables
normally do not need to be changed.

### 5. Start the system

Open terminal 1:

```powershell
cd backend
npm run dev
```

Wait for the backend, skill service, and orchestrator to report that they are
listening. Open terminal 2:

```powershell
cd frontend
npm run dev
```

Open the URL printed by Vite, normally `http://localhost:5173/`.

- `/` is the study interface.
- `/debug` includes the development and Codex terminal interface.

Stop both foreground commands with `Ctrl+C`. The backend is responsible for
stopping the Python services it started.

## Verification

Health endpoints:

```text
http://127.0.0.1:8787/api/health
http://127.0.0.1:8899/health
http://127.0.0.1:8900/health
```

Run the automated checks from their respective directories:

```powershell
cd backend
npm test

cd ..\frontend
npm test
npm run build

cd ..
uv run --with pytest pytest
```

## Data kept outside Git

| Path | Migration policy |
| --- | --- |
| `frontend/public/` | Required runtime asset archive; curate it to the active scenes |
| `study/` | Optional separate study/checkpoint archive |
| `archive/` | Optional cold backup of retired scene exports |
| `assets/` | Legacy/source asset repositories; not needed by the default runtime package |
| `experiments/` | Local experiment workspace; archive only if its history is needed |
| `.agents/` | Local agent skills; reinstall or transfer separately if needed |

Dependencies and build outputs such as `.venv/`, `node_modules/`, and
`frontend/dist/` should be recreated on the destination machine rather than
copied.

## Common problems

- **Scene file not found:** confirm the ZIP was extracted under
  `frontend/public/`, not beside it or into a nested `public/public` directory.
- **MuJoCo reports a missing mesh or texture:** the XML's referenced asset
  directory was omitted. For `layout042_study`, restore both `robocasa_assets/`
  and `robosuite_assets/`.
- **Viewer works but planning is unavailable:** restore `skills_manifest.json`,
  `standoffs.json`, and `tracks/robot0`, `tracks/robot1` for the active scene.
- **Port already in use:** check ports 8787, 8899, and 8900 and stop only the
  process tree previously started for this repository.
- **AI authoring fails while the viewer works:** verify `OPENAI_API_KEY` in the
  root `.env` and confirm port 8900 is healthy.
