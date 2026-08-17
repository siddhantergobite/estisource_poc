from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from cad_engine.occt_engine import analyze_shape, export_shape, load_shape
from server.main import DOCUMENTS, app


def sample_step_bytes() -> bytes:
    return export_shape(BRepPrimAPI_MakeBox(100, 50, 25).Shape(), "step")


def test_api_upload_edit_reset_and_download():
    client = TestClient(app)
    uploaded = client.post("/api/documents", files={"file": ("sample.step", sample_step_bytes(), "application/step")})

    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["engine"] == "occt"
    assert payload["source_format"] == "step"
    assert payload["solid_count"] == 1
    assert payload["editing_available"] is True
    assert payload["length"] == pytest.approx(100)
    assert payload["mesh"]["triangle_count"] == 12
    assert payload["detected_features"]

    document_id = payload["document_id"]
    edited = client.post(
        f"/api/documents/{document_id}/parameters",
        json={"length": 200, "breadth": 100, "height": 50, "angle": 20},
    )
    assert edited.status_code == 200
    assert edited.json()["length"] == 200
    assert edited.json()["height"] == 50
    assert edited.json()["angle"] == 20

    downloaded = client.get(f"/api/documents/{document_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/step"
    assert downloaded.content.startswith(b"ISO-10303-")

    converted = client.get(f"/api/documents/{document_id}/download?format=iges")
    assert converted.status_code == 200
    assert converted.headers["content-type"] == "application/iges"
    assert analyze_shape(load_shape(converted.content, "iges"), include_mesh=False).face_count > 0

    reset = client.post(f"/api/documents/{document_id}/reset")
    assert reset.status_code == 200
    assert reset.json()["length"] == pytest.approx(100)
    DOCUMENTS.clear()


def test_complex_model_supports_whole_model_editing():
    client = TestClient(app)
    sample = Path("samples/complex/iges_hammer_reference_sample.iges")
    uploaded = client.post("/api/documents", files={"file": (sample.name, sample.read_bytes(), "application/iges")})

    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["editing_available"] is True
    assert payload["mode"] == "freeform"
    assert payload["detected_features"]
    assert [parameter["key"] for parameter in payload["parameters"]] == ["length", "breadth", "height", "angle"]
    assert all(parameter["editable"] is True for parameter in payload["parameters"])

    edited = client.post(
        f"/api/documents/{payload['document_id']}/parameters",
        json={"length": 0.2, "breadth": 0.3, "height": 0.1, "angle": 15},
    )
    assert edited.status_code == 200
    assert edited.json()["length"] == pytest.approx(0.2)
    assert edited.json()["breadth"] == pytest.approx(0.3)
    assert edited.json()["height"] == pytest.approx(0.1)
    assert edited.json()["angle"] == pytest.approx(15)
    DOCUMENTS.clear()


def test_api_rejects_legacy_2d_formats():
    client = TestClient(app)
    response = client.post("/api/documents", files={"file": ("drawing.dxf", b"not a 3D model", "application/dxf")})

    assert response.status_code == 400
    assert "step" in response.json()["detail"].lower()
