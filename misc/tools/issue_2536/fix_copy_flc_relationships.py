#!/usr/bin/env python3
"""
Temporary workaround for Esri/arcgis-python-api#2536.

``Item.copy_feature_layer_collection`` copies layer/table admin properties
verbatim into ``add_to_definition``. When any layer/table has non-empty
``relationships``, AGOL/Enterprise returns:

  Unable to add feature service definition.
  Invalid definition for ...LayerCoreInfo... (Error Code: 400)

Internal ``_clone.py`` already documents the required pattern: clear
``relationships`` on the first ``add_to_definition``, then re-add them in a
second call after layers/tables exist. This monkeypatch applies that pattern.

Usage (from this repo layout)::

    from misc.tools.issue_2536 import apply
    apply()
    # then use item.copy_feature_layer_collection(...) as usual

Or copy this file next to your script::

    import fix_copy_flc_relationships as fix
    fix.apply()

Call ``apply()`` once per process before copying. Remove when Esri ships a
fixed arcgis release.
"""

from __future__ import annotations

import copy
from typing import Any

_applied = False
_original = None


def _prepare_layer_def(
    props: dict[str, Any],
    *,
    new_item_id: str | None,
    relationships_out: dict[Any, list],
) -> dict[str, Any]:
    """Strip indexes/adminLayerInfo; park relationships for a second add."""
    v = dict(props)
    if "indexes" in v:
        del v["indexes"]
    if "adminLayerInfo" in v:
        del v["adminLayerInfo"]
    if new_item_id is not None and "serviceItemId" in v:
        v["serviceItemId"] = new_item_id

    rels = v.get("relationships")
    layer_id = v.get("id")
    if layer_id is not None and isinstance(rels, list) and len(rels) > 0:
        relationships_out[layer_id] = copy.deepcopy(rels)
        v["relationships"] = []
    return v


def _relationships_add_definition(
    relationships: dict[Any, list],
    copied_layer_ids: set[Any],
) -> dict[str, list]:
    """Build second-pass add_to_definition payload for relationships only.

    Drops relationship ends whose relatedTableId was not included in the copy.
    Keeps relatedTableId as-is when ids are preserved on create (typical).
    """
    layers_payload: list[dict[str, Any]] = []
    for layer_id, rels in relationships.items():
        if layer_id not in copied_layer_ids:
            continue
        kept = []
        for rel in rels:
            if not isinstance(rel, dict):
                continue
            related = rel.get("relatedTableId")
            if related not in copied_layer_ids:
                continue
            kept.append(rel)
        if kept:
            layers_payload.append({"id": layer_id, "relationships": kept})
    return {"layers": layers_payload}


