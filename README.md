# Open CAD Engine

Open CAD Engine is a 3D CAD inspection and editing proof of concept. It combines a React/Three.js frontend, a FastAPI backend, Open CASCADE geometry processing, and native Autodesk Inventor automation.

The application supports:

- STEP: `.step`, `.stp`
- IGES: `.iges`, `.igs`
- Autodesk Inventor parts: `.ipt`
- Autodesk Inventor assemblies: `.iam`

## What the POC does

### STEP and IGES workflow

1. The frontend uploads the CAD file to the FastAPI backend.
2. Open CASCADE imports the original file as a B-Rep shape.
3. The backend validates the shape and extracts bounds, solids, faces, edges, vertices, measurements, and mesh data.
4. Open CASCADE tessellates the B-Rep faces into triangles. Every triangle keeps its source preview face ID.
5. The frontend renders the mesh with Three.js.
6. The user can orbit, zoom, pan, hover, and select model faces.
7. The backend applies supported free-form or schema-driven edits.
8. The edited model can be exported as STEP or IGES.

### Native Inventor workflow

Native Inventor files require the full Autodesk Inventor desktop application to be installed and activated on the Windows machine running the backend.

1. The backend opens the `.ipt` or `.iam` file through the Inventor COM API.
2. Inventor parameters and feature-history relationships are read from the native document.
3. Parameters are displayed in the frontend with feature-based labels such as `Extrusion1 · d0`.
4. The user changes an editable parameter.
5. Inventor rebuilds the native feature history.
6. Inventor exports a temporary STEP preview.
7. Open CASCADE imports and validates that preview.
8. The frontend refreshes the 3D model and preserves face-level highlighting where the feature-to-preview mapping is resolved.
9. The native Inventor document can be exported again as `.ipt` or `.iam`; STEP and IGES exports are also available.

The original uploaded production file is kept immutable. Editing is performed on a session-scoped working copy.

## Architecture

```text
React + Three.js frontend
        │
        │ HTTP JSON and multipart upload
        ▼
FastAPI backend
        │
        ├── STEP / IGES ── Open CASCADE/OCP ── B-Rep, PMI, mesh, export
        │
        └── IPT / IAM ── pywin32 ── Autodesk Inventor COM API
                              │
                              └── rebuilt STEP preview ── Open CASCADE/OCP

Inventor feature faces
        │
        └── custom geometry matching ── OCCT preview face IDs
                                             │
                                             └── Three.js yellow highlight
```

## Highlighting and naming

The yellow surface highlight and green feature label are created by the application. No single CAD viewer library provides the complete Inventor-to-preview behavior.

- Inventor provides feature names, parameters, and native feature faces.
- Open CASCADE/OCP provides B-Rep faces, topology, geometry analysis, and triangle data.
- The backend assigns each preview triangle its source OCCT face ID.
- Custom mapping code compares Inventor feature-face signatures with OCCT preview-face signatures.
- Three.js raycasting identifies the face under the pointer.
- Three.js renders a separate transparent yellow mesh using the matched face IDs.
- React displays the matching feature names in the green label.

When several Inventor features affect the same preview face, the label can contain more than one feature name. This represents the available native feature relationship; it does not mean that Three.js independently discovered those CAD feature names.

## Libraries and tools

### Backend Python dependencies

Declared in `pyproject.toml`:

- `cadquery-ocp` — Python bindings and packaged Open CASCADE kernel used for CAD import/export, B-Rep topology, geometry, validation, meshing, STEP/IGES, and XCAF/AP242 PMI access.
- `fastapi` — HTTP API framework.
- `python-multipart` — multipart file-upload parsing used by FastAPI.
- `pywin32` — Windows COM access used to communicate with Autodesk Inventor through `pythoncom` and `win32com.client`.
- `uvicorn[standard]` — ASGI server used to run FastAPI.

Used through the backend dependency stack:

- `pydantic` — request and response data validation used by FastAPI.

Open CASCADE modules used by this project include:

