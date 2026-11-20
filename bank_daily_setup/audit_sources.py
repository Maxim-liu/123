#!/usr/bin/env python3
"""Audit the published edition and collection coverage without fetching websites.

Only parsed/published counts are reported. Raw duplicate candidates are not
claimed to be duplicate published offers. Unverified leads remain unverified.
"""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
SH = ZoneInfo("Asia/Shanghai")
REGIONS = ("cn", "hk", "us")


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def offers(value):
    return value if isinstance(value, list) else value.get("offers", [])


def duplicates(items, key_fn):
    counts = collections.Counter(key_fn(item) for item in items if item.get("title"))
    return sum(n - 1 for n in counts.values() if n > 1)


def build(base, edition=None):
    sources = load(base / "sources.json", {}).get("sources", [])
    registry = load(base / "source_registry.json", {}).get("candidates", [])
    status = load(base / "collection_status.json", {})
    latest = load(base.parent / "latest.json", {})
    curated = offers(load(base / "verified_offers.json", []))
    collected = offers(load(base / "collected_offers.json", {}))
    edition = edition or date(latest.get("report_date")) or date(status.get("report_date"))
    if not edition:
        raise ValueError("No valid edition date in latest.json or collection_status.json")
    iso = edition.isoformat()
    source_rows = status.get("sources", [])
    states = collections.Counter(x.get("status", "unknown") for x in source_rows)
    producing = [x for x in source_rows if x.get("items_accepted", 0) > 0]
    fresh = [x for x in collected if not x.get("retained_after_source_failure")]
    retained = [x for x in collected if x.get("retained_after_source_failure")]
    candidate_duplicates = duplicates(curated + collected,
        lambda x: (x.get("region"), str(x.get("bank", "")).casefold(),
                   "".join(str(x.get("title", "")).casefold().split())))
    url_duplicates = duplicates(curated + collected,
        lambda x: (x.get("region"), x.get("source_url")))
    expired = lambda arr: sum(bool(date(x.get("end_date")) and date(x["end_date"]) < edition) for x in arr)
    unique_cfg = len({x.get("id") for x in sources})
    unique_registry = len({x.get("id") for x in registry})
    if unique_cfg != len(sources) or unique_registry != len(registry):
        raise ValueError("Duplicate source IDs would corrupt the append-only audit")
    published = latest.get("report_date") == iso
    collected_match = status.get("report_date") == iso
    generated_at = latest.get("generated_at")
    publishing_lag_minutes = None
    if published and generated_at:
        try:
            generated = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated.tzinfo:
                target = dt.datetime.combine(edition - dt.timedelta(days=1), dt.time(21), tzinfo=SH)
                publishing_lag_minutes = round((generated.astimezone(SH) - target).total_seconds() / 60)
        except ValueError:
            pass
    warnings = []
    if not published or not collected_match:
        warnings.append("edition_mismatch_or_stale_inputs")
    if status.get("source_count") != len(sources) or len(source_rows) != len(sources):
        warnings.append("source_inventory_mismatch")
    if publishing_lag_minutes is not None and publishing_lag_minutes > 90:
        warnings.append("publication_more_than_90_minutes_after_21_bjt")
    if states.get("blocked") or states.get("error"):
        warnings.append("blocked_or_failed_sources_exist")
    result = {
        "schema_version": 1, "edition": iso, "timezone": "Asia/Shanghai",
        "published_report": {
            "available_for_edition": published,
            "generated_at": generated_at if published else None,
            "lag_minutes_from_previous_day_21_bjt": publishing_lag_minutes,
            "verified_count": latest.get("verified_count") if published else None,
            "unverified_count": latest.get("collected_count") if published else None,
            "regions": latest.get("regions") if published else None,
        },
        "registered_sources": {
            "configured": len(sources), "candidate_registry": len(registry),
            "reachable": states.get("ok", 0) + states.get("empty", 0),
            "producing": len(producing), "empty": states.get("empty", 0),
            "blocked": states.get("blocked", 0), "error": states.get("error", 0),
            "disabled": states.get("disabled", 0),
            "candidate_states": dict(sorted(collections.Counter(x.get("status", "unknown") for x in registry).items())),
            "producing_ids": sorted(x["id"] for x in producing),
            "failed_ids": sorted(x["id"] for x in source_rows if x.get("status") in ("blocked", "error")),
        },
        "raw_collection": {
            "edition_matches": collected_match,
            "total": len(collected), "fresh": len(fresh), "retained_after_failure": len(retained),
            "regions": {r: {"total": sum(x.get("region") == r for x in collected),
                             "fresh": sum(x.get("region") == r for x in fresh),
                             "retained": sum(x.get("region") == r for x in retained)} for r in REGIONS},
        },
        "quality_checks": {
            "raw_duplicate_title_bank_candidates": candidate_duplicates,
            "raw_duplicate_url_candidates": url_duplicates,
            "expired_curated_records_excluded_by_date": expired(curated),
            "expired_collected_records_excluded_by_date": expired(collected),
            "notice": "Raw duplicates are review candidates, not counts of published duplicate offers."
        },
        "warnings": warnings,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    report = build(args.base, date(args.date) if args.date else None)
    target = args.out or args.base / "audits" / (report["edition"] + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"edition": report["edition"], "sources": report["registered_sources"],
                      "quality": report["quality_checks"], "warnings": report["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
