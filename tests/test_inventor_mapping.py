from __future__ import annotations

from types import SimpleNamespace

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from cad_engine.inventor_adapter import InventorAdapter, _feature_bindings
from cad_engine.occt_engine import analyze_shape, resolve_preview_face_ids


class FakeCollection:
    def __init__(self, *items):
        self._items = list(items)
        self.Count = len(self._items)

    def Item(self, index):
        return self._items[index - 1]


def test_inventor_parameter_is_bound_to_feature_and_role():
    parameter = SimpleNamespace(Name="d0", Expression="10 mm", Value=1.0, Units="mm", Comment="")
    feature = SimpleNamespace(
        Name="Extrusion1",
        Parameters=FakeCollection(parameter),
        Faces=FakeCollection(object(), object(), object()),
        Extent=SimpleNamespace(Distance=parameter),
    )
    features = FakeCollection(feature)
    features.ExtrudeFeatures = FakeCollection(feature)
    document = SimpleNamespace(ComponentDefinition=SimpleNamespace(Features=features))

    bindings = _feature_bindings(document)
    records = InventorAdapter._parameter_records(FakeCollection(parameter), document)

    assert bindings["d0"][0]["feature_name"] == "Extrusion1"
    assert bindings["d0"][0]["parameter_role"] == "Distance"
    assert records[0].mapping_status == "feature"
    assert records[0].label == "Extrusion1 · Distance"
    assert records[0].to_dict()["feature_bindings"][0]["face_count"] == 3


def test_occt_preview_contains_triangle_face_ids():
    mesh = analyze_shape(BRepPrimAPI_MakeBox(10, 20, 30).Shape()).mesh

    assert len(mesh["triangle_face_ids"]) == mesh["triangle_count"]
    assert len(mesh["indices"]) == mesh["triangle_count"] * 3
    assert set(mesh["triangle_face_ids"]) == set(range(6))


def test_preview_face_matching_uses_normalized_feature_boxes():
    shape = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
    face_ids, score = resolve_preview_face_ids(
        shape,
        [{"min": [0, 0, 0], "max": [10, 20, 0]}],
        {"min": [0, 0, 0], "max": [10, 20, 30]},
    )

    assert face_ids
    assert score == pytest.approx(0, abs=1e-6)


def test_preview_face_matching_falls_back_to_feature_region():
    shape = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
    face_ids, score = resolve_preview_face_ids(
        shape,
        [],
        {"min": [0, 0, 0], "max": [10, 20, 30]},
        {"min": [0, 0, 0], "max": [10, 20, 30]},
    )

    assert set(face_ids) == set(range(6))
    assert score == pytest.approx(0.025)