- `STEPControl` and `STEPCAFControl` — STEP import/export and assembly/semantic data.
- `IGESControl` — IGES import/export.
- `BRep`, `BRepAdaptor`, `BRepBuilderAPI`, `BRepBndLib`, and `BRepCheck` — B-Rep access, transforms, bounds, and shape validation.
- `BRepMesh` — tessellation of CAD faces into triangles.
- `TopExp`, `TopoDS`, and `TopAbs` — face, edge, vertex, solid, and topology traversal.
- `GeomAbs` — surface-type classification.
- `XCAFDoc`, `TDocStd`, `TDF`, and related XCAF modules — AP242 semantic PMI access.
- `gp` — points, vectors, axes, transforms, and geometric operations.

### Frontend dependencies

Declared in `frontend/package.json`:

- `react` — frontend component framework.
- `react-dom` — React browser rendering.
- `three` — WebGL 3D scene, camera, lights, materials, buffer geometry, mesh rendering, edges, and raycasting.
- `three/examples/jsm/controls/OrbitControls.js` — orbit, zoom, and pan controls.
- `lucide-react` — interface icons.
- `vite` — frontend development server and production bundler.
- `@vitejs/plugin-react` — Vite React transform/plugin.

There is no separate commercial CAD viewer in the frontend. The 3D preview is rendered by Three.js from mesh data generated by the backend.

### Development and testing tools

- `uv` — Python environment and dependency management using `pyproject.toml` and `uv.lock`.
- `npm` — frontend dependency management.
- `pytest` — backend test runner.
- `httpx2` — development HTTP client used by API tests.
- Autodesk Inventor desktop application — required only for native `.ipt` and `.iam` automation.

## Requirements

- Windows for native Inventor support.
- Python 3.11 or newer.
- Node.js and npm.
- Autodesk Inventor installed and activated for `.ipt` and `.iam` files.
- A valid Inventor installation must be available to the same Windows user/account that runs the backend.

STEP and IGES processing does not require Autodesk Inventor.

## Run locally

From the repository root, install Python dependencies and start the API:

```powershell
uv sync
uv run uvicorn server.main:app --reload --port 8000
```

In a second terminal, install and start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open:

- Frontend: `http://localhost:5173`
- API: `http://localhost:8000`
- Health check: `http://localhost:8000/api/health`

## Build and test

Run the backend tests from the repository root:

```powershell
uv run pytest -q
```

Build the frontend:

```powershell
cd frontend
npm run build
```

The frontend build may report a large JavaScript chunk warning because Three.js is included in the viewer bundle. This is a bundling optimization warning, not a build failure.

## Current editing boundaries

- Native Inventor parameters are rebuilt by Inventor itself, preserving the native feature-history workflow.
- STEP/IGES free-form editing changes supported model dimensions or transforms through Open CASCADE.
- Named feature editing for arbitrary neutral STEP/IGES files requires a model-specific parameter schema.
- AP242 PMI dimensions can be read from supported files, but they are not automatically editable unless a safe geometry rebuild operation is defined.
- A parameter may remain editable without a face highlight when its native feature relationship cannot be safely mapped to the exported preview.
- The original uploaded file is not overwritten during a normal edit session.

## Project structure

```text
cad_engine/
  occt_engine.py        Open CASCADE import, analysis, meshing, mapping, and export
  inventor_adapter.py   Autodesk Inventor COM adapter and native rebuild worker

server/
  main.py               FastAPI routes, document sessions, editing, and downloads

frontend/
  src/App.jsx           React UI, Three.js viewer, hover, and highlighting behavior
  src/styles.css        Application styling and viewer labels
  package.json          Frontend dependencies and scripts

tests/
  test_api.py           API behavior tests
  test_occt_engine.py   Open CASCADE geometry tests
  test_inventor_mapping.py  Inventor mapping tests

parametric_models/      Controlled parametric sample assets
samples/                STEP, IGES, AP242, and test model fixtures
tools/                  Development and model-generation utilities
pyproject.toml          Python dependencies and project configuration
uv.lock                Resolved Python dependency versions
```

## POC status

The main POC workflow is complete: upload, inspect, edit, rebuild, preview, hover-highlight, feature naming, reset, and export are implemented. Future production work would focus on persistent storage, authentication, background job processing, stronger topology correspondence for difficult models, and additional native CAD adapters.
