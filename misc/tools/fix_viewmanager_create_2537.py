#!/usr/bin/env python3
"""
Temporary workaround for Esri/arcgis-python-api#2537.

Confirmed still present in arcgis 2.4.2 and 2.4.3:
  FeatureLayerCollectionManager.create_view incorrectly reassigns
  ``view_item = item`` (the parent) before ``update_item_data``, which
  corrupts multi-layer parent hosted feature services. Separately,
  AGOL ``add_to_definition(..., future=True)`` is not awaited, so
  ``set_visible_fields_and_query`` can hit ``item.layers[0]`` while
  layers are still empty (IndexError).

Usage (from this repo layout)::

    from misc.tools.fix_viewmanager_create_2537 import apply
    apply()
    # then use item.view_manager.create(...) as usual

Or copy this file next to your script::

    import fix_viewmanager_create_2537 as fix
    fix.apply()

Call ``apply()`` once per process before creating views. Remove when Esri
ships a fixed arcgis release.
"""

from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any

_applied = False
_original_create_view = None


def _field_visibility(fields: list[dict[str, Any]], visible_fields: list[str] | None):
    """Build update_definition field visibility; treat '*' as all visible."""
    if not visible_fields or any(f == "*" for f in visible_fields):
        return [{"name": fld["name"], "visible": True} for fld in fields]
    names = {f.lower() for f in visible_fields if f != "*"}
    return [
        {"name": fld["name"], "visible": fld["name"].lower() in names}
        for fld in fields
    ]


def _update_view_item_data(view_item, parent_item, view_layers) -> None:
    """Copy parent item JSON layer subset onto the *view* item (not the parent)."""
    if view_layers:
        data = parent_item.get_data()
        if isinstance(data, dict) and "layers" in data:
            item_upd_dict = {
                "layers": [
                    ilyr
                    for ilyr in data["layers"]
                    for lyr in view_layers
                    if int(lyr.url.split("/")[-1]) == ilyr["id"]
                ]
            }
            view_item.update(data=item_upd_dict)
    else:
        view_item.update(data=parent_item.get_data())


def _wait_for_layers(item, timeout_s: float = 60.0, interval_s: float = 1.0):
    """Refresh until the view item exposes layers, or raise."""
    from arcgis.features import FeatureLayerCollection

    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        try:
            item = item._gis.content.get(item.id)
            flc = FeatureLayerCollection.fromitem(item)
            if flc.layers:
                return item, flc
        except Exception as exc:  # noqa: BLE001 - retry until timeout
            last_error = exc
        time.sleep(interval_s)
    detail = f" Last error: {last_error}" if last_error else ""
    raise RuntimeError(
        f"View item {getattr(item, 'id', item)} has no layers after "
        f"{timeout_s:.0f}s; add_to_definition may still be pending.{detail}"
    )


def _update_layer_definition(layer_manager, values: dict[str, Any], is_agol: bool) -> None:
    if is_agol:
        layer_manager.update_definition(values, future=True).result()
    else:
        layer_manager.update_definition(values)


def _set_visible_fields_and_query(item, visible_fields, query, gis) -> None:
    if not (visible_fields or query):
        return

    item, flc = _wait_for_layers(item)
    if not flc.layers:
        raise RuntimeError(
            f"View item {item.id} has no layers; cannot apply query/visible_fields."
        )

    for lyr in flc.layers:
        fields = list(lyr.properties.get("fields") or [])
        values: dict[str, Any] = {}
        if visible_fields is not None or query:
            # Always set field visibility when either knob is used, matching
            # upstream intent; '*' means all visible.
            values["fields"] = _field_visibility(
                fields, visible_fields if visible_fields is not None else None
            )
        if query:
            values["viewDefinitionQuery"] = query
        if values:
            _update_layer_definition(lyr.manager, values, gis._is_arcgisonline)


def _awaiting_add_to_definition(original):
    """Wrap add_to_definition so AGOL async jobs are waited on."""

    @wraps(original)
    def wrapper(self, json_dict, future: bool = False):
        result = original(self, json_dict, future=future)
        if hasattr(result, "result"):
            result.result()
            try:
                self._hydrated = False
                self.refresh()
            except Exception:
                self._hydrated = False
        return result

    return wrapper


def apply() -> bool:
    """
    Monkey-patch FeatureLayerCollectionManager.create_view (used by ViewManager.create).

    Returns True if the patch was applied now, False if it was already applied.
    """
    global _applied, _original_create_view
    if _applied:
        return False

    from arcgis.features.managers import FeatureLayerCollectionManager

    _original_create_view = FeatureLayerCollectionManager.create_view
    original = _original_create_view
    sig = inspect.signature(original)

    @wraps(original)
    def create_view_fixed(self, *args, **kwargs):
        ba = sig.bind(self, *args, **kwargs)
        ba.apply_defaults()

        visible_fields = ba.arguments.get("visible_fields")
        query = ba.arguments.get("query")
        view_layers = ba.arguments.get("view_layers")

        # Avoid upstream IndexError path; we apply these after a safe create.
        ba.arguments["visible_fields"] = None
        ba.arguments["query"] = None

        gis = self._gis
        parent_item = gis.content.get(self.properties["serviceItemId"])
        parent_data = parent_item.get_data()

        mgr_cls = type(self)
        original_add = mgr_cls.add_to_definition
        mgr_cls.add_to_definition = _awaiting_add_to_definition(original_add)

        try:
            view_item = original(*ba.args, **ba.kwargs)
        finally:
            mgr_cls.add_to_definition = original_add
            # Upstream wrote filtered layer JSON onto the parent; restore it.
            try:
                parent_item.update(data=parent_data)
            except Exception:
                # Re-fetch and retry once — portal may briefly lock the item.
                parent_item = gis.content.get(parent_item.id)
                parent_item.update(data=parent_data)

        view_item = gis.content.get(view_item.id)
        parent_item = gis.content.get(parent_item.id)
        _update_view_item_data(view_item, parent_item, view_layers)
        _set_visible_fields_and_query(view_item, visible_fields, query, gis)
        return gis.content.get(view_item.id)

    FeatureLayerCollectionManager.create_view = create_view_fixed
    _applied = True
    return True


def unapply() -> bool:
    """Restore the original create_view if this module patched it."""
    global _applied, _original_create_view
    if not _applied or _original_create_view is None:
        return False
    from arcgis.features.managers import FeatureLayerCollectionManager

    FeatureLayerCollectionManager.create_view = _original_create_view
    _applied = False
    _original_create_view = None
    return True


if __name__ == "__main__":
    applied = apply()
    import arcgis

    print(
        f"arcgis {arcgis.__version__}: "
        f"{'patched' if applied else 'already patched'} "
        "FeatureLayerCollectionManager.create_view "
        "(workaround for issue #2537)"
    )
