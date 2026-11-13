"""Private, aggregate-only comparison of two public Tokenometer snapshots.

The report describes arithmetic changes in saved aggregate snapshots. It does
not identify new events, explain why a value changed, or treat absent daily
rows as measured zero use.
"""
from __future__ import annotations

from typing import Any

from .release_guard import validate_public_payload


PROVIDERS = ("codex", "antigravity")
COUNTERS = ("fresh_input", "cache_read", "cache_write", "output", "total")
REPORT_NOTE = (
    "Signed deltas compare saved aggregate snapshots and do not equal newly used tokens or "
    "explain causes. Provider deltas use provider totals; supplemental daily totals are "
    "reported separately and included in the combined delta. Missing daily rows mean no "
    "retained dated record, not measured zero use. Partial coverage may limit either snapshot."
)


def _supplemental_by_date(snapshot: dict[str, Any]) -> dict[str, int]:
    return {row["date"]: row["total"] for row in snapshot.get("supplemental_daily", [])}


def _daily_by_date(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in snapshot["daily"]:
        state = result.setdefault(row["date"], {"events": 0, "total": 0})
        state["events"] += row["events"]
        state["total"] += row["total"]
    return result


def _daily_state(snapshot: dict[str, Any], day: str,
                 daily: dict[str, dict[str, Any]], supplemental: dict[str, int]) -> dict[str, Any]:
    local = daily.get(day)
    has_daily = local is not None
    has_supplement = day in supplemental
    has_record = has_daily or has_supplement
    daily_total = local["total"] if has_daily else None
    supplement_total = supplemental.get(day)
    return {
        "has_daily_record": has_daily,
        "daily_events": local["events"] if has_daily else None,
        "daily_total": daily_total,
        "has_supplemental_record": has_supplement,
        "supplemental_total": supplement_total,
        "has_any_record": has_record,
        "combined_recorded_total": (
            (daily_total or 0) + (supplement_total or 0) if has_record else None
        ),
    }


def _delta(previous: int | None, current: int) -> int | None:
    return None if previous is None else current - previous


def _value_delta(previous: int | None, current: int) -> dict[str, int | None]:
    return {"previous": previous, "current": current, "delta": _delta(previous, current)}


def _snapshot_quality(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    quality = snapshot["quality"]
    return {
        "coverage_status": quality["coverage_status"],
        "source_errors": quality["source_errors"],
        "conflicted_events": quality["conflicted_events"],
        "undated_events": quality["undated_events"],
        "undated_totals": dict(quality["undated_totals"]),
        "providers": {
            name: {
                "execution_status": snapshot["providers"][name]["execution_status"],
                "coverage_status": snapshot["providers"][name]["coverage_status"],
                "source_errors": snapshot["providers"][name]["source_errors"],
            }
            for name in PROVIDERS
        },
    }


def _history_audit(snapshot: dict[str, Any] | None, provider: str) -> dict[str, int | None] | None:
    if snapshot is None or "history_audit" not in snapshot:
        return None
    # A null value is meaningful for an unobserved provider; keep it unknown.
    return dict(snapshot["history_audit"][provider])


def _freshness(snapshot: dict[str, Any] | None, provider: str) -> dict[str, str | None] | None:
    if snapshot is None:
        return None
    state = snapshot["providers"][provider]
    return {
        "latest_cutoff": state["latest_cutoff"],
        "latest_capture_completed_at": state["latest_capture_completed_at"],
        "last_successful_cutoff": state["last_successful_cutoff"],
    }


def build_report(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Compare strict aggregate snapshots without inferring event-level usage.

    ``None`` means no saved baseline. Inputs are validated using the release
    aggregate schema so unknown fields, malformed counters, and private
    per-event data cannot enter the report.
    """
    if type(current) is not dict:
        raise ValueError("Current aggregate must be an object")
    current = validate_public_payload(current)
    if previous is not None:
        if type(previous) is not dict:
            raise ValueError("Previous aggregate must be an object or null")
        previous = validate_public_payload(previous)

    baseline = previous is not None
    current_supplemental = sum(row["total"] for row in current.get("supplemental_daily", []))
    previous_supplemental = (
        sum(row["total"] for row in previous.get("supplemental_daily", [])) if previous else None
    )
    previous_recorded = previous["totals"]["total"] if previous else None
    current_recorded = current["totals"]["total"]
    previous_combined = (
        previous_recorded + previous_supplemental if previous_recorded is not None else None
    )
    current_combined = current_recorded + current_supplemental

    providers = {}
    for name in PROVIDERS:
        old_total = previous["providers"][name]["totals"]["total"] if previous else None
        new_total = current["providers"][name]["totals"]["total"]
        providers[name] = _value_delta(old_total, new_total)

    undated = {
        "events": _value_delta(
            previous["quality"]["undated_events"] if previous else None,
            current["quality"]["undated_events"],
        ),
        "totals": {
            key: _value_delta(
                previous["quality"]["undated_totals"][key] if previous else None,
                current["quality"]["undated_totals"][key],
            )
            for key in COUNTERS
        },
    }

    if previous is None:
        daily_comparison = {
            "status": "baseline_unknown",
            "previous_timezone": None,
            "current_timezone": current["timezone"],
            "changed_dates": None,
        }
    elif previous["timezone"] != current["timezone"]:
        daily_comparison = {
            "status": "not_comparable",
            "reason": "timezone_changed",
            "previous_timezone": previous["timezone"],
            "current_timezone": current["timezone"],
            "changed_dates": None,
        }
    else:
        old_daily, new_daily = _daily_by_date(previous), _daily_by_date(current)
        old_supplemental = _supplemental_by_date(previous)
        new_supplemental = _supplemental_by_date(current)
        changed_dates = []
        all_dates = sorted(set(old_daily) | set(new_daily) | set(old_supplemental) | set(new_supplemental))
        for day in all_dates:
            old_state = _daily_state(previous, day, old_daily, old_supplemental)
            new_state = _daily_state(current, day, new_daily, new_supplemental)
            if old_state == new_state:
                continue
            old_total = old_state["combined_recorded_total"]
            new_total = new_state["combined_recorded_total"]
            # For arithmetic only, a missing retained row contributes no stored
            # aggregate to the sum. Presence flags keep that distinct from
            # evidence of measured zero use.
            changed_dates.append({
                "date": day,
                "previous": old_state,
                "current": new_state,
                "recorded_total_delta": (new_total or 0) - (old_total or 0),
            })
        daily_comparison = {
            "status": "comparable",
            "previous_timezone": previous["timezone"],
            "current_timezone": current["timezone"],
            "changed_dates": changed_dates,
        }

    return {
        "schema_version": 1,
        "artifact": "tokenometer.private.quality-report.v1",
        "baseline_status": "available" if baseline else "unknown",
        "baseline_generated_at": previous["generated_at"] if previous else None,
        "current_generated_at": current["generated_at"],
        "totals": {
            "recorded": _value_delta(previous_recorded, current_recorded),
            "supplemental_daily": _value_delta(previous_supplemental, current_supplemental),
            "combined": _value_delta(previous_combined, current_combined),
        },
        "provider_total_deltas": providers,
        "daily_comparison": daily_comparison,
        "undated": undated,
        "quality": {
            "previous": _snapshot_quality(previous),
            "current": _snapshot_quality(current),
            "conflicted_events": _value_delta(
                previous["quality"]["conflicted_events"] if previous else None,
                current["quality"]["conflicted_events"],
            ),
            "source_errors": _value_delta(
                previous["quality"]["source_errors"] if previous else None,
                current["quality"]["source_errors"],
            ),
        },
        "retained_not_latest": {
            name: {
                "previous": _history_audit(previous, name),
                "current": _history_audit(current, name),
            }
            for name in PROVIDERS
        },
        "freshness": {
            name: {
                "previous": _freshness(previous, name),
                "current": _freshness(current, name),
            }
            for name in PROVIDERS
        },
        "note": REPORT_NOTE,
    }
