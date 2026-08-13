#!/usr/bin/env python3
"""Unit tests for the #2531 Survey.generate_report timeout workaround.

Run from the repo root (so ``misc`` is importable)::

    python -m unittest misc.tools.issue_2531.test_fix_survey_generate_report_timeout_2531 -v

Or::

    python misc/tools/issue_2531/test_fix_survey_generate_report_timeout_2531.py
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Repo root on sys.path when executed as a script.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from arcgis.apps.survey123 import Survey  # noqa: E402

from misc.tools.issue_2531 import (  # noqa: E402
    apply,
    cancel_report_job,
    unapply,
)
from misc.tools.issue_2531 import fix_survey_generate_report_timeout_2531 as fix  # noqa: E402


def _make_survey() -> MagicMock:
    survey = MagicMock(spec=Survey)
    survey._baseurl = "survey123.arcgis.com"
    survey._si._gis.url = "https://www.arcgis.com"
    survey._si._gis.users.me.username = "test_user"
    survey._si._gis._con.get = MagicMock()
    survey._si._gis._con.post = MagicMock(return_value={"jobStatus": "esriJobStatusCancelled"})
    return survey


class ApplyUnapplyTests(unittest.TestCase):
    def tearDown(self) -> None:
        unapply()

    def test_apply_unapply_roundtrip(self) -> None:
        original_generate = Survey.generate_report
        original_check = Survey._check_status

        self.assertTrue(apply())
        self.assertIsNot(Survey.generate_report, original_generate)
        self.assertIsNot(Survey._check_status, original_check)
        self.assertFalse(apply())  # already applied

        self.assertTrue(unapply())
        self.assertIs(Survey.generate_report, original_generate)
        self.assertIs(Survey._check_status, original_check)
        self.assertFalse(unapply())  # already restored

    def test_generate_report_signature_exposes_timeout_kwargs(self) -> None:
        apply()
        params = inspect.signature(Survey.generate_report).parameters
        self.assertIn("job_timeout", params)
        self.assertIn("cancel_on_timeout", params)
        unapply()
        restored = inspect.signature(Survey.generate_report).parameters
        self.assertNotIn("job_timeout", restored)
        self.assertNotIn("cancel_on_timeout", restored)


class WaitOrTimeoutUnitTests(unittest.TestCase):
    """Tests for ``_wait_for_job_or_timeout`` without full Survey patching."""

    def setUp(self) -> None:
        self.survey = _make_survey()

    @patch.object(fix.time, "sleep", return_value=None)
    def test_returns_when_job_succeeds(self, _sleep: MagicMock) -> None:
        self.survey._si._gis._con.get.side_effect = [
            {"jobStatus": "esriJobExecuting"},
            {"jobStatus": "esriJobSucceeded"},
        ]
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 0.5, 1.0]):
            fix._wait_for_job_or_timeout(
                self.survey, "job-1", job_timeout=30.0, cancel_on_timeout=False
            )
        self.assertEqual(self.survey._si._gis._con.get.call_count, 2)
        self.survey._si._gis._con.post.assert_not_called()

    @patch.object(fix.time, "sleep", return_value=None)
    def test_timeout_raises_without_cancel(self, _sleep: MagicMock) -> None:
        self.survey._si._gis._con.get.return_value = {
            "jobStatus": "esriJobExecuting"
        }
        # First monotonic: deadline = 0 + 5 = 5; loop body sees 10 >= 5.
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 10.0]):
            with self.assertRaises(TimeoutError) as ctx:
                fix._wait_for_job_or_timeout(
                    self.survey,
                    "job-stuck",
                    job_timeout=5.0,
                    cancel_on_timeout=False,
                )
        self.assertIn("job-stuck", str(ctx.exception))
        self.assertIn("5.0s", str(ctx.exception))
        self.survey._si._gis._con.post.assert_not_called()

    @patch.object(fix.time, "sleep", return_value=None)
    def test_timeout_cancels_when_requested(self, _sleep: MagicMock) -> None:
        self.survey._si._gis._con.get.return_value = {
            "jobStatus": "esriJobSubmitted"
        }
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaises(TimeoutError):
                fix._wait_for_job_or_timeout(
                    self.survey,
                    "job-cancel-me",
                    job_timeout=1.0,
                    cancel_on_timeout=True,
                )
        self.survey._si._gis._con.post.assert_called_once()
        args, kwargs = self.survey._si._gis._con.post.call_args
        self.assertIn(
            "/api/featureReport/jobs/job-cancel-me/cancel",
            args[0],
        )
        self.assertEqual(
            kwargs.get("add_headers"),
            {"X-Survey123-Request-Source": "API/Python"},
        )

    @patch.object(fix.time, "sleep", return_value=None)
    def test_timeout_still_raises_if_cancel_fails(self, _sleep: MagicMock) -> None:
        self.survey._si._gis._con.get.return_value = {
            "jobStatus": "esriJobExecuting"
        }
        self.survey._si._gis._con.post.side_effect = RuntimeError("network")
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 50.0]):
            with self.assertRaises(TimeoutError) as ctx:
                fix._wait_for_job_or_timeout(
                    self.survey,
                    "job-x",
                    job_timeout=2.0,
                    cancel_on_timeout=True,
                )
        self.assertIn("job-x", str(ctx.exception))


class CancelReportJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.survey = _make_survey()

    def test_cancel_with_explicit_job_id(self) -> None:
        cancel_report_job(self.survey, job_id="explicit-id")
        args, kwargs = self.survey._si._gis._con.post.call_args
        self.assertIn("/jobs/explicit-id/cancel", args[0])
        self.assertEqual(args[1]["username"], "test_user")
        self.assertEqual(args[1]["portalUrl"], "https://www.arcgis.com")

    def test_cancel_uses_last_report_job_id(self) -> None:
        self.survey.last_report_job_id = "from-attr"
        cancel_report_job(self.survey)
        args, _kwargs = self.survey._si._gis._con.post.call_args
        self.assertIn("/jobs/from-attr/cancel", args[0])

    def test_cancel_requires_job_id(self) -> None:
        self.survey.last_report_job_id = None
        with self.assertRaises(ValueError):
            cancel_report_job(self.survey)


class GenerateReportStashTests(unittest.TestCase):
    """Patched generate_report stashes timeout flags for _check_status."""

    def setUp(self) -> None:
        self._real_generate = Survey.generate_report
        self._real_check = Survey._check_status

    def tearDown(self) -> None:
        unapply()
        Survey.generate_report = self._real_generate
        Survey._check_status = self._real_check

    def test_stashes_kwargs_during_call(self) -> None:
        seen_box: dict = {}

        def capturing_generate(self, *args, **kwargs):
            seen_box["timeout"] = getattr(self, "_report_job_timeout", None)
            seen_box["cancel"] = getattr(self, "_report_cancel_on_timeout", None)
            return "ok"

        Survey.generate_report = capturing_generate
        apply()
        survey = MagicMock()
        result = Survey.generate_report(
            survey,
            report_template=MagicMock(),
            job_timeout=90,
            cancel_on_timeout=True,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(seen_box["timeout"], 90)
        self.assertEqual(seen_box["cancel"], True)
        self.assertIsNone(survey._report_job_timeout)
        self.assertFalse(survey._report_cancel_on_timeout)


class PatchedCheckStatusIntegrationTests(unittest.TestCase):
    """End-to-end patched _check_status with a stubbed stock original."""

    def setUp(self) -> None:
        self._real_generate = Survey.generate_report
        self._real_check = Survey._check_status
        self.stock_check = MagicMock(return_value="final-report")
        Survey._check_status = self.stock_check
        apply()
        self.survey = _make_survey()

    def tearDown(self) -> None:
        unapply()
        Survey.generate_report = self._real_generate
        Survey._check_status = self._real_check

    def test_sets_last_report_job_id_without_timeout(self) -> None:
        self.survey._report_job_timeout = None
        result = Survey._check_status(
            self.survey,
            {"jobId": "job-abc-123"},
            "generate_report",
            None,
        )
        self.assertEqual(result, "final-report")
        self.assertEqual(self.survey.last_report_job_id, "job-abc-123")
        self.stock_check.assert_called_once_with(
            self.survey,
            {"jobId": "job-abc-123"},
            "generate_report",
            None,
        )
        self.survey._si._gis._con.get.assert_not_called()

    @patch.object(fix.time, "sleep", return_value=None)
    def test_timeout_path_skips_stock_check(self, _sleep: MagicMock) -> None:
        self.survey._report_job_timeout = 1.0
        self.survey._report_cancel_on_timeout = True
        self.survey._si._gis._con.get.return_value = {
            "jobStatus": "esriJobExecuting"
        }
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 10.0]):
            with self.assertRaises(TimeoutError):
                Survey._check_status(
                    self.survey,
                    {"jobId": "jid-1"},
                    "generate_report",
                    None,
                )
        self.assertEqual(self.survey.last_report_job_id, "jid-1")
        self.stock_check.assert_not_called()
        self.survey._si._gis._con.post.assert_called_once()
        self.assertIn(
            "/jobs/jid-1/cancel",
            self.survey._si._gis._con.post.call_args[0][0],
        )

    @patch.object(fix.time, "sleep", return_value=None)
    def test_success_before_timeout_calls_stock_check(
        self, _sleep: MagicMock
    ) -> None:
        self.survey._report_job_timeout = 60.0
        self.survey._report_cancel_on_timeout = False
        self.survey._si._gis._con.get.side_effect = [
            {"jobStatus": "esriJobExecuting"},
            {"jobStatus": "esriJobSucceeded"},
        ]
        with patch.object(fix.time, "monotonic", side_effect=[0.0, 1.0, 2.0]):
            result = Survey._check_status(
                self.survey,
                {"jobId": "jid-ok"},
                "generate_report",
                "/tmp",
            )
        self.assertEqual(result, "final-report")
        self.assertEqual(self.survey.last_report_job_id, "jid-ok")
        self.stock_check.assert_called_once_with(
            self.survey,
            {"jobId": "jid-ok"},
            "generate_report",
            "/tmp",
        )
        self.survey._si._gis._con.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
