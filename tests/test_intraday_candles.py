import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import market_api


class Response:
    ok = True

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class IntradayCandlesTests(unittest.TestCase):
    def test_fugle_resistance_uses_only_complete_bars(self):
        now = datetime.now(market_api.TZ)
        timestamps = [now - timedelta(minutes=n) for n in (30, 25, 20, 15, 2)]
        closes = (98, 101, 99, 102, 97)
        payload = {"data": [
            {"date": when.isoformat(), "open": 99, "high": 101,
             "low": 98, "close": close, "volume": 10}
            for when, close in zip(timestamps, closes)
        ]}
        with patch.object(market_api, "FUGLE_TOKEN", "key"), patch.object(market_api.requests.Session, "get", return_value=Response(payload)):
            result = market_api.intraday_candles("2327", resistance=100)
        self.assertEqual(result["source"], "Fugle")
        self.assertEqual(result["bar_count"], 5)
        self.assertEqual(result["confirmed_bar_count"], 4)
        self.assertFalse(result["last_bar_complete"])
        self.assertEqual(result["resistance_review"]["touch_bar_count"], 4)
        self.assertEqual(result["resistance_review"]["closes_above_count"], 2)
        self.assertTrue(result["resistance_review"]["closed_back_below_after_breakout"])
        self.assertTrue(result["resistance_review"]["latest_confirmed_close_above"])

    def test_yahoo_fallback_converts_shares_to_lots(self):
        start = datetime.now(market_api.TZ) - timedelta(minutes=15)
        ts = int(start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0).timestamp())
        yahoo = {"chart": {"result": [{"meta": {"symbol": "2327.TW"},
            "timestamp": [ts, ts + 98], "indicators": {"quote": [{"open": [100, 101], "high": [102, 101],
                "low": [99, 101], "close": [101, 101], "volume": [3500, 0]}]}}]}}
        with patch.object(market_api, "FUGLE_TOKEN", ""), patch.object(market_api.requests.Session, "get", return_value=Response(yahoo)):
            result = market_api.intraday_candles("2327")
        self.assertEqual(result["source"], "Yahoo Finance 2327.TW")
        self.assertEqual(result["bar_count"], 1)
        self.assertEqual(result["bars"][0]["volume_lots"], 3.5)
        self.assertIn("is_recent_for_intraday_decisions", result)

    def test_invalid_interval_and_no_data(self):
        with self.assertRaises(ValueError):
            market_api.intraday_candles("2327", interval="30")
        with patch.object(market_api, "FUGLE_TOKEN", ""), patch.object(market_api.requests.Session, "get", return_value=Response({"chart": {"result": None}})):
            with self.assertRaises(market_api.HTTPException):
                market_api.intraday_candles("2327")


if __name__ == "__main__":
    unittest.main()
