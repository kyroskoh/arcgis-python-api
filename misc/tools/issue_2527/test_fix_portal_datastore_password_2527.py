#!/usr/bin/env python3
"""
Smoke / mock tests for the #2527 Portal datastore password workaround.

No live Portal or Server required. Run from the repo root::

    python -m unittest misc.tools.issue_2527.test_fix_portal_datastore_password_2527 -v

Or::

    python misc/tools/issue_2527/test_fix_portal_datastore_password_2527.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Allow running this file directly without installing the package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from misc.tools.issue_2527 import (  # noqa: E402
    apply,
    find_portal_datastore_item,
    unapply,
    update_password,
)
from misc.tools.issue_2527 import fix_portal_datastore_password_2527 as fix  # noqa: E402


DS_PATH = "/enterpriseDatabases/testDs"
CONN_OLD = "ENCRYPTED_PASSWORD=old;SERVER=db"
CONN_NEW = "ENCRYPTED_PASSWORD=new;SERVER=db"


def _portal_item_data(path: str = DS_PATH, conn: str = CONN_OLD) -> dict:
    return {
        "path": path,
        "type": "egdb",
        "info": {
            "dataStoreConnectionType": "shared",
            "isManaged": "false",
            "connectionString": conn,
        },
    }


def _make_portal_item(item_id: str = "item123", path: str = DS_PATH, conn: str = CONN_OLD):
    item = MagicMock()
    item.id = item_id
    item.get_data.return_value = _portal_item_data(path=path, conn=conn)
    item.update.return_value = True
    return item


def _make_gis(portal_item=None, servers=None):
    if portal_item is None:
        portal_item = _make_portal_item()
    if servers is None:
        servers = [{"id": "serverA", "name": "hosting"}]

    gis = MagicMock()
    gis.content.get.return_value = portal_item
    gis.content.search.return_value = [portal_item]

    portal_ds = MagicMock()
    portal_ds.servers.return_value = servers
    portal_ds.refresh_server.return_value = True
    portal_ds.validate.return_value = True
    gis.datastore = portal_ds
    return gis, portal_item, portal_ds


class TestHelpers(unittest.TestCase):
    def test_as_dict_accepts_dict_and_json(self):
        payload = {"path": DS_PATH, "info": {"connectionString": CONN_NEW}}
        self.assertEqual(fix._as_dict(payload)["path"], DS_PATH)
        self.assertEqual(fix._as_dict(json.dumps(payload))["path"], DS_PATH)

    def test_as_dict_rejects_bad_type(self):
        with self.assertRaises(TypeError):
            fix._as_dict(123)

    def test_extract_connection_string(self):
        self.assertEqual(
            fix._extract_connection_string(
                {"info": {"connectionString": CONN_NEW}}
            ),
            CONN_NEW,
        )
        with self.assertRaises(ValueError):
            fix._extract_connection_string({"info": {}})

    def test_extract_path(self):
        self.assertEqual(fix._extract_path({"path": DS_PATH}), DS_PATH)
        self.assertEqual(fix._extract_path({}, fallback=DS_PATH), DS_PATH)
        with self.assertRaises(ValueError):
            fix._extract_path({})

    def test_is_portal_managed_error(self):
        self.assertTrue(
            fix._is_portal_managed_error(
                {
                    "status": "error",
                    "message": "The data store item can only be managed via Portal",
                }
            )
        )
        self.assertTrue(
            fix._is_portal_managed_error(
                {"messages": ["managed via portal for this item"]}
            )
        )
        self.assertFalse(
            fix._is_portal_managed_error({"status": "error", "message": "timeout"})
        )


class TestFindPortalDatastoreItem(unittest.TestCase):
    def test_finds_unique_match(self):
        gis, portal_item, _ = _make_gis()
        found = find_portal_datastore_item(gis, DS_PATH)
        self.assertIs(found, portal_item)

    def test_raises_when_missing(self):
        gis, _, _ = _make_gis()
        gis.content.search.return_value = []
        with self.assertRaises(RuntimeError) as ctx:
            find_portal_datastore_item(gis, DS_PATH)
        self.assertIn("No Portal Data Store item", str(ctx.exception))

    def test_raises_when_ambiguous(self):
        a = _make_portal_item("a")
        b = _make_portal_item("b")
        gis, _, _ = _make_gis(portal_item=a)
        gis.content.search.return_value = [a, b]
        with self.assertRaises(RuntimeError) as ctx:
            find_portal_datastore_item(gis, DS_PATH)
        self.assertIn("Multiple", str(ctx.exception))


class TestUpdatePassword(unittest.TestCase):
    def test_updates_item_refreshes_and_validates(self):
        gis, portal_item, portal_ds = _make_gis()

        result = update_password(
            gis,
            "item123",
            connection_string=CONN_NEW,
            refresh=True,
            validate_after=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["item_id"], "item123")
        self.assertEqual(result["refreshed"], ["serverA"])
        self.assertEqual(result["validated"], ["serverA"])
        self.assertEqual(result["errors"], [])

        portal_item.update.assert_called_once()
        updated_data = portal_item.update.call_args.kwargs["data"]
        self.assertEqual(updated_data["info"]["connectionString"], CONN_NEW)
        self.assertEqual(updated_data["path"], DS_PATH)
        portal_ds.refresh_server.assert_called_once_with(portal_item, "serverA")
        portal_ds.validate.assert_called_once_with("serverA", item=portal_item)

    def test_sde_path_uses_generate_connection_string(self):
        gis, portal_item, _ = _make_gis()
        server = MagicMock()
        server.datastores.generate_connection_string.return_value = CONN_NEW

        result = update_password(
            gis,
            "item123",
            sde_path=r"C:\tmp\updated.sde",
            server=server,
            refresh=False,
            validate_after=False,
        )

        self.assertTrue(result["ok"])
        server.datastores.generate_connection_string.assert_called_once_with(
            r"C:\tmp\updated.sde"
        )
        updated_data = portal_item.update.call_args.kwargs["data"]
        self.assertEqual(updated_data["info"]["connectionString"], CONN_NEW)

    def test_refresh_failure_sets_ok_false(self):
        gis, _, portal_ds = _make_gis()
        portal_ds.refresh_server.return_value = False

        result = update_password(
            gis, "item123", connection_string=CONN_NEW, validate_after=False
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["refreshed"], [])
        self.assertTrue(any("refresh_server failed" in e for e in result["errors"]))

    def test_no_servers_sets_ok_false(self):
        gis, _, portal_ds = _make_gis(servers=[])

        result = update_password(gis, "item123", connection_string=CONN_NEW)

        self.assertFalse(result["ok"])
        self.assertTrue(any("No federated servers" in e for e in result["errors"]))
        portal_ds.refresh_server.assert_not_called()

    def test_item_update_false_raises(self):
        gis, portal_item, _ = _make_gis()
        portal_item.update.return_value = False
        with self.assertRaises(RuntimeError):
            update_password(
                gis,
                "item123",
                connection_string=CONN_NEW,
                refresh=False,
                validate_after=False,
            )


class TestApplyUnapply(unittest.TestCase):
    def tearDown(self):
        unapply()

    def test_apply_unapply_idempotent(self):
        from arcgis.gis.server.admin._data import Datastore

        original = Datastore.update
        gis = MagicMock()

        self.assertTrue(apply(gis))
        self.assertIsNot(Datastore.update, original)
        self.assertFalse(apply(gis))  # already applied
        self.assertTrue(unapply())
        self.assertIs(Datastore.update, original)
        self.assertFalse(unapply())

    def test_update_success_path_no_portal_fallback(self):
        from arcgis.gis.server.admin._data import Datastore

        gis, _, _ = _make_gis()
        apply(gis)

        ds = MagicMock()
        ds.path = DS_PATH
        ds._datastore._url = "https://server.example/arcgis/admin/data"
        ds._con.post.return_value = {"status": "success"}

        self.assertTrue(Datastore.update(ds, _portal_item_data(conn=CONN_NEW)))
        ds.regenerate.assert_called_once()
        ds._con.post.assert_called_once()

    def test_portal_managed_fallback_calls_update_password(self):
        from arcgis.gis.server.admin._data import Datastore

        gis, portal_item, portal_ds = _make_gis()
        apply(gis)

        ds = MagicMock()
        ds.path = DS_PATH
        ds._datastore._url = "https://server.example/arcgis/admin/data"
        ds._con.post.return_value = {
            "status": "error",
            "message": "The data store item can only be managed via Portal",
        }

        payload = _portal_item_data(conn=CONN_NEW)
        self.assertTrue(Datastore.update(ds, payload))

        portal_item.update.assert_called_once()
        portal_ds.refresh_server.assert_called_once_with(portal_item, "serverA")
        portal_ds.validate.assert_called_once_with("serverA", item=portal_item)
        ds.regenerate.assert_called()

    def test_non_portal_error_raises(self):
        from arcgis.gis.server.admin._data import Datastore

        gis, _, _ = _make_gis()
        apply(gis)

        ds = MagicMock()
        ds.path = DS_PATH
        ds._datastore._url = "https://server.example/arcgis/admin/data"
        ds._con.post.return_value = {"status": "error", "message": "token expired"}

        with self.assertRaises(RuntimeError) as ctx:
            Datastore.update(ds, _portal_item_data(conn=CONN_NEW))
        self.assertIn("not a Portal-managed rejection", str(ctx.exception))


class TestSmokeImport(unittest.TestCase):
    def test_package_exports(self):
        from misc.tools import issue_2527

        for name in ("apply", "unapply", "update_password", "find_portal_datastore_item"):
            self.assertTrue(hasattr(issue_2527, name), name)


if __name__ == "__main__":
    unittest.main()
