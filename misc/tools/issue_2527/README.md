# Workaround for Esri/arcgis-python-api#2527

Temporary hotfix for **[Data Store Database Password Update via Portal](https://github.com/Esri/arcgis-python-api/issues/2527)**.

## Problem

[KB 000022551](https://support.esri.com/en-us/knowledge-base/how-to-programmatically-update-a-password-change-in-an--000022551) updates registered database passwords via Server Admin `Datastore.update()` (`POST .../admin/data/items/.../edit`).

Once a **Portal Data Store item** exists (Pro registration or Manager **Create Item**):

1. Server rejects `/edit` with *"The data store item can only be managed via Portal"* → `ds.update()` returns `False`.
2. `item.update(data=...)` updates Portal JSON only; federated Server datastores stay stale and validation fails.

There is no public Portal REST `updatePassword` operation. After changing item registration data, sync with [`PortalDataStore.refresh_server`](https://developers.arcgis.com/python/latest/api-reference/arcgis.gis.toc.html#arcgis.gis._impl._datastores.PortalDataStore.refresh_server) (`POST .../datastores/refreshServer`). See also the [community idea](https://community.esri.com/t5/arcgis-enterprise-ideas/expose-database-datastore-item-update-password-to/idi-p/1701215).

## Branch

`fix/issue-2527-portal-datastore-password` on [kyroskoh/arcgis-python-api](https://github.com/kyroskoh/arcgis-python-api/tree/fix/issue-2527-portal-datastore-password/misc/tools/issue_2527)

## Install / import

From a clone of this fork (repo root on `PYTHONPATH`):

```python
from misc.tools.issue_2527 import apply, update_password, unapply
```

Or copy `fix_portal_datastore_password_2527.py` next to your script and import it directly.

Requires ArcGIS API for Python (tested against 2.4.3) and item-owner or admin privileges.

## Preferred API: `update_password`

```python
from arcgis.gis import GIS
from misc.tools.issue_2527 import update_password

gis = GIS(profile="your_ent_admin_profile")
ds_item = gis.content.get("<datastore_item_id>")
hosting_server = gis.admin.servers.get(role="HOSTING_SERVER")[0]

result = update_password(
    gis,
    ds_item,
    sde_path=r"C:\path\updated.sde",  # password already updated in the .sde
    server=hosting_server,            # encrypts via generate_connection_string
    refresh=True,
    validate_after=True,
)
print(result)
# {"ok": True, "item_id": "...", "refreshed": [...], "validated": [...], "errors": []}
```

Or pass a prebuilt encrypted string:

```python
conn = hosting_server.datastores.generate_connection_string(r"C:\path\updated.sde")
result = update_password(gis, ds_item, connection_string=conn)
```

### What it does

1. Sets `info.connectionString` on the Portal item data.
2. Calls `gis.datastore.refresh_server(item, server_id)` for each server from `gis.datastore.servers(item)`.
3. Optionally runs `gis.datastore.validate(server_id, item=item)` per server.

This approximates the Portal UI **Update Password** flow described in [Manage data store items](https://enterprise.arcgis.com/en/portal/latest/use/manage-data-store-items.htm).

## KB compatibility: `apply()` / `unapply()`

Monkey-patches Server `Datastore.update` so existing KB scripts keep working when the datastore is Portal-managed:

```python
from misc.tools.issue_2527 import apply

apply(gis)  # pass GIS so the fallback can find/update the Portal item

# KB-style payload
pro2 = {
    "path": "/enterpriseDatabases/yourDsName",
    "type": "egdb",
    "info": {
        "dataStoreConnectionType": "shared",
        "isManaged": "false",
        "connectionString": conn,
    },
}
ok = ds.update(pro2)  # Server /edit, or Portal fallback + refresh_server
```

- Pass **`gis`** to `apply(gis)` so the fallback can search Portal Data Store items by `path`.
- Non–Portal-managed failures raise `RuntimeError` instead of returning silent `False`.
- Call `unapply()` to restore the original `Datastore.update`.

## Helpers

| Symbol | Purpose |
|--------|---------|
| `update_password(...)` | Explicit Portal path (recommended) |
| `apply(gis)` / `unapply()` | Patch / restore `Datastore.update` |
| `find_portal_datastore_item(gis, path)` | Resolve Portal item by datastore `path` |

## When Esri releases a fix

Once a fixed ArcGIS API for Python build (or first-class Portal update-password API) ships and you have upgraded:

1. **Restore the stock `Datastore.update`** in any long-running process that called `apply()`:

   ```python
   from misc.tools.issue_2527 import unapply

   unapply()  # returns True if the monkey-patch was removed
   ```

2. **Stop importing / calling this workaround** — remove `apply(...)`, `update_password(...)`, and any `from misc.tools.issue_2527 import ...` lines from your scripts.

3. **Switch to Esri’s supported path** — use the fixed API / updated KB steps for Portal-managed datastore password changes (or the Portal UI **Update Password** flow).

4. **Optional cleanup** — delete the local `misc/tools/issue_2527/` copy (or stop checking out this fork branch) so the hotfix cannot be reapplied by mistake.

Until then, keep the workaround only in environments that still hit #2527.

## Limits

- Not a permanent Esri API fix — follow **When Esri releases a fix** above when an official path ships.
- Portal UI validation may differ from the public REST `validate` operation.
- Needs a reachable updated `.sde` (or encrypted `connectionString`) and rights to update the item / refresh servers.
- `find_portal_datastore_item` searches Data Store items the signed-in user can see; pass the item explicitly to `update_password` when search cannot resolve a unique match.

## References

- Issue: https://github.com/Esri/arcgis-python-api/issues/2527
- KB: https://support.esri.com/en-us/knowledge-base/how-to-programmatically-update-a-password-change-in-an--000022551
- `refreshServer` REST: https://developers.arcgis.com/rest/users-groups-and-items/refresh-server/
- Community idea: https://community.esri.com/t5/arcgis-enterprise-ideas/expose-database-datastore-item-update-password-to/idi-p/1701215
