#!/usr/bin/env python3
"""
Smoke / mock tests for the #2537 ViewManager create_view workaround.

No live ArcGIS Online or Enterprise required. Run from the repo root::

    python -m unittest misc.tools.issue_2537.test_fix_viewmanager_create_2537 -v

Or::

    python misc/tools/issue_2537/test_fix_viewmanager_create_2537.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running this file directly without installing the package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from misc.tools.issue_2537 import apply, unapply  # noqa: E402
from misc.tools.issue_2537 import fix_viewmanager_create_2537 as fix  # noqa: E402

PARENT_ID = "parent123"
VIEW_ID = "view456"
QUERY = "Status <> 'Abandoned'"
PARENT_DATA = {
    "layers": [
        {"id": 0, "name": "Stations", "layerType": "ArcGISFeatureLayer"},
        {"id": 1, "name": "Mains", "layerType": "ArcGISFeatureLayer"},
    ]
}


def _layer(layer_id: int, url_base: str = "https://example.com/arcgis/rest/services/svc/FeatureServer"):
    lyr = MagicMock()
    lyr.url = f"{url_base}/{layer_id}"
    lyr.properties = {
        "fields": [
            {"name": "OBJECTID"},
            {"name": "GlobalID"},
            {"name": "Status"},
            {"name": "Secret"},
        ]
    }
    future = MagicMock()
    future.result.return_value = {"success": True}
    lyr.manager.update_definition.return_value = future
    return lyr


def _make_items():
    parent = MagicMock()
    parent.id = PARENT_ID
    parent.get_data.return_value = {
        "layers": [dict(lyr) for lyr in PARENT_DATA["layers"]]
    }
    parent.update.return_value = True

    view = MagicMock()
    view.id = VIEW_ID
    view.get_data.return_value = {}
    view.update.return_value = True
    view._gis = None  # set below

    return parent, view


def _make_manager(parent, view, *, is_agol: bool = True):
    gis = MagicMock()
    gis._is_arcgisonline = is_agol

    def _get(itemid=None, **kwargs):
        key = itemid or kwargs.get("itemid")
        if key == PARENT_ID:
            return parent
        if key == VIEW_ID:
            return view
        return parent

    gis.content.get.side_effect = _get
    parent._gis = gis
    view._gis = gis

    # Plain stub so type(self).add_to_definition is patchable (MagicMock.__class__ is not).
    class StubCollectionManager:
        add_to_definition = staticmethod(lambda *a, **k: {"success": True})

    mgr = StubCollectionManager()
    mgr._gis = gis
    mgr.properties = {"serviceItemId": PARENT_ID}
    return mgr, gis


class TestFieldVisibility(unittest.TestCase):
    def setUp(self):
        self.fields = [
            {"name": "OBJECTID"},
            {"name": "Status"},
            {"name": "Secret"},
        ]

    def test_star_makes_all_visible(self):
        result = fix._field_visibility(self.fields, ["OBJECTID", "*"])
        self.assertTrue(all(f["visible"] for f in result))

    def test_none_or_empty_makes_all_visible(self):
        self.assertTrue(all(f["visible"] for f in fix._field_visibility(self.fields, None)))
        self.assertTrue(all(f["visible"] for f in fix._field_visibility(self.fields, [])))

    def test_named_fields_case_insensitive(self):
        result = fix._field_visibility(self.fields, ["objectid", "STATUS"])
        by_name = {f["name"]: f["visible"] for f in result}
        self.assertTrue(by_name["OBJECTID"])
        self.assertTrue(by_name["Status"])
        self.assertFalse(by_name["Secret"])


class TestUpdateViewItemData(unittest.TestCase):
    def test_filters_layers_onto_view_not_parent(self):
        parent, view = _make_items()
        layer0 = _layer(0)
        fix._update_view_item_data(view, parent, [layer0])

        view.update.assert_called_once()
        data = view.update.call_args.kwargs["data"]
        self.assertEqual([lyr["id"] for lyr in data["layers"]], [0])
        parent.update.assert_not_called()

    def test_no_view_layers_copies_full_parent_data(self):
        parent, view = _make_items()
        fix._update_view_item_data(view, parent, None)
        view.update.assert_called_once_with(data=parent.get_data())


class TestAwaitAddToDefinition(unittest.TestCase):
    def test_awaits_future_and_refreshes(self):
        original = MagicMock()
        future = MagicMock()
        future.result.return_value = {"success": True}
        original.return_value = future

        wrapped = fix._awaiting_add_to_definition(original)
        mgr = MagicMock()
        out = wrapped(mgr, {"layers": []}, future=True)

        self.assertIs(out, future)
        future.result.assert_called_once()
        self.assertFalse(mgr._hydrated)
        mgr.refresh.assert_called_once()

    def test_sync_result_passthrough(self):
        original = MagicMock(return_value={"success": True})
        wrapped = fix._awaiting_add_to_definition(original)
        mgr = MagicMock()
        out = wrapped(mgr, {"layers": []}, future=False)
        self.assertEqual(out, {"success": True})
        mgr.refresh.assert_not_called()


class TestApplyUnapply(unittest.TestCase):
    def tearDown(self):
        unapply()

    def test_apply_unapply_idempotent(self):
        from arcgis.features.managers import FeatureLayerCollectionManager

        original = FeatureLayerCollectionManager.create_view
        self.assertTrue(apply())
        self.assertIsNot(FeatureLayerCollectionManager.create_view, original)
        self.assertFalse(apply())
        self.assertTrue(unapply())
        self.assertIs(FeatureLayerCollectionManager.create_view, original)
        self.assertFalse(unapply())


class TestCreateViewFixed(unittest.TestCase):
    def setUp(self):
        from arcgis.features.managers import FeatureLayerCollectionManager

        unapply()
        self._real_create_view = FeatureLayerCollectionManager.create_view

    def tearDown(self):
        from arcgis.features.managers import FeatureLayerCollectionManager

        unapply()
        FeatureLayerCollectionManager.create_view = self._real_create_view

    def _install_stock(self, parent, view, calls):
        """Replace create_view with a buggy stock impl that keeps the real signature."""
        import inspect

        from arcgis.features.managers import FeatureLayerCollectionManager

        sig = inspect.signature(self._real_create_view)

        def stock_create_view(*args, **kwargs):
            ba = sig.bind(*args, **kwargs)
            ba.apply_defaults()
            calls["bound"] = dict(ba.arguments)
            # Upstream bug: write filtered layers onto the parent.
            parent.update(data={"layers": [dict(PARENT_DATA["layers"][0])]})
            return view

        stock_create_view.__signature__ = sig
        FeatureLayerCollectionManager.create_view = stock_create_view
        return FeatureLayerCollectionManager

    def test_restores_parent_strips_query_and_applies_fields(self):
        parent, view = _make_items()
        layer0 = _layer(0)
        mgr, gis = _make_manager(parent, view, is_agol=True)
        calls = {}
        FeatureLayerCollectionManager = self._install_stock(parent, view, calls)
        self.assertTrue(apply())

        flc = MagicMock()
        flc.layers = [layer0]

        with patch(
            "arcgis.features.FeatureLayerCollection.fromitem", return_value=flc
        ):
            result = FeatureLayerCollectionManager.create_view(
                mgr,
                name="Stations_view",
                view_layers=[layer0],
                query=QUERY,
                visible_fields=["OBJECTID", "GlobalID", "Status"],
            )

        self.assertIs(result, view)

        # Stock path must not see query / visible_fields (avoids IndexError path).
        self.assertIsNone(calls["bound"].get("query"))
        self.assertIsNone(calls["bound"].get("visible_fields"))

        # Parent restored to original multi-layer JSON.
        restore_calls = [
            c.kwargs.get("data")
            for c in parent.update.call_args_list
            if c.kwargs.get("data") and len(c.kwargs["data"].get("layers", [])) == 2
        ]
        self.assertTrue(restore_calls, "parent should be restored with both layers")
        self.assertEqual(
            [lyr["id"] for lyr in restore_calls[-1]["layers"]], [0, 1]
        )

        # View receives filtered layer JSON (not left on the parent only).
        view_layer_updates = [
            c.kwargs["data"]
            for c in view.update.call_args_list
            if isinstance(c.kwargs.get("data"), dict) and "layers" in c.kwargs["data"]
        ]
        self.assertTrue(view_layer_updates)
        self.assertEqual([lyr["id"] for lyr in view_layer_updates[-1]["layers"]], [0])

        # Query / fields applied to each view layer.
        layer0.manager.update_definition.assert_called()
        values = layer0.manager.update_definition.call_args.args[0]
        self.assertEqual(values["viewDefinitionQuery"], QUERY)
        by_name = {f["name"]: f["visible"] for f in values["fields"]}
        self.assertTrue(by_name["OBJECTID"])
        self.assertTrue(by_name["Status"])
        self.assertFalse(by_name["Secret"])

    def test_applies_query_to_all_view_layers(self):
        parent, view = _make_items()
        lyr0, lyr1 = _layer(0), _layer(1)
        mgr, gis = _make_manager(parent, view, is_agol=False)
        calls = {}
        FeatureLayerCollectionManager = self._install_stock(parent, view, calls)
        self.assertTrue(apply())

        flc = MagicMock()
        flc.layers = [lyr0, lyr1]
        with patch(
            "arcgis.features.FeatureLayerCollection.fromitem", return_value=flc
        ):
            FeatureLayerCollectionManager.create_view(
                mgr,
                name="Both_view",
                query=QUERY,
                visible_fields=["*"],
            )

        self.assertEqual(lyr0.manager.update_definition.call_count, 1)
        self.assertEqual(lyr1.manager.update_definition.call_count, 1)
        for lyr in (lyr0, lyr1):
            values = lyr.manager.update_definition.call_args.args[0]
            self.assertEqual(values["viewDefinitionQuery"], QUERY)
            self.assertTrue(all(f["visible"] for f in values["fields"]))


class TestSmokeImport(unittest.TestCase):
    def test_package_exports(self):
        from misc.tools import issue_2537

        for name in ("apply", "unapply"):
            self.assertTrue(hasattr(issue_2537, name), name)


if __name__ == "__main__":
    unittest.main()
