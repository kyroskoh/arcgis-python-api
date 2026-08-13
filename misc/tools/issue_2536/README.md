# Workaround for Esri/arcgis-python-api#2536

Temporary hotfix for **[Item.copy_feature_layer_collection() fails when relationships are set](https://github.com/Esri/arcgis-python-api/issues/2536)**.

## Problem

`Item.copy_feature_layer_collection` in `arcgis` **2.4.3** copies layer/table admin properties into the first `add_to_definition` call **including non-empty `relationships`**. AGOL/Enterprise then returns:

```text
Unable to add feature service definition.
Invalid definition for ...LayerCoreInfo... (Error Code: 400)
```

Services **without** relationships copy fine. Internal `_clone.py` already clears relationships on the first add and re-adds them afterward; `copy_feature_layer_collection` does not.

## Branch

`fix/issue-2536-copy-flc-relationships` on [kyroskoh/arcgis-python-api](https://github.com/kyroskoh/arcgis-python-api/tree/fix/issue-2536-copy-flc-relationships/misc/tools/issue_2536)

## Install / import

From a clone of this fork (repo root on `PYTHONPATH`):

```python
from misc.tools.issue_2536 import apply, restore
```

Or copy `fix_copy_flc_relationships.py` next to your script and import it directly.

Requires ArcGIS API for Python (confirmed broken on 2.4.3).

## Preferred API: `apply()` / `restore()`

Monkey-patches `Item.copy_feature_layer_collection`:

```python
from arcgis.gis import GIS
from misc.tools.issue_2536 import apply

apply()  # once per process

gis = GIS(profile="your_profile")  # or GIS("home")
item = gis.content.get("<feature_service_item_id_with_relationships>")

copied_item = item.copy_feature_layer_collection(
    service_name="TESTING_DEEP_COPY",
    layers=list(range(len(item.layers))),
    tables=list(range(len(item.tables))),
    description=item.description,
    snippet=item.snippet,
)
```

### What it does

1. Builds layer/table definitions like the stock method (`indexes` / `adminLayerInfo` removed).
2. Parks non-empty `relationships` and sets them to `[]` on the first `add_to_definition` (clone pattern).
3. Updates `serviceItemId` to the new item id when present.
4. After layers/tables exist, re-adds relationships in a second `add_to_definition` (drops ends whose `relatedTableId` was not included in the copy).

## Helpers

| Symbol | Purpose |
|--------|---------|
| `apply()` | Patch `Item.copy_feature_layer_collection` |
| `restore()` | Restore the original `copy_feature_layer_collection` |

## Smoke test

### Mock (no AGOL credentials)

Confirms the monkeypatch strips relationships on the first `add_to_definition`, re-adds them on the second pass, and returns a copied item:

```bash
python -m misc.tools.issue_2536.smoke_test
# or
python misc/tools/issue_2536/smoke_test.py
```

Expect: `PASS: mock smoke — relationships stripped then re-added; copy succeeded`

### Live AGOL / Portal

Uses a Feature Service that has layer/table relationships (the #2536 repro case):

```bash
python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID
python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID --profile your_profile
python misc/tools/issue_2536/smoke_test.py --item-id YOUR_FS_ITEM_ID --keep
```

The live run applies the patch, copies the service shell, checks that relationships exist on the copy, then deletes the copied item unless `--keep` is set.

## When Esri releases a fix

Once a fixed ArcGIS API for Python build ships and you have upgraded:

1. **Upgrade `arcgis`** to the release that includes the `#2536` fix (check the release notes / changelog).

2. **Restore the stock method** in any long-running process/notebook that called `apply()`:

   ```python
   from misc.tools.issue_2536 import restore

   restore()
   ```

   Or, if you imported the copied module:

   ```python
   import fix_copy_flc_relationships as fix

   fix.restore()
   ```

3. **Stop importing / calling this workaround** — remove every `apply()` and `from misc.tools.issue_2536 import ...` line from your scripts/notebooks.

4. **Optional cleanup** — delete the local `misc/tools/issue_2536/` copy (or stop checking out this fork branch) so the hotfix cannot be reapplied by mistake.

5. **Smoke-test** a Feature Service copy that has layer/table relationships **without** the patch and confirm the stock API succeeds.

Until then, keep the workaround only in environments that still hit #2536.

## Limits

- Not a permanent Esri API fix — follow **When Esri releases a fix** above when an official path ships.
- Copies the service **shell** (schema), same as stock `copy_feature_layer_collection` — it does not deep-copy feature data.
- Relationships that point at layers/tables you excluded from `layers` / `tables` are dropped on the second pass.
- Mock smoke covers the relationship strip/re-add path locally; live AGOL/Portal validation still needs a Feature Service with relationships (see **Smoke test**).

## References

- Issue: https://github.com/Esri/arcgis-python-api/issues/2536
- Workaround comment: https://github.com/Esri/arcgis-python-api/issues/2536#issuecomment-5276653680
- `copy_feature_layer_collection` docs: https://developers.arcgis.com/python/latest/api-reference/arcgis.gis.toc.html#arcgis.gis.Item.copy_feature_layer_collection
