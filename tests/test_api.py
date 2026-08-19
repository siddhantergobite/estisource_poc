from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from cad_engine.occt_engine import analyze_shape, export_shape, load_shape, make_parametric_hammer_shape
from server.main import DOCUMENTS, app


def sample_step_bytes() -> bytes:
    return export_shape(BRepPrimAPI_MakeBox(100, 50, 25).Shape(), "step")


def parametric_hammer_step_bytes() -> bytes:
    return export_shape(
        make_parametric_hammer_shape({
            "L1": 300,
            "L2": 80,
            "L3": 220,
            "HandleDiameter": 32,
            "HeadWidth": 120,
            "HeadHeight": 60,
            "HeadThickness": 42,
            "ClawAngle": 25,
        }),
        "step",
    )


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


def test_parametric_hammer_rebuilds_with_constraints_and_exports():
    client = TestClient(app)
    uploaded = client.post(
        "/api/documents",
        files={"file": ("ParametricHammer.step", parametric_hammer_step_bytes(), "application/step")},
    )

    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["mode"] == "parametric"
    assert [parameter["key"] for parameter in payload["parameters"]] == [
        "L1", "L2", "L3", "HandleDiameter", "HeadWidth", "HeadHeight", "HeadThickness", "ClawAngle"
    ]

    edited = client.post(
        f"/api/documents/{payload['document_id']}/parameters",
        json={"values": {"L1": 420, "L2": 120, "HandleDiameter": 36, "ClawAngle": 30}},
    )
    assert edited.status_code == 200
    edited_payload = edited.json()
    assert edited_payload["constraints"]["valid"] is True
    assert next(parameter["value"] for parameter in edited_payload["parameters"] if parameter["key"] == "L3") == 300
    assert edited_payload["length"] > payload["length"]

    invalid = client.post(
        f"/api/documents/{payload['document_id']}/parameters",
        json={"values": {"L1": 100, "L2": 120}},
    )
    assert invalid.status_code == 400
    assert "L1 must be greater than L2" in invalid.json()["detail"]

    exported = client.get(f"/api/documents/{payload['document_id']}/download?format=iges")
    assert exported.status_code == 200
    assert analyze_shape(load_shape(exported.content, "iges"), include_mesh=False).face_count > 0

    exported_name = exported.headers["content-disposition"].split('filename="', 1)[1].rstrip('"')
    reuploaded = client.post(
        "/api/documents",
        files={"file": (exported_name, exported.content, "application/iges")},
    )
    assert reuploaded.status_code == 200
    reuploaded_values = {parameter["key"]: parameter["value"] for parameter in reuploaded.json()["parameters"]}
    assert reuploaded_values["L1"] == 420
    assert reuploaded_values["L2"] == 120
    assert reuploaded_values["L3"] == 300
    assert reuploaded_values["ClawAngle"] == 30

    generic_reupload = client.post(
        "/api/documents",
        files={"file": ("ParametricHammer_edited.iges", exported.content, "application/iges")},
    )
    assert generic_reupload.status_code == 200
    generic_values = {parameter["key"]: parameter["value"] for parameter in generic_reupload.json()["parameters"]}
    assert generic_values["L2"] == 120
    assert generic_values["L3"] == 300
    assert generic_values["ClawAngle"] == 30
    DOCUMENTS.clear()


def test_api_rejects_legacy_2d_formats():
    client = TestClient(app)
    response = client.post("/api/documents", files={"file": ("drawing.dxf", b"not a 3D model", "application/dxf")})

    assert response.status_code == 400
    assert "step" in response.json()["detail"].lower()
