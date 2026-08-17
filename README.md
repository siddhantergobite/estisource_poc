# Open CAD Engine

An open-source STEP/IGES 3D CAD inspection and parametric editing PoC built with Open CASCADE through the `cadquery-ocp` Python wrapper, FastAPI, and a React/Vite frontend.

## Current workflow

1. React uploads a STEP or IGES model to the FastAPI backend.
2. The backend imports the original file into an Open CASCADE B-Rep shape and validates it.
3. The API returns 3D bounds, solids, faces, edges, vertices, and a tessellated mesh.
4. React renders the mesh in an orbitable Three.js viewer.
5. The user edits length, breadth, height, and Z angle in free-form PoC mode.
6. Open CASCADE regenerates the edited solid and the API exports it back to STEP or IGES.

For STEP AP242 files, the backend also reads semantic PMI through OCCT XCAF.
Those source dimensions are shown dynamically in the UI. They remain read-only
until a model-specific rebuild operation maps each dimension to real geometry;
an arbitrary B-Rep does not contain enough design intent to safely infer L1/L2/L3
edit operations.

The original uploaded file is kept immutable. Edited models are held as session-scoped working copies until versioned storage is added. Free-form scaling is implemented first; model-specific parameter schemas and constraints are the next layer.

## Run locally

Start the API in one terminal:

```powershell
uv sync
uv run uvicorn server.main:app --reload --port 8000
```

Start React in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

The frontend runs at `http://localhost:5173` and the API runs at `http://localhost:8000`.

Run tests with:

```powershell
uv run pytest
```

## Next engineering milestones

- Add designer-defined parameter schemas for named dimensions such as L1, L2, and L3.
- Add fixed/variable parameter controls and expression-based constraints.
- Add immutable production versions, exploration versions, and resumable drafts.
- Add PostgreSQL metadata plus object storage for multi-model tenancy.
- Add representative customer STEP/IGES fixtures and export fidelity checks.
