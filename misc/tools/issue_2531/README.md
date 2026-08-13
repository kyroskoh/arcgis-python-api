# Workaround for Esri/arcgis-python-api#2531

Temporary hotfix for **[Survey.generate_report() — add job timeout and cancel support](https://github.com/Esri/arcgis-python-api/issues/2531)**.

## Problem

`Survey.generate_report` submits a Survey123 Feature Report job and polls forever in `_check_status` while status is `esriJobSubmitted` / `esriJobExecuting` (1s sleep, no deadline). In batch/ETL PDF generation, a stuck job hangs the process indefinitely.

The stock method also does not expose `jobId`, so callers cannot cancel a hung job via `POST .../api/featureReport/jobs/{jobId}/cancel`. External process timeouts kill Python but leave orphan remote jobs (and wasted credits).

Confirmed behavior on ArcGIS API for Python **2.4.3**.

## Branch

`fix/issue-2531-survey-generate-report-timeout` on [kyroskoh/arcgis-python-api](https://github.com/kyroskoh/arcgis-python-api/tree/fix/issue-2531-survey-generate-report-timeout/misc/tools/issue_2531)

## Install / import

From a clone of this fork (repo root on `PYTHONPATH`):

```python
from misc.tools.issue_2531 import apply, unapply, cancel_report_job
```

Or copy `fix_survey_generate_report_timeout_2531.py` next to your script and import it directly.

Requires ArcGIS API for Python (confirmed missing timeout/cancel on 2.4.3).

## Preferred API: `apply()` / `unapply()`

Monkey-patches `Survey.generate_report` and `Survey._check_status`:

```python
from arcgis.gis import GIS
from arcgis.apps.survey123 import SurveyManager
from misc.tools.issue_2531 import apply, cancel_report_job

apply()  # once per process

gis = GIS(profile="your_profile")
survey = SurveyManager(gis).get("<survey_item_id>")
template = gis.content.get("<report_template_item_id>")

result = survey.generate_report(
    report_template=template,
    where="OBJECTID=1",
    utc_offset="+00:00",
    output_format="pdf",
    job_timeout=90,          # NEW — seconds; None = stock infinite poll
    cancel_on_timeout=True,  # NEW — POST cancel when the client gives up
)

# After submit (even while polling), the last job id is available:
print(survey.last_report_job_id)

# Manual cancel (e.g. if cancel_on_timeout=False):
# cancel_report_job(survey)
# cancel_report_job(survey, job_id="<jobId>")
```

### What it does

1. Accepts optional `job_timeout` / `cancel_on_timeout` on `generate_report` (keyword-only; stock callers unchanged).
2. Sets `survey.last_report_job_id` from the submit response so callers can track/cancel.
3. When `job_timeout` is set, polls until the job leaves Submitted/Executing or the deadline passes.
4. On timeout with `cancel_on_timeout=True`, calls `POST .../api/featureReport/jobs/{jobId}/cancel`, then raises `TimeoutError` (message includes `jobId` and timeout seconds).
5. On success within the timeout, delegates to the stock `_check_status` result handling (download / Item resolution).

## Helpers

| Symbol | Purpose |
|--------|---------|
| `apply()` | Patch `Survey.generate_report` and `Survey._check_status` |
| `unapply()` | Restore the original methods |
| `cancel_report_job(survey, job_id=None)` | Cancel via REST using `job_id` or `survey.last_report_job_id` |

## Tests

Mock unit tests (no live AGOL/Enterprise calls). From the repo root:

```bash
python -m unittest misc.tools.issue_2531.test_fix_survey_generate_report_timeout_2531 -v
```

## When Esri releases a fix

Once a fixed ArcGIS API for Python build ships and you have upgraded:

1. **Restore the stock methods** in any long-running process that called `apply()`:

   ```python
   from misc.tools.issue_2531 import unapply

   unapply()  # returns True if the monkey-patch was removed
   ```

2. **Stop importing / calling this workaround** — remove `apply()` and any `from misc.tools.issue_2531 import ...` lines from your scripts. Drop `job_timeout` / `cancel_on_timeout` kwargs unless the official API added the same names.

3. **Upgrade `arcgis`** to the release that includes the `#2531` fix (check the release notes).

4. **Smoke-test** a Feature Report with a short `where` clause and confirm timeout/cancel behave as documented upstream (or are no longer needed).

5. **Optional cleanup** — delete the local `misc/tools/issue_2531/` copy (or stop checking out this fork branch) so the hotfix cannot be reapplied by mistake.

Until then, keep the workaround only in environments that still hit #2531.

## Limits

- Not a permanent Esri API fix — follow **When Esri releases a fix** above when an official path ships.
- Unit tests mock the Feature Report REST client; still smoke-test against a non-production survey/template before production ETL use.
- Cancel on timeout is best-effort: if the cancel POST fails, `TimeoutError` is still raised.
- `create_sample_report` is not given new kwargs; it only benefits from `last_report_job_id` if you call `apply()` (no timeout unless you set the private stash attrs yourself).

## References

- Issue: https://github.com/Esri/arcgis-python-api/issues/2531
- Survey123 Create Report: https://developers.arcgis.com/survey123/api-reference/rest/report/#create-report
- Feature Report job cancel (used by this workaround): `POST /api/featureReport/jobs/{jobId}/cancel`
