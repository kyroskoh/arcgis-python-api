#!/usr/bin/env python3
"""
Temporary workaround for Esri/arcgis-python-api#2531.

``Survey.generate_report`` submits a Feature Report job and polls forever in
``_check_status`` while status is ``esriJobSubmitted`` / ``esriJobExecuting``.
There is no timeout, no cancel, and no ``jobId`` exposed to the caller.

This module monkey-patches ``Survey.generate_report`` and ``Survey._check_status``
to add optional ``job_timeout`` / ``cancel_on_timeout``, stash
``survey.last_report_job_id``, and provide ``cancel_report_job``.

Usage (from this repo layout)::

    from misc.tools.issue_2531 import apply
    apply()
    # then use survey.generate_report(..., job_timeout=90, cancel_on_timeout=True)

Or copy this file next to your script::

    import fix_survey_generate_report_timeout_2531 as fix
    fix.apply()

Call ``apply()`` once per process before generating reports. Remove when Esri
ships a fixed arcgis release.
"""

from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any, Optional

_applied = False
_original_generate_report = None
_original_check_status = None

_EXECUTING = frozenset({"esriJobExecuting", "esriJobSubmitted"})


def _status_params(survey) -> dict[str, Any]:
    return {
        "f": "json",
        "username": survey._si._gis.users.me.username,
        "portalUrl": survey._si._gis.url,
    }


def _status_headers() -> dict[str, str]:
    return {"X-Survey123-Request-Source": "API/Python"}


def _job_status_url(survey, job_id: str) -> str:
    return "https://{base}/api/featureReport/jobs/{jid}/status".format(
        base=survey._baseurl, jid=job_id
    )


def _job_cancel_url(survey, job_id: str) -> str:
    return "https://{base}/api/featureReport/jobs/{jid}/cancel".format(
        base=survey._baseurl, jid=job_id
    )


def cancel_report_job(survey, job_id: Optional[str] = None) -> Any:
    """
    Cancel a Survey123 Feature Report job.

    Uses ``job_id`` if provided, otherwise ``survey.last_report_job_id``.
    """
    jid = job_id or getattr(survey, "last_report_job_id", None)
    if not jid:
        raise ValueError(
            "No job_id provided and survey.last_report_job_id is not set"
        )
    return survey._si._gis._con.post(
        _job_cancel_url(survey, jid),
        _status_params(survey),
        add_headers=_status_headers(),
    )


def _wait_for_job_or_timeout(
    survey, job_id: str, job_timeout: float, cancel_on_timeout: bool
) -> None:
    """Poll until the job leaves Submitted/Executing, or raise TimeoutError."""
    gis = survey._si._gis
    params = _status_params(survey)
    headers = _status_headers()
    status_url = _job_status_url(survey, job_id)
    deadline = time.monotonic() + float(job_timeout)

    res = gis._con.get(status_url, params=params, add_headers=headers)
    while res.get("jobStatus") in _EXECUTING:
        if time.monotonic() >= deadline:
            if cancel_on_timeout:
                try:
                    cancel_report_job(survey, job_id)
                except Exception:
                    # Still raise TimeoutError; cancel is best-effort.
                    pass
            raise TimeoutError(
                f"Survey123 feature report job {job_id} timed out after "
                f"{job_timeout}s"
            )
        res = gis._con.get(status_url, params=params, add_headers=headers)
        time.sleep(1)


def apply() -> bool:
    """
    Monkey-patch ``Survey.generate_report`` and ``Survey._check_status``.

    After ``apply()``, callers may pass::

        survey.generate_report(..., job_timeout=90, cancel_on_timeout=True)

    Returns True if the patch was applied now, False if it was already applied.
    """
    global _applied, _original_generate_report, _original_check_status
    if _applied:
        return False

    from arcgis.apps.survey123 import Survey

    _original_generate_report = Survey.generate_report
    _original_check_status = Survey._check_status
    original_generate = _original_generate_report
    original_check = _original_check_status

    @wraps(original_generate)
    def generate_report_fixed(
        self,
        *args,
        job_timeout: Optional[float] = None,
        cancel_on_timeout: bool = False,
        **kwargs,
    ):
        self._report_job_timeout = job_timeout
        self._report_cancel_on_timeout = cancel_on_timeout
        try:
            return original_generate(self, *args, **kwargs)
        finally:
            self._report_job_timeout = None
            self._report_cancel_on_timeout = False

    # @wraps copies __wrapped__; expose the extra kwargs on the public signature.
    orig_sig = inspect.signature(original_generate)
    extra = [
        inspect.Parameter(
            "job_timeout",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Optional[float],
        ),
        inspect.Parameter(
            "cancel_on_timeout",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=bool,
        ),
    ]
    params = list(orig_sig.parameters.values())
    if params and params[-1].kind == inspect.Parameter.VAR_KEYWORD:
        params = params[:-1] + extra + params[-1:]
    else:
        params = params + extra
    generate_report_fixed.__signature__ = orig_sig.replace(parameters=params)

    @wraps(original_check)
    def check_status_fixed(self, res, status_type, save_folder):
        jid = res["jobId"]
        self.last_report_job_id = jid

        job_timeout = getattr(self, "_report_job_timeout", None)
        cancel_on_timeout = bool(getattr(self, "_report_cancel_on_timeout", False))
        if job_timeout is not None:
            _wait_for_job_or_timeout(
                self, jid, float(job_timeout), cancel_on_timeout
            )

        return original_check(self, res, status_type, save_folder)

    Survey.generate_report = generate_report_fixed
    Survey._check_status = check_status_fixed
    _applied = True
    return True


def unapply() -> bool:
    """Restore the original Survey methods if this module patched them."""
    global _applied, _original_generate_report, _original_check_status
    if (
        not _applied
        or _original_generate_report is None
        or _original_check_status is None
    ):
        return False

    from arcgis.apps.survey123 import Survey

    Survey.generate_report = _original_generate_report
    Survey._check_status = _original_check_status
    _applied = False
    _original_generate_report = None
    _original_check_status = None
    return True


if __name__ == "__main__":
    applied = apply()
    import arcgis

    print(
        f"arcgis {arcgis.__version__}: "
        f"{'patched' if applied else 'already patched'} "
        "Survey.generate_report / Survey._check_status "
        "(workaround for issue #2531)"
    )
