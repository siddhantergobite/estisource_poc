from __future__ import annotations

from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
import pytest

from cad_engine.occt_engine import analyze_shape, apply_freeform_parameters, export_shape, load_shape


def test_occt_analyzes_and_meshes_a_solid():
    analysis = analyze_shape(BRepPrimAPI_MakeBox(100, 50, 25).Shape())

    assert analysis.solid_count == 1
    assert analysis.face_count == 6
    assert analysis.length == pytest.approx(100)
    assert analysis.breadth == pytest.approx(50)
    assert analysis.height == pytest.approx(25)
    assert analysis.mesh["triangle_count"] == 12
    assert analysis.valid is True


def test_freeform_edit_scales_and_rotates_the_shape():
    shape = BRepPrimAPI_MakeBox(100, 50, 25).Shape()
    updated = apply_freeform_parameters(
        shape,
        {"length": 100, "breadth": 50, "height": 25, "angle": 0},
        {"length": 200, "breadth": 100, "height": 50, "angle": 20},
    )
    analysis = analyze_shape(updated)

    assert analysis.height == pytest.approx(50)
    assert analysis.valid is True
    assert analysis.mesh["triangle_count"] == 12


def test_step_round_trip_preserves_a_solid():
    original = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
    exported = export_shape(original, "step")
    restored = load_shape(exported, "step")

    assert exported.startswith(b"ISO-10303-")
    assert analyze_shape(restored, include_mesh=False).solid_count == 1


def test_step_primitive_exchange_fixture_is_imported():
    fixture = Path("samples/step_block_part_sample.step")
    restored = load_shape(fixture.read_bytes(), "step")
    analysis = analyze_shape(restored, include_mesh=False)

    assert analysis.solid_count == 1
    assert analysis.length == pytest.approx(40)
    assert analysis.breadth == pytest.approx(20)
    assert analysis.height == pytest.approx(8)


def test_step_primitive_assembly_exchange_fixture_is_imported():
    fixture = Path("samples/step_assembly_exchange_sample.step")
    restored = load_shape(fixture.read_bytes(), "step")
    analysis = analyze_shape(restored, include_mesh=False)

    assert analysis.solid_count == 3
    assert analysis.length == pytest.approx(60)
    assert analysis.breadth == pytest.approx(35)
