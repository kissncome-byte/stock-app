# 免費起漲候選掃描

新增 MCP action：`scan_prebreakout_candidates`。

```json
{"market":"ALL","limit":10}
```

此 action 不需要 Fugle 付費市場快照。它使用 Yahoo Finance 免費的台灣「成交活躍」與「漲幅」候選排行，合併後只對最多 80 檔查詢已完成日 K，再篩選均線改善、近 20 日區間收斂、接近前高且當日未急漲的股票。結果附上來源、資料日期、觀察壓力／支撐與限制；180 秒內重複查詢使用快取。

結果是候選觀察名單，並非全市場逐檔掃描，也不是買進訊號。安靜整理、尚未進入成交活躍或漲幅排行的股票可能漏掉。Yahoo 行情可能延遲或限流；候選股仍須使用 `get_stock_quote` 和 `get_intraday_candles` 核對 Fugle 即時報價與 1／5 分 K。Yahoo 免費篩選端點沒有穩定的公開 API 服務承諾，若其回應格式或存取政策變更，action 會回報暫時無法取得。

## 部署後驗證

1. 在 ChatGPT／Codex 的「台股查詢」重新整理 action 清單。
2. 呼叫 `scan_prebreakout_candidates`，確認回傳 `source`、`candidate_pool_count`、`data_limitations` 與候選列表。
3. 挑一檔候選，用 `get_stock_quote` 與 `get_intraday_candles` 驗證即時行情；未完成的 1／5 分 K 不可視為突破確認。
