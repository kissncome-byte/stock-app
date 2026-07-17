from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd
import requests

class DataError(RuntimeError):
    pass


def _normalize_stock_id(stock_id: str) -> str:
    value = str(stock_id).strip()
    if not value.isdigit() or len(value) not in (4, 5, 6):
        raise ValueError("股票代碼格式不正確")
    return value


def fetch_twse_month(stock_id: str, date_yyyymmdd: str) -> pd.DataFrame:
    stock_id = _normalize_stock_id(stock_id)
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    response = requests.get(url, params={"response": "json", "date": date_yyyymmdd, "stockNo": stock_id}, timeout=12)
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "OK" or not payload.get("data"):
        return pd.DataFrame()
    rows = []
    for row in payload["data"]:
        try:
            roc_y, month, day = [int(x) for x in row[0].split("/")]
            date = pd.Timestamp(roc_y + 1911, month, day)
            rows.append({
                "date": date,
                "volume": float(str(row[1]).replace(",", "")),
                "open": float(str(row[3]).replace(",", "")),
                "high": float(str(row[4]).replace(",", "")),
                "low": float(str(row[5]).replace(",", "")),
                "close": float(str(row[6]).replace(",", "")),
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows)


def fetch_daily_prices(stock_id: str, months: int = 18) -> pd.DataFrame:
    frames = []
    anchor = datetime.now().replace(day=1)
    for offset in range(months):
        month = anchor - pd.DateOffset(months=offset)
        try:
            frame = fetch_twse_month(stock_id, month.strftime("%Y%m%d"))
            if not frame.empty:
                frames.append(frame)
        except requests.RequestException:
            continue
    if not frames:
        raise DataError("無法從證交所取得日線資料；上櫃股票或網路受限時，可改用 CSV 匯入。")
    return pd.concat(frames, ignore_index=True).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def read_price_csv(uploaded_file) -> pd.DataFrame:
    df = pd.read_csv(uploaded_file)
    aliases = {
        "日期":"date", "開盤價":"open", "最高價":"high", "最低價":"low", "收盤價":"close",
        "成交量":"volume", "vol":"volume", "Date":"date", "Open":"open", "High":"high",
        "Low":"low", "Close":"close", "Volume":"volume"
    }
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df
