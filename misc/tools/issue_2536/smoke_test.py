#!/usr/bin/env python3
"""
Smoke tests for Esri/arcgis-python-api#2536 workaround.

Mock mode (default, no AGOL credentials required)::

    python -m misc.tools.issue_2536.smoke_test
    python misc/tools/issue_2536/smoke_test.py

Live AGOL / Portal mode::

    python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID
    python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID --profile your_profile
    python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID --keep

The live run applies the monkeypatch, copies the Feature Service shell
(layers + tables), checks that relationships survived on the copy, then
deletes the copied item unless ``--keep`` is set.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _layer_has_relationships(layer_or_table: Any) -> bool:
    try:
        props = dict(layer_or_table.properties)
    except Exception:
        return False
    rels = props.get("relationships") or []
    return isinstance(rels, list) and len(rels) > 0


def run_mock_smoke() -> int:
    """Verify the patch strips relationships on first add, then re-adds them."""
    import misc.tools.issue_2536.fix_copy_flc_relationships as fix

    importlib.reload(fix)
    fix.apply()

    from arcgis.gis import Item

    class FakeProps(dict):
        pass

    class FakeLayer:
        def __init__(self, props: dict[str, Any]):
            self.manager = MagicMock()
            self.manager.properties = FakeProps(props)

    class FakeFLC:
        calls: list = []

        def __init__(self, url=None, gis=None):
            self.url = url
            self.gis = gis
            self.properties = FakeProps(
                {
                    "name": "src",
                    "capabilities": "Query",
                    "maxRecordCount": 1000,
                    "supportedQueryFormats": "JSON",
                }
            )
            self.manager = MagicMock()

            def add_to_definition(json_dict=None):
                FakeFLC.calls.append(json_dict)
                layers = (json_dict or {}).get("layers") or []
                tables = (json_dict or {}).get("tables") or []
                for layer in layers + tables:
                    # Simulate AGOL: first-pass layer create with relationships fails
                    if "fields" in layer and (layer.get("relationships") or []):
                        raise Exception(
                            "Unable to add feature service definition. "
                            "Invalid definition for System.Collections.Generic."
                            "List`1[ESRI.ArcGIS.SDS.Metadata.LayerCoreInfo] "
                            "(Error Code: 400)"
                        )
                return {"success": True}

            self.manager.add_to_definition = add_to_definition

    item = MagicMock(spec=Item)
    item.type = "Feature Service"
    item.id = "SRC_ITEM_ID"
    item.url = "https://example.com/FS"
    item.description = "desc"
    item.snippet = "snip"
    item.tags = ["t"]
    item.layers = [
        FakeLayer(
            {
                "id": 0,
                "name": "parents",
                "fields": [{"name": "OBJECTID"}, {"name": "GlobalID"}],
                "relationships": [
                    {
                        "id": 0,
                        "name": "parents_children",
                        "relatedTableId": 1,
                        "role": "esriRelRoleOrigin",
                        "keyField": "GlobalID",
                    }
                ],
                "serviceItemId": "SRC_ITEM_ID",
                "indexes": [{"fields": "OBJECTID"}],
                "adminLayerInfo": {"x": 1},
            }
        )
    ]
    item.tables = [
        FakeLayer(
            {
                "id": 1,
                "name": "children",
                "fields": [
                    {"name": "OBJECTID"},
                    {"name": "GlobalID"},
                    {"name": "parent_guid"},
                ],
                "relationships": [
                    {
                        "id": 0,
                        "name": "children_parents",
                        "relatedTableId": 0,
                        "role": "esriRelRoleDestination",
                        "keyField": "parent_guid",
                    }
                ],
                "serviceItemId": "SRC_ITEM_ID",
                "indexes": [],
                "adminLayerInfo": {},
            }
        )
    ]
    copied = MagicMock()
    copied.id = "NEW_ITEM_ID"
    copied.url = "https://example.com/New"
    copied.delete = MagicMock()
    content = MagicMock()
    content.is_service_name_available.return_value = True
    content.create_service.return_value = copied
    item._gis = MagicMock()
    item._gis.content = content

    FakeFLC.calls = []
    with patch("arcgis.features.FeatureLayerCollection", FakeFLC):
        result = Item.copy_feature_layer_collection(
            item,
            service_name="SMOKE_TEST_2536",
            layers=[0],
            tables=[0],
        )

    if result is None or getattr(result, "id", None) != "NEW_ITEM_ID":
        print("FAIL: mock copy did not return the new item")
        return 1
    if len(FakeFLC.calls) != 2:
        print(f"FAIL: expected 2 add_to_definition calls, got {len(FakeFLC.calls)}")
        return 1

    first = FakeFLC.calls[0]
    second = FakeFLC.calls[1]
    first_layers = (first.get("layers") or []) + (first.get("tables") or [])
    second_layers = second.get("layers") or []

    for layer in first_layers:
        if layer.get("relationships"):
            print("FAIL: first add_to_definition still had relationships:", layer)
            return 1
        if layer.get("serviceItemId") != "NEW_ITEM_ID":
            print("FAIL: serviceItemId not rewritten on first pass:", layer)
            return 1

    if len(second_layers) < 1:
        print("FAIL: second add_to_definition had no relationship updates")
        return 1
    for layer in second_layers:
        if not (layer.get("relationships") or []):
            print("FAIL: second pass missing relationships for layer", layer.get("id"))
            return 1

    fix.restore()
    print("PASS: mock smoke - relationships stripped then re-added; copy succeeded")
    return 0


def run_live_smoke(
    item_id: str,
    *,
    profile: str | None = None,
    service_name: str | None = None,
    keep: bool = False,
) -> int:
    """Copy a real Feature Service with relationships using the workaround."""
    from arcgis.gis import GIS

    from misc.tools.issue_2536 import apply, restore

    apply()
    try:
        gis = GIS(profile=profile) if profile else GIS("home")
        me = gis.users.me
        print(f"signed in as: {getattr(me, 'username', me)}")

        item = gis.content.get(item_id)
        if item is None:
            print(f"FAIL: item not found: {item_id}")
            return 2
        if item.type not in ("Feature Service", "Feature Layer Collection"):
            print(f"FAIL: item type is {item.type!r}, expected Feature Service")
            return 2

        n_layers = len(item.layers or [])
        n_tables = len(item.tables or [])
        src_rel_layers = sum(1 for lyr in (item.layers or []) if _layer_has_relationships(lyr))
        src_rel_tables = sum(1 for tbl in (item.tables or []) if _layer_has_relationships(tbl))
        print(
            f"source item={item.id} layers={n_layers} tables={n_tables} "
            f"layers_with_rels={src_rel_layers} tables_with_rels={src_rel_tables}"
        )
        if src_rel_layers + src_rel_tables == 0:
            print(
                "FAIL: source has no relationships on layers/tables — "
                "use a Feature Service that reproduces #2536"
            )
            return 2

        name = service_name or f"SMOKE_2536_{int(time.time())}"
        print(f"copying shell as {name!r} ...")
        copied = item.copy_feature_layer_collection(
            service_name=name,
            layers=list(range(n_layers)),
            tables=list(range(n_tables)),
            description=item.description,
            snippet=item.snippet,
        )
        if copied is None:
            print("FAIL: copy_feature_layer_collection returned None")
            return 1

        print(f"copied item id={copied.id} url={copied.url}")
        # Refresh layer/table props
        copied_rel_layers = sum(
            1 for lyr in (copied.layers or []) if _layer_has_relationships(lyr)
        )
        copied_rel_tables = sum(
            1 for tbl in (copied.tables or []) if _layer_has_relationships(tbl)
        )
        print(
            f"copied layers_with_rels={copied_rel_layers} "
            f"tables_with_rels={copied_rel_tables}"
        )
        if copied_rel_layers + copied_rel_tables == 0:
            print("FAIL: copied service has no relationships")
            if not keep:
                try:
                    copied.delete()
                except Exception as exc:
                    print(f"cleanup warning: could not delete {copied.id}: {exc}")
            return 1

        print(f"PASS: live smoke - copied {copied.id} with relationships intact")
        if keep:
            print(f"--keep set; left item {copied.id} in place")
        else:
            try:
                copied.delete()
                print(f"deleted smoke copy {copied.id}")
            except Exception as exc:
                print(f"cleanup warning: could not delete {copied.id}: {exc}")
        return 0
    finally:
        restore()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test for #2536 workaround")
    parser.add_argument(
        "--item-id",
        help="Feature Service item id with relationships (enables live AGOL/Portal mode)",
    )
    parser.add_argument("--profile", default=None, help="GIS profile name")
    parser.add_argument(
        "--service-name",
        default=None,
        help="Optional name for the live copied service",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the live copied item after a successful smoke test",
    )
    parser.add_argument(
        "--mock-only",
        action="store_true",
        help="Force mock mode even if --item-id is set",
    )
    args = parser.parse_args(argv)

    if args.item_id and not args.mock_only:
        return run_live_smoke(
            args.item_id,
            profile=args.profile,
            service_name=args.service_name,
            keep=args.keep,
        )
    return run_mock_smoke()


if __name__ == "__main__":
    raise SystemExit(main())
