import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import market_api


class MarketScanTests(unittest.TestCase):
    def setUp(self):
        market_api._SCAN_CACHE.clear()

    def test_analyzer_finds_tight_base_near_resistance(self):
        today = datetime.now(market_api.TZ).date()
        bars = []
        for i in range(60):
            close = 100 + i * 0.16 + (i % 4) * 0.15
            bars.append({
                "date": (today - timedelta(days=70 - i)).isoformat(),
                "open": close - 0.2,
                "high": close + 0.7,
                "low": close - 0.7,
                "close": close,
                "volume": 250_000,
            })
        quote = {
            "symbol": "1234.TW", "quoteType": "EQUITY", "shortName": "測試股",
            "regularMarketPrice": 109.6, "regularMarketChangePercent": 0.8,
            "regularMarketVolume": 300_000,
        }
        with patch.object(market_api, "_yahoo_daily_history", return_value=bars):
            result = market_api._analyze_scan_quote(quote, active_rank=4)
        self.assertIsNotNone(result)
        self.assertEqual(result["stock_id"], "1234")
        self.assertGreaterEqual(result["distance_to_resistance_percent"], 0)
        self.assertLessEqual(result["distance_to_resistance_percent"], 5)
        self.assertGreater(result["breakout_trigger_to_watch"], result["resistance_20d"])

    def test_analyzer_rejects_already_running_stock_before_history_fetch(self):
        quote = {
            "symbol": "1234.TW", "quoteType": "EQUITY", "regularMarketPrice": 120,
            "regularMarketChangePercent": 7.5,
        }
        with patch.object(market_api, "_yahoo_daily_history") as history:
            self.assertIsNone(market_api._analyze_scan_quote(quote, active_rank=1))
            history.assert_not_called()

    def test_analyzer_keeps_earlier_setup_as_lower_grade_candidate(self):
        today = datetime.now(market_api.TZ).date()
        bars = []
        for i in range(60):
            close = 100 + i * 0.16 + (i % 4) * 0.15
            bars.append({
                "date": (today - timedelta(days=70 - i)).isoformat(),
                "open": close - 0.2,
                "high": close + 0.7,
                "low": close - 0.7,
                "close": close,
                "volume": 250_000,
            })
        resistance = max(bar["high"] for bar in bars[-20:])
        quote = {
            "symbol": "1234.TW", "quoteType": "EQUITY", "shortName": "測試股",
            "regularMarketPrice": resistance / 1.07,
            "regularMarketChangePercent": 0.2,
            "regularMarketVolume": 300_000,
        }
        with patch.object(market_api, "_yahoo_daily_history", return_value=bars):
            result = market_api._analyze_scan_quote(quote, active_rank=4)
        self.assertIsNotNone(result)
        self.assertEqual(result["setup_grade"], "B")
        self.assertEqual(result["setup_label"], "提前觀察")
        self.assertLessEqual(result["distance_to_resistance_percent"], 8)

    def test_scan_merges_public_screens_and_labels_limited_coverage(self):
        active = [{"symbol": "1234.TW", "quoteType": "EQUITY", "regularMarketVolume": 1000}]
        gainers = [{"symbol": "5678.TWO", "quoteType": "EQUITY", "regularMarketVolume": 500}]
        with patch.object(market_api, "_yahoo_screener", side_effect=[active, gainers]), \
             patch.object(market_api, "_analyze_scan_quote", side_effect=[
                 {"stock_id": "1234", "score": 80, "active_rank": 1, "distance_to_resistance_percent": 2},
                 {"stock_id": "5678", "score": 70, "active_rank": 1, "distance_to_resistance_percent": 3},
             ]):
            result = market_api._scan_prebreakout_candidates(limit=5)
        self.assertEqual(result["candidate_pool_count"], 2)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["source"], "Yahoo Finance free predefined screeners + completed daily candles")
        self.assertTrue(any("不等於全市場" in note for note in result["data_limitations"]))

    def test_market_and_limit_validation(self):
        with self.assertRaises(ValueError):
            market_api._scan_prebreakout_candidates(market="ESB")
        with self.assertRaises(ValueError):
            market_api._scan_prebreakout_candidates(limit=21)


if __name__ == "__main__":
    unittest.main()
