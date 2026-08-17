"""HTTP API for STEP/IGES upload, 3D inspection, editing, and export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from cad_engine.occt_engine import (
    SUPPORTED_FORMATS,
    analyze_shape,
    apply_freeform_parameters,
    detect_geometry_features,
    export_shape,
    extract_pmi,
    load_shape,
)


MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_DOCUMENTS = 20


@dataclass
class StoredDocument:
    document_id: str
    filename: str
    original_bytes: bytes
    working_bytes: bytes
    source_format: str
    parameter_state: dict[str, float]
    pmi_dimensions: list[dict]


class ParameterRequest(BaseModel):
    length: float = Field(gt=0)
    breadth: float = Field(gt=0)
    height: float = Field(gt=0)
    angle: float = 0.0


DOCUMENTS: dict[str, StoredDocument] = {}

app = FastAPI(title="Open CAD Engine 3D API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _canonical_format(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail="Supported 3D CAD files are .step, .stp, .iges, and .igs.")
    return "step" if extension in {"step", "stp"} else "iges"


def _load_document_shape(document: StoredDocument):
    try:
        return load_shape(document.working_bytes, document.source_format)
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the 3D CAD model: {exc}") from exc


def _initial_parameters(shape) -> dict[str, float]:
    analysis = analyze_shape(shape, include_mesh=False)
    return {"length": round(analysis.length, 6), "breadth": round(analysis.breadth, 6), "height": round(analysis.height, 6), "angle": 0.0}


def _is_simple_model(analysis) -> bool:
    """Permit generic scaling only for small, single-part geometry."""

    return analysis.face_count <= 20 and analysis.edge_count <= 80 and analysis.solid_count <= 1


def _analysis_payload(document: StoredDocument, shape=None) -> dict:
    shape = shape or _load_document_shape(document)
    analysis = analyze_shape(shape)
    values = document.parameter_state
    is_simple = _is_simple_model(analysis)
    editing_available = True
    detected_features = detect_geometry_features(shape)
    dimension_label = "Length" if is_simple else "Overall length"
    breadth_label = "Breadth" if is_simple else "Overall breadth"
    height_label = "Height" if is_simple else "Overall height"
    parameters = [
        {"key": "length", "label": dimension_label, "value": values["length"], "unit": "model units", "editable": editing_available},
        {"key": "breadth", "label": breadth_label, "value": values["breadth"], "unit": "model units", "editable": editing_available},
        {"key": "height", "label": height_label, "value": values["height"], "unit": "model units", "editable": editing_available},
        {"key": "angle", "label": "Z angle", "value": values["angle"], "unit": "deg", "editable": editing_available},
    ]
    return {
        "document_id": document.document_id,
        "filename": document.filename,
        "source_format": document.source_format,
        "engine": "occt",
        "mode": "freeform",
        "editing_available": editing_available,
        "complexity": "simple" if is_simple else "complex",
        "complexity_reason": "Simple geometry supports free-form editing." if is_simple else "Complex geometry supports whole-model scaling and rotation. Named feature parameters require a designer-defined schema.",
        "units": "model units",
        "length": values["length"],
        "breadth": values["breadth"],
        "height": values["height"],
        "angle": values["angle"],
        "parameters": parameters,
        "detected_features": detected_features,
        "pmi_dimensions": document.pmi_dimensions,
        "pmi_summary": {
            "count": len(document.pmi_dimensions),
            "source": "AP242 semantic PMI" if document.pmi_dimensions else None,
            "editable_count": sum(1 for dimension in document.pmi_dimensions if dimension["editable"]),
        },
        "bounds": {
            "min": [analysis.min_x, analysis.min_y, analysis.min_z],
            "max": [analysis.max_x, analysis.max_y, analysis.max_z],
            "length": analysis.length,
            "breadth": analysis.breadth,
            "height": analysis.height,
        },
        "solid_count": analysis.solid_count,
        "face_count": analysis.face_count,
        "edge_count": analysis.edge_count,
        "vertex_count": analysis.vertex_count,
        "valid": analysis.valid,
        "mesh": analysis.mesh,
        "constraints": {"valid": True, "message": "Free-form mode: dimensions and Z rotation may change independently."},
    }


def _get_document(document_id: str) -> StoredDocument:
    document = DOCUMENTS.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="CAD document not found or session expired.")
    return document


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "occt", "formats": "STEP, IGES"}


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "model.step").name
    source_format = _canonical_format(filename)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded CAD file is empty.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="3D CAD files are limited to 50 MB in this PoC.")
    try:
        shape = load_shape(data, source_format)
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the {source_format.upper()} model: {exc}") from exc
    if len(DOCUMENTS) >= MAX_DOCUMENTS:
        DOCUMENTS.pop(next(iter(DOCUMENTS)))
    document_id = uuid4().hex
    document = StoredDocument(
        document_id,
        filename,
        data,
        data,
        source_format,
        _initial_parameters(shape),
        extract_pmi(data, source_format),
    )
    DOCUMENTS[document_id] = document
    return _analysis_payload(document, shape)


@app.post("/api/documents/{document_id}/parameters")
def apply_parameters(document_id: str, request: ParameterRequest) -> dict:
    document = _get_document(document_id)
    shape = _load_document_shape(document)
    target = {key: round(float(value), 6) for key, value in request.model_dump().items()}
    try:
        updated = apply_freeform_parameters(shape, document.parameter_state, target)
        working_bytes = export_shape(updated, document.source_format)
    except Exception as exc:  # noqa: BLE001 - CAD kernel errors are client-edit errors
        raise HTTPException(status_code=400, detail=f"Could not update the CAD model: {exc}") from exc
    document.working_bytes = working_bytes
    document.parameter_state = target
    return _analysis_payload(document, updated)


@app.post("/api/documents/{document_id}/reset")
def reset_document(document_id: str) -> dict:
    document = _get_document(document_id)
    document.working_bytes = document.original_bytes
    shape = _load_document_shape(document)
    document.parameter_state = _initial_parameters(shape)
    return _analysis_payload(document, shape)


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: str, format: str | None = Query(default=None)) -> Response:
    document = _get_document(document_id)
    requested_format = document.source_format if format is None else format.lower().lstrip(".")
    if requested_format in {"stp", "step"}:
        requested_format = "step"
    elif requested_format in {"igs", "iges"}:
        requested_format = "iges"
    else:
        raise HTTPException(status_code=400, detail="Export format must be STEP or IGES.")
    try:
        output_bytes = document.working_bytes if requested_format == document.source_format else export_shape(_load_document_shape(document), requested_format)
    except Exception as exc:  # noqa: BLE001 - return export details to the client
        raise HTTPException(status_code=400, detail=f"Could not export the model as {requested_format.upper()}: {exc}") from exc
    media_type = "application/step" if requested_format == "step" else "application/iges"
    extension = "step" if requested_format == "step" else "iges"
    output_name = f"{Path(document.filename).stem}_edited.{extension}"
    return Response(
        content=output_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )
