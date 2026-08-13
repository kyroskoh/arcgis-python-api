#!/usr/bin/env python3
"""
Temporary workaround for Esri/arcgis-python-api#2527.

When a database datastore is managed as a Portal Data Store item, Server Admin
``Datastore.update()`` (KB 000022551) fails with "The data store item can only
be managed via Portal". Updating the Portal item alone does not push credentials
to federated servers.

There is no public Portal REST ``updatePassword`` operation. The supported sync
step after changing item registration data is
``PortalDataStore.refresh_server`` (POST .../datastores/refreshServer).

This module:

1. Provides ``update_password(...)`` — preferred explicit API.
2. Optionally monkey-patches ``Datastore.update`` so KB-style scripts fall back
   to the Portal path when Server rejects the edit as Portal-managed.

Usage (from this repo layout)::

    from misc.tools.issue_2527 import apply, update_password
    apply(gis)  # optional KB compatibility for ds.update(...)

    update_password(
        gis,
        ds_item,
        sde_path=r"C:\\path\\updated.sde",
        server=hosting_server,
    )

Or copy this file next to your script::

    import fix_portal_datastore_password_2527 as fix
    fix.apply(gis)
    fix.update_password(gis, item, connection_string=conn)

Call ``apply(gis)`` once per process before KB-style ``ds.update``. Remove when
Esri ships a dedicated Portal update-password API.

Limits:

- Approximates the Portal UI Update Password flow (import encrypted connection,
  refresh servers, optional validate). UI validation may differ from the public
  REST validate operation.
- Requires item-owner or admin privileges and either an encrypted connection
  string or a reachable ``.sde`` plus a Server for ``generate_connection_string``.
"""

from __future__ import annotations

import copy
import json
import re
from functools import wraps
from typing import Any, Optional

_applied = False
_original_update = None
_applied_gis = None

_PORTAL_MANAGED_RE = re.compile(
    r"managed\s+via\s+portal",
    re.IGNORECASE,
)


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if isinstance(item, str):
        return json.loads(item)
    raise TypeError(
        "Datastore update payload must be a dict or JSON string; "
        f"got {type(item).__name__}"
    )


def _extract_connection_string(payload: dict[str, Any]) -> str:
    info = payload.get("info") or {}
    conn = info.get("connectionString") or payload.get("connectionString")
    if not conn or not isinstance(conn, str):
        raise ValueError(
            "Payload is missing info.connectionString "
            "(encrypted connection string from generate_connection_string)."
        )
    return conn


def _extract_path(payload: dict[str, Any], fallback: Optional[str] = None) -> str:
    path = payload.get("path") or fallback
    if not path:
        raise ValueError("Payload is missing path (e.g. /enterpriseDatabases/name).")
    return path


def _is_portal_managed_error(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return _PORTAL_MANAGED_RE.search(str(resp) or "") is not None
    chunks: list[str] = []
    for key in ("message", "description", "messages", "error", "status"):
        val = resp.get(key)
        if isinstance(val, str):
            chunks.append(val)
        elif isinstance(val, list):
            chunks.extend(str(v) for v in val)
        elif isinstance(val, dict):
            chunks.append(json.dumps(val))
    return _PORTAL_MANAGED_RE.search(" ".join(chunks)) is not None


def _error_text(resp: Any) -> str:
    if isinstance(resp, dict):
        for key in ("message", "description", "messages", "error"):
            if key in resp and resp[key]:
                return str(resp[key])
        return json.dumps(resp)
    return str(resp)


def _resolve_item(gis, item):
    from arcgis.gis import Item

    if isinstance(item, Item):
        return item
    if isinstance(item, str):
        resolved = gis.content.get(item)
        if resolved is None:
            raise ValueError(f"No Portal item found for id={item!r}")
        return resolved
    # Duck-typed Item-like objects (tests / wrappers with id + get_data + update).
    if all(hasattr(item, name) for name in ("id", "get_data", "update")):
        return item
    raise TypeError("item must be a Portal Item or item id string")


def _resolve_connection_string(
    *,
    connection_string: Optional[str],
    sde_path: Optional[str],
    server,
) -> str:
    if connection_string:
        return connection_string
    if not sde_path:
        raise ValueError("Provide connection_string= or sde_path=")
    if server is None:
        raise ValueError(
            "server= is required when sde_path is provided "
            "(used for datastores.generate_connection_string)."
        )
    dsm = getattr(server, "datastores", None)
    if dsm is None:
        raise ValueError("server must expose a .datastores DataStoreManager")
    return dsm.generate_connection_string(sde_path)


def find_portal_datastore_item(gis, path: str, max_items: int = 500):
    """
    Find a Portal Data Store item whose ``get_data()['path']`` matches ``path``.
    """
    path = path.rstrip("/")
    # Prefer org-wide search when signed in as admin; fall back to default search.
    try:
        results = gis.content.search(
            query="*",
            item_type="Data Store",
            max_items=max_items,
        )
    except Exception:
        results = gis.content.search(
            query="type:\"Data Store\"",
            max_items=max_items,
        )

    matches = []
    for it in results:
        try:
            data = it.get_data()
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        item_path = (data.get("path") or "").rstrip("/")
        if item_path == path:
            matches.append(it)

    if not matches:
        raise RuntimeError(
            f"No Portal Data Store item found with path={path!r}. "
            "Pass the Portal item to update_password(...) explicitly, or "
            "ensure the signed-in user can search the item."
        )
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        raise RuntimeError(
            f"Multiple Portal Data Store items share path={path!r}: {ids}. "
            "Pass the Portal item to update_password(...) explicitly."
        )
    return matches[0]


def update_password(
    gis,
    item,
    *,
    connection_string: Optional[str] = None,
    sde_path: Optional[str] = None,
    server=None,
    refresh: bool = True,
    validate_after: bool = True,
) -> dict[str, Any]:
    """
    Update a Portal-managed database datastore password and sync to servers.

    Steps:
      1. Set ``info.connectionString`` on the Portal item data.
      2. ``gis.datastore.refresh_server`` for each federated server.
      3. Optionally ``gis.datastore.validate`` per server.

    :return: dict with keys ``ok``, ``item_id``, ``refreshed``, ``validated``,
             ``errors``.
    """
    portal_item = _resolve_item(gis, item)
    conn = _resolve_connection_string(
        connection_string=connection_string,
        sde_path=sde_path,
        server=server,
    )

    data = portal_item.get_data()
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Portal item {portal_item.id} has no JSON datastore payload "
            f"(get_data returned {type(data).__name__})."
        )

    updated = copy.deepcopy(data)
    info = updated.setdefault("info", {})
    if not isinstance(info, dict):
        raise RuntimeError(
            f"Portal item {portal_item.id} data.info is not an object."
        )
    info["connectionString"] = conn

    update_ok = portal_item.update(data=updated)
    if update_ok is False:
        raise RuntimeError(
            f"Portal item.update failed for datastore item {portal_item.id}."
        )

    result: dict[str, Any] = {
        "ok": True,
        "item_id": portal_item.id,
        "refreshed": [],
        "validated": [],
        "errors": [],
    }

    if not refresh and not validate_after:
        return result

    portal_ds = gis.datastore
    servers = portal_ds.servers(portal_item)
    if not isinstance(servers, list):
        raise RuntimeError(
            f"gis.datastore.servers failed for item {portal_item.id}: {servers!r}"
        )
    if not servers:
        result["errors"].append(
            "No federated servers are registered to this data store item; "
            "Portal item data was updated but nothing was refreshed."
        )
        result["ok"] = False
        return result

    for srv in servers:
        server_id = srv.get("id") if isinstance(srv, dict) else None
        if not server_id:
            result["errors"].append(f"Server entry missing id: {srv!r}")
            result["ok"] = False
            continue

        if refresh:
            refreshed = portal_ds.refresh_server(portal_item, server_id)
            if refreshed is True:
                result["refreshed"].append(server_id)
            else:
                result["ok"] = False
                result["errors"].append(
                    f"refresh_server failed for serverId={server_id}: {refreshed!r}"
                )

        if validate_after:
            validated = portal_ds.validate(server_id, item=portal_item)
            if validated is True:
                result["validated"].append(server_id)
            else:
                result["ok"] = False
                result["errors"].append(
                    f"validate failed for serverId={server_id}: {validated!r}"
                )

    return result


def _resolve_gis_for_datastore(datastore_obj):
    """Best-effort GIS recovery for KB monkey-patch; prefer apply(gis=...)."""
    global _applied_gis
    if _applied_gis is not None:
        return _applied_gis

    from arcgis.gis import GIS

    dsm = getattr(datastore_obj, "_datastore", None)
    con = getattr(datastore_obj, "_con", None) or getattr(dsm, "_con", None)

    # DataStoreManager constructed with a GIS keeps it on _gis in some paths.
    candidate = getattr(dsm, "_gis", None)
    if isinstance(candidate, GIS):
        return candidate

    portal_con = getattr(con, "_portal_connection", None)
    if isinstance(portal_con, GIS):
        return portal_con

    raise RuntimeError(
        "Cannot resolve a Portal GIS for the Datastore.update fallback. "
        "Call apply(gis) with your GIS, or use update_password(gis, item, ...) "
        "directly."
    )


def apply(gis=None) -> bool:
    """
    Monkey-patch Server ``Datastore.update`` for Portal-managed datastores.

    When Server rejects the edit as Portal-managed, falls back to updating the
    matching Portal Data Store item and calling ``refresh_server``.

    Pass ``gis`` so the fallback can search/update Portal items. Returns True if
    the patch was applied now, False if it was already applied.
    """
    global _applied, _original_update, _applied_gis
    if gis is not None:
        _applied_gis = gis
    if _applied:
        return False

    from arcgis.gis.server.admin._data import Datastore

    _original_update = Datastore.update

    @wraps(_original_update)
    def update_fixed(self, item: Any) -> bool:
        payload = _as_dict(item)
        params = {"f": "json", "item": item}
        # Match upstream: .../items{path}/edit
        path = self._datastore._url + "/items" + self.path + "/edit"
        resp = self._con.post(path, params, verify_cert=False)

        if isinstance(resp, dict) and resp.get("status") == "success":
            self.regenerate()
            return True

        if not _is_portal_managed_error(resp):
            raise RuntimeError(
                "Datastore.update failed and was not a Portal-managed rejection: "
                f"{_error_text(resp)}"
            )

        gis_obj = _resolve_gis_for_datastore(self)
        ds_path = _extract_path(payload, fallback=getattr(self, "path", None))
        conn = _extract_connection_string(payload)
        portal_item = find_portal_datastore_item(gis_obj, ds_path)
        result = update_password(
            gis_obj,
            portal_item,
            connection_string=conn,
            refresh=True,
            validate_after=True,
        )
        if not result["ok"]:
            detail = "; ".join(result["errors"]) or "unknown error"
            raise RuntimeError(
                "Portal-managed datastore password update failed after Server "
                f"rejected /edit: {detail}"
            )
        try:
            self.regenerate()
        except Exception:
            pass
        return True

    Datastore.update = update_fixed
    _applied = True
    return True


def unapply() -> bool:
    """Restore the original ``Datastore.update`` if this module patched it."""
    global _applied, _original_update, _applied_gis
    if not _applied or _original_update is None:
        return False
    from arcgis.gis.server.admin._data import Datastore

    Datastore.update = _original_update
    _applied = False
    _original_update = None
    _applied_gis = None
    return True


if __name__ == "__main__":
    import arcgis

    print(
        f"arcgis {arcgis.__version__}: issue #2527 workaround module loaded. "
        "Import apply/update_password from misc.tools.issue_2527 "
        "(or this file) — do not run apply() without a GIS in __main__."
    )
