import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "bank_daily_setup" / "audit_sources.py"
SPEC = importlib.util.spec_from_file_location("audit_sources", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SourceAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.base = root / "bank_daily_setup"
        self.base.mkdir()
        def write(name, data, root=self.base):
            (root / name).write_text(json.dumps(data), encoding="utf-8")
        write("sources.json", {"sources": [{"id": "a"}, {"id": "b"}]})
        write("source_registry.json", {"candidates": [{"id": "c"}]})
        write("collection_status.json", {
            "report_date": "2026-11-20", "source_count": 2,
            "sources": [{"id": "a", "status": "ok", "items_accepted": 2},
                        {"id": "b", "status": "blocked", "items_accepted": 0}]
        })
        write("verified_offers.json", [{"region": "cn", "bank": "B", "title": "Old",
            "source_url": "https://example.com/old", "end_date": "2026-11-19"}])
        write("collected_offers.json", {"offers": [
            {"region": "us", "bank": "A", "title": "Deal",
             "source_url": "https://example.com/a", "retained_after_source_failure": False},
            {"region": "us", "bank": "A", "title": " Deal ",
             "source_url": "https://example.com/a", "retained_after_source_failure": True}
        ]})
        write("latest.json", {"report_date": "2026-11-20", "generated_at": "2026-11-20T05:15:56+08:00",
            "verified_count": 1, "collected_count": 2, "regions": {"cn": {}, "hk": {}, "us": {}}}, root)

    def test_classifies_access_production_and_retained_separately(self):
        report = module.build(self.base)
        self.assertEqual(report["registered_sources"]["configured"], 2)
        self.assertEqual(report["registered_sources"]["candidate_registry"], 1)
        self.assertEqual(report["registered_sources"]["reachable"], 1)
        self.assertEqual(report["registered_sources"]["producing"], 1)
        self.assertEqual(report["registered_sources"]["blocked"], 1)
        self.assertEqual(report["raw_collection"]["fresh"], 1)
        self.assertEqual(report["raw_collection"]["retained_after_failure"], 1)
        self.assertEqual(report["raw_collection"]["regions"]["us"]["total"], 2)

    def test_expired_and_duplicate_candidates_are_labelled_not_promoted(self):
        report = module.build(self.base)
        quality = report["quality_checks"]
        self.assertEqual(quality["expired_curated_records_excluded_by_date"], 1)
        self.assertEqual(quality["raw_duplicate_url_candidates"], 1)
        self.assertEqual(quality["raw_duplicate_title_bank_candidates"], 1)
        self.assertIn("blocked_or_failed_sources_exist", report["warnings"])

    def test_publication_lag_warning_without_modifying_schedule(self):
        report = module.build(self.base)
        self.assertGreater(report["published_report"]["lag_minutes_from_previous_day_21_bjt"], 90)
        self.assertIn("publication_more_than_90_minutes_after_21_bjt", report["warnings"])

    def test_duplicate_ids_fail_fast(self):
        path = self.base / "source_registry.json"
        path.write_text(json.dumps({"candidates": [{"id": "x"}, {"id": "x"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate source IDs"):
            module.build(self.base)


if __name__ == "__main__":
    unittest.main()
