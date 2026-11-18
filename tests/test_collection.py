"""Regression checks for source classification (no network requests)."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "collect_offers", Path(__file__).resolve().parents[1] / "bank_daily_setup" / "collect_offers.py"
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CollectionClassificationTests(unittest.TestCase):
    def test_purchases_does_not_mean_chase_bank(self):
        source = {"id": "test", "name": "Public feed", "region": "us", "timezone": "America/New_York"}
        now = dt.datetime(2026, 11, 18, 13, 30, tzinfo=dt.timezone.utc)
        for title in (
            "Instacart: Free Turkey Up To $40, Requires $10 In Other Purchases [11/18 – 11/22]",
            "United MPX App Restricts Gift Card Purchases To Premier Members & Cardholders",
        ):
            with self.subTest(title=title):
                self.assertNotEqual(collector.bank_name(title, source), "Chase")
                result = collector.normalize(
                    {"title": title, "source_url": "https://example.com/article",
                     "published_raw": "Wed, 18 Nov 2026 12:00:00 +0000"},
                    source, now, dt.date(2026, 11, 19),
                )
                self.assertIsNone(result)
        self.assertEqual(collector.bank_name("Chase $400 checking bonus", source), "Chase")
        self.assertEqual(collector.bank_name("Citi credit card offer", source), "Citi")
        self.assertNotEqual(collector.bank_name("Rewards in participating cities", source), "Citi")


if __name__ == "__main__":
    unittest.main()