def apply() -> None:
    """Monkeypatch Item.copy_feature_layer_collection to handle relationships."""
    global _applied, _original
    if _applied:
        return

    from arcgis.gis import Item, User

    _original = Item.copy_feature_layer_collection

    def _fixed(self, *args, **kwargs):
        from arcgis.features import FeatureLayerCollection as FLC

        service_name = kwargs.get("service_name", args[0] if args else None)
        layers = kwargs.get("layers", args[1] if len(args) > 1 else None)
        tables = kwargs.get("tables", args[2] if len(args) > 2 else None)
        folder = kwargs.get("folder", args[3] if len(args) > 3 else None)
        description = kwargs.get("description", args[4] if len(args) > 4 else None)
        snippet = kwargs.get("snippet", args[5] if len(args) > 5 else None)
        owner = kwargs.get("owner", args[6] if len(args) > 6 else None)

        if self.type != "Feature Service" and self.type != "Feature Layer Collection":
            return
        if layers is None and tables is None:
            raise ValueError("An index of layers or tables must be provided")
        content = self._gis.content
        if isinstance(owner, User):
            owner = owner.username
        idx_layers = []
        idx_tables = []
        params = {}
        allowed = [
            "description",
            "allowGeometryUpdates",
            "units",
            "syncEnabled",
            "serviceDescription",
            "capabilities",
            "serviceItemId",
            "supportsDisconnectedEditing",
            "maxRecordCount",
            "supportsApplyEditsWithGlobalIds",
            "name",
            "supportedQueryFormats",
            "xssPreventionInfo",
            "copyrightText",
            "currentVersion",
            "syncCapabilities",
            "_ssl",
            "hasStaticData",
            "hasVersionedData",
            "editorTrackingInfo",
            "name",
        ]
        if description is None:
            description = self.description
        if snippet is None:
            snippet = self.snippet
        i = 1
        is_free = content.is_service_name_available(
            service_name=service_name, service_type="Feature Service"
        )
        if is_free is False:
            while is_free is False:
                i += 1
                s = service_name + "_%s" % i
                is_free = content.is_service_name_available(
                    service_name=s, service_type="Feature Service"
                )
                if is_free:
                    service_name = s
                    break
        if len(self.tables) > 0 or len(self.layers) > 0:
            parent = FLC(url=self.url, gis=self._gis)
        else:
            raise Exception("No tables or layers found in service, cannot copy it.")
        if layers is not None:
            if isinstance(layers, (list, tuple)):
                for idx in layers:
                    idx_layers.append(self.layers[idx])
            elif isinstance(layers, str):
                for idx in layers.split(","):
                    idx_layers.append(self.layers[idx])
            else:
                raise ValueError(
                    "layers must be a comma seperated list of integers or a list"
                )
        if tables is not None:
            if isinstance(tables, (list, tuple)):
                for idx in tables:
                    idx_tables.append(self.tables[idx])
            elif isinstance(tables, str):
                for idx in tables.split(","):
                    idx_tables.append(self.tables[idx])
            else:
                raise ValueError(
                    "tables must be a comma seperated list of integers or a list"
                )
        for k, v in dict(parent.properties).items():
            if k in allowed:
                if k.lower() == "name":
                    params[k] = service_name
                if k.lower() == "_ssl":
                    params["_ssl"] = False
                params[k] = v
        if "name" not in params.keys():
            params["name"] = service_name
        params["_ssl"] = False
        copied_item = content.create_service(
            name=service_name,
            create_params=params,
            folder=folder,
            owner=owner,
            item_properties={
                "description": description,
                "snippet": snippet,
                "tags": self.tags,
                "title": service_name,
            },
        )

        fs = FLC(url=copied_item.url, gis=self._gis)
        fs_manager = fs.manager
        add_defs: dict[str, list] = {"layers": [], "tables": []}
        relationships: dict[Any, list] = {}
        new_item_id = getattr(copied_item, "id", None)

        for l in idx_layers:
            add_defs["layers"].append(
                _prepare_layer_def(
                    dict(l.manager.properties),
                    new_item_id=new_item_id,
                    relationships_out=relationships,
                )
            )
        for l in idx_tables:
            add_defs["tables"].append(
                _prepare_layer_def(
                    dict(l.manager.properties),
                    new_item_id=new_item_id,
                    relationships_out=relationships,
                )
            )

        try:
            res = fs_manager.add_to_definition(json_dict=add_defs)
        except Exception:
            try:
                copied_item.delete()
            except Exception:
                pass
            raise

        if not (isinstance(res, dict) and res.get("success") is True):
            try:
                copied_item.delete()
            except Exception:
                pass
            return None

        if relationships:
            copied_ids = {
                layer.get("id")
                for layer in (add_defs["layers"] + add_defs["tables"])
                if layer.get("id") is not None
            }
            rel_defs = _relationships_add_definition(relationships, copied_ids)
            if rel_defs.get("layers"):
                try:
                    rel_res = fs_manager.add_to_definition(json_dict=rel_defs)
                except Exception:
                    try:
                        copied_item.delete()
                    except Exception:
                        pass
                    raise
                if not (
                    isinstance(rel_res, dict) and rel_res.get("success") is True
                ):
                    try:
                        copied_item.delete()
                    except Exception:
                        pass
                    return None

        return copied_item

    Item.copy_feature_layer_collection = _fixed
    _applied = True


def restore() -> None:
    """Restore the original Item.copy_feature_layer_collection."""
    global _applied, _original
    if _original is not None:
        from arcgis.gis import Item

        Item.copy_feature_layer_collection = _original
    _applied = False
    _original = None
