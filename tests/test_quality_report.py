import json
import unittest

from tokenometer.aggregate import summarize
from tokenometer.models import UsageEvent, digest
from tokenometer.public import public_snapshot
from tokenometer.quality_report import build_report


def event(name, date, amount, *, undated=False, platform="codex"):
    return UsageEvent(
        f"{platform}:{digest(name)}", platform, digest("session-" + name),
        None if undated else f"{date}T12:00:00Z", amount, 0, 0, 0, 0,
    )


def aggregate(days=(), *, undated=0, timezone="UTC", supplemental=(),
              errors=0, conflicts=0, history_audit=None, cutoff="2026-11-10T12:00:00Z"):
    events = [event(f"{i}-{date}", date, amount) for i, (date, amount) in enumerate(days)]
    if undated:
        events.append(event("undated", "2026-11-01", undated, undated=True))
    summary = summarize(events, timezone)
    codex_state = {
        "latest_cutoff": cutoff,
        "latest_capture_completed_at": "2026-11-10T12:05:00Z",
        "last_successful_cutoff": cutoff,
        "execution_status": "complete",
        "coverage_status": "partial" if history_audit and history_audit["codex"]["retained_events_not_in_latest"] else "qualified",
        "errors": errors,
    }
    snapshot = {
        "summary": summary,
        "quality": {"coverage_status": "partial", "conflicted_events": conflicts},
        "providers": {"codex": codex_state},
    }
    if supplemental:
        snapshot["supplemental_daily"] = [
            {"date": date, "total": amount} for date, amount in supplemental
        ]
    if history_audit is not None:
        snapshot["history_audit"] = history_audit
    return public_snapshot(snapshot)


class QualityReportTests(unittest.TestCase):
    def test_first_run_keeps_absent_baseline_unknown_instead_of_zero(self):
        current = aggregate([("2026-11-01", 12)], supplemental=[("2026-10-20", 8)])

        report = build_report(None, current)

        self.assertEqual(report["baseline_status"], "unknown")
        self.assertIsNone(report["baseline_generated_at"])
        self.assertEqual(report["totals"]["recorded"], {
            "previous": None, "current": 12, "delta": None,
        })
        self.assertEqual(report["totals"]["supplemental_daily"], {
            "previous": None, "current": 8, "delta": None,
        })
        self.assertEqual(report["totals"]["combined"]["current"], 20)
        self.assertIsNone(report["totals"]["combined"]["delta"])
        self.assertEqual(report["daily_comparison"]["status"], "baseline_unknown")
        self.assertIsNone(report["daily_comparison"]["changed_dates"])
        self.assertIsNone(report["provider_total_deltas"]["antigravity"]["delta"])

    def test_supplements_are_separate_and_included_in_signed_combined_delta(self):
        previous = aggregate([("2026-11-01", 100)], supplemental=[("2026-10-20", 50)])
        current = aggregate([("2026-11-01", 80)], supplemental=[("2026-10-20", 65)])

        report = build_report(previous, current)

        self.assertEqual(report["totals"]["recorded"]["delta"], -20)
        self.assertEqual(report["totals"]["supplemental_daily"]["delta"], 15)
        self.assertEqual(report["totals"]["combined"], {
            "previous": 150, "current": 145, "delta": -5,
        })
        self.assertEqual(report["provider_total_deltas"]["codex"]["delta"], -20)
        changed = report["daily_comparison"]["changed_dates"]
        self.assertEqual([item["date"] for item in changed], ["2026-10-20", "2026-11-01"])
        self.assertEqual(changed[0]["recorded_total_delta"], 15)
        self.assertIn("do not equal newly used tokens", report["note"])

    def test_daily_rows_appearing_and_disappearing_have_presence_flags(self):
        previous = aggregate([("2026-11-01", 20), ("2026-11-02", 7)])
        current = aggregate([("2026-11-01", 25)])

        changed = build_report(previous, current)["daily_comparison"]["changed_dates"]

        self.assertEqual([row["date"] for row in changed], ["2026-11-01", "2026-11-02"])
        self.assertEqual(changed[0]["recorded_total_delta"], 5)
        self.assertEqual(changed[1]["recorded_total_delta"], -7)
        self.assertFalse(changed[1]["current"]["has_any_record"])
        self.assertIsNone(changed[1]["current"]["combined_recorded_total"])
        self.assertTrue(changed[1]["previous"]["has_daily_record"])

    def test_timezone_change_disables_daily_comparison_but_keeps_totals(self):
        previous = aggregate([("2026-11-01", 20)], timezone="UTC")
        current = aggregate([("2026-11-01", 24)], timezone="Europe/Warsaw")

        report = build_report(previous, current)

        self.assertEqual(report["totals"]["combined"]["delta"], 4)
        self.assertEqual(report["daily_comparison"]["status"], "not_comparable")
        self.assertEqual(report["daily_comparison"]["reason"], "timezone_changed")
        self.assertIsNone(report["daily_comparison"]["changed_dates"])

    def test_quality_undated_conflicts_history_audit_and_freshness_are_aggregate_only(self):
        audit = {
            "codex": {"retained_events_not_in_latest": 1, "retained_tokens_not_in_latest": 11},
            "antigravity": {"retained_events_not_in_latest": None, "retained_tokens_not_in_latest": None},
        }
        previous = aggregate([("2026-11-01", 10)], undated=3, conflicts=1)
        current = aggregate([("2026-11-01", 8)], undated=5, conflicts=2,
                            errors=1, history_audit=audit,
                            cutoff="2026-11-11T12:00:00Z")

        report = build_report(previous, current)

        self.assertEqual(report["undated"]["events"]["delta"], 0)
        self.assertEqual(report["undated"]["totals"]["total"]["delta"], 2)
        self.assertEqual(report["quality"]["conflicted_events"]["delta"], 1)
        self.assertEqual(report["quality"]["source_errors"]["delta"], 1)
        self.assertEqual(report["retained_not_latest"]["codex"]["current"], audit["codex"])
        self.assertIsNone(report["retained_not_latest"]["codex"]["previous"])
        self.assertIsNone(report["retained_not_latest"]["antigravity"]["current"]["retained_events_not_in_latest"])
        self.assertEqual(report["freshness"]["codex"]["current"]["last_successful_cutoff"],
                         "2026-11-11T12:00:00Z")
        serialized = json.dumps(report)
        for forbidden in ("source_refs", "event_key", "session_key", "/Users/", "account"):
            self.assertNotIn(forbidden, serialized)

    def test_strict_aggregate_validation_rejects_unknown_or_private_fields(self):
        current = aggregate([("2026-11-01", 5)])
        current["private_debug"] = "PRIVATE_CANARY"

        with self.assertRaises(ValueError) as raised:
            build_report(None, current)
        self.assertNotIn("PRIVATE_CANARY", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
