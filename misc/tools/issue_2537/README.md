# Workaround for Esri/arcgis-python-api#2537

Temporary hotfix for **[IndexError and corrupted hosted feature service when using the ViewManager create method](https://github.com/Esri/arcgis-python-api/issues/2537)**.

## Problem

`ViewManager.create` → `FeatureLayerCollectionManager.create_view` in `arcgis` **2.4.2** and **2.4.3** has two defects:

1. **Parent corruption** — after creating the view item, the code reassigns `view_item = item` (the parent) and then `update_item_data` writes a filtered `data.layers` subset onto the **parent** hosted feature service. On multi-layer parents with `view_layers=[one layer]`, Portal/AGOL UI stops treating feature layers as layers (only tables remain).

2. **IndexError** — on ArcGIS Online, `add_to_definition(..., future=True)` is not awaited, so `set_visible_fields_and_query` can hit `item.layers[0]` while layers are still empty. Query / field visibility are also applied only to `layers[0]`, not every view layer.

## Branch

`fix/issue-2537-viewmanager-create` on [kyroskoh/arcgis-python-api](https://github.com/kyroskoh/arcgis-python-api/tree/fix/issue-2537-viewmanager-create/misc/tools/issue_2537)

## Install / import

From a clone of this fork (repo root on `PYTHONPATH`):

```python
from misc.tools.issue_2537 import apply, unapply
```

Or copy `fix_viewmanager_create_2537.py` next to your script and import it directly.

Requires ArcGIS API for Python (confirmed broken on 2.4.2 and 2.4.3).

## Tests (mock / smoke)

No live ArcGIS Online or Enterprise required:

```bash
python -m unittest misc.tools.issue_2537.test_fix_viewmanager_create_2537 -v
```

Or:

```bash
python misc/tools/issue_2537/test_fix_viewmanager_create_2537.py
```

## Preferred API: `apply()` / `unapply()`

Monkey-patches `FeatureLayerCollectionManager.create_view` (what `ViewManager.create` calls):

```python
from arcgis.gis import GIS
from misc.tools.issue_2537 import apply

apply()  # once per process

gis = GIS(profile="your_profile")
flyr_item = gis.content.get("<hosted_feature_layer_item_id>")

view_item = flyr_item.view_manager.create(
    name="My_Layer_View",
    view_layers=[flyr_item.layers[0]],
    allow_schema_changes=False,
    updateable=False,
    capabilities="Query",
    preserve_layer_ids=True,
    query="Status <> 'Abandoned'",
    visible_fields=["OBJECTID", "GlobalID", "*"],
)
```

### What it does

1. Calls the stock `create_view` without `query` / `visible_fields` (avoids the empty-`layers[0]` path).
2. Awaits AGOL `add_to_definition` futures so the view definition finishes before post-steps.
3. Restores the parent item JSON if it was overwritten.
4. Writes the correct layer subset onto the **view** item.
5. Applies `query` / `visible_fields` to **each** view layer (`"*"` means all fields visible).

## Helpers

| Symbol | Purpose |
|--------|---------|
| `apply()` | Patch `FeatureLayerCollectionManager.create_view` |
| `unapply()` | Restore the original `create_view` |

## When Esri releases a fix

Once a fixed ArcGIS API for Python build ships and you have upgraded:

1. **Restore the stock `create_view`** in any long-running process that called `apply()`:

   ```python
   from misc.tools.issue_2537 import unapply

   unapply()  # returns True if the monkey-patch was removed
   ```

2. **Stop importing / calling this workaround** — remove `apply()` and any `from misc.tools.issue_2537 import ...` lines from your scripts.

3. **Upgrade `arcgis`** to the release that includes the `#2537` fix (check the release notes).

4. **Smoke-test** a multi-layer `view_manager.create(..., query=..., visible_fields=...)` and confirm the parent service `data.layers` is unchanged.

5. **Optional cleanup** — delete the local `misc/tools/issue_2537/` copy (or stop checking out this fork branch) so the hotfix cannot be reapplied by mistake.

Until then, keep the workaround only in environments that still hit #2537.

## Limits

- Not a permanent Esri API fix — follow **When Esri releases a fix** above when an official path ships.
- Mock / smoke tests cover the monkey-patch without a live org; still validate against a non-production multi-layer hosted feature service before production use.
- Already-corrupted parent items are not repaired by this patch — restore them from backup or republish before creating more views.

## References

- Issue: https://github.com/Esri/arcgis-python-api/issues/2537
- RCA comment: https://github.com/Esri/arcgis-python-api/issues/2537#issuecomment-5275574852
- `create_view` docs: https://developers.arcgis.com/python/latest/api-reference/arcgis.features.managers.html#arcgis.features.managers.FeatureLayerCollectionManager.create_view
- Hosted feature layer views: https://doc.arcgis.com/en/arcgis-online/manage-data/create-hosted-views.htm
