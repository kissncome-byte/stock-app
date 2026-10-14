import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import market_api


class WeeklyCandlesTests(unittest.TestCase):
    def test_groups_trading_days_and_excludes_current_week(self):
        monday = datetime.now(market_api.TZ).date()
        monday -= timedelta(days=monday.weekday())
        rows = []
        for week in range(45, 0, -1):
            start = monday - timedelta(weeks=week)
            base = 100 + (45 - week)
            for offset, opening, high, low, close, volume in (
                (0, base, base + 2, base - 1, base + 1, 1000),
                (3, base + 1, base + 4, base, base + 3, 2000),
            ):
                rows.append({"date": (start + timedelta(days=offset)).isoformat(),
                             "open": opening, "high": high, "low": low,
                             "close": close, "volume": volume})
        rows.append({"date": monday.isoformat(), "open": 999, "high": 999,
                     "low": 999, "close": 999, "volume": 999000})
        with patch.object(market_api, "history", return_value=pd.DataFrame(rows)):
            result = market_api.weekly_candles("6274", market="OTC", limit=2)
        self.assertEqual(result["bar_count"], 2)
        self.assertEqual(result["bars"][-1]["last_trading_date"],
                         (monday - timedelta(days=4)).isoformat())
        self.assertEqual(result["bars"][-1]["volume_lots"], 3)
        self.assertEqual(result["bars"][-1]["high"], 148)
        self.assertTrue(result["current_week_excluded"])
        self.assertIsNotNone(result["indicators"]["ma40w"])
        self.assertIsNotNone(result["indicators"]["macd_hist"])
        self.assertLess(result["indicators"]["ma40w"], 999)

    def test_no_history_and_invalid_market(self):
        with self.assertRaises(ValueError):
            market_api.weekly_candles("6274", market="UNKNOWN")
        with patch.object(market_api, "history", return_value=pd.DataFrame()):
            with self.assertRaises(market_api.HTTPException):
                market_api.weekly_candles("6274")


if __name__ == "__main__":
    unittest.main()
