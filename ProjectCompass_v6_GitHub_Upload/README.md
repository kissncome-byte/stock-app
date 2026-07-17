# Project Compass v6.0

> 不是幫使用者預測未來，而是幫使用者在不確定的市場裡，做出更好的決策。

## 本版已完成

- 新專案模組化架構
- Trend / Money / Risk / Compass 四個 Agent
- AI 綜合結論與今日重點
- 未持有／已持有雙模式
- 最佳布局區、突破方案、趨勢失效價、兩段目標區
- 風險報酬比
- AI 自我檢查：支持理由與反對理由
- 每個模組均可展開查看數據依據
- 證交所日線自動下載及 CSV 匯入備援
- 基礎單元測試

## 執行方式

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

## CSV 格式

必須包含：

`date, open, high, low, close, volume`

也支援常見中文欄名：日期、開盤價、最高價、最低價、收盤價、成交量。

## 目前限制

- 自動下載目前優先支援上市股票；上櫃股票建議先使用 CSV。
- 法人與基本面 Agent 尚未搬入，本版 Money Agent 先以價量資金行為為核心。
- 目標區與失效價是依目前資料推導的交易參考，不是價格預測。
