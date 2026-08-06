# StockPilot 4.0 — Sprint 1 Models

本套件只包含純資料模型，不含任何技術分析、評分、價格計算或買賣決策。

## 上傳位置

將 ZIP 解壓後，把內容合併到 GitHub `develop` branch：

- `stockpilot/__init__.py`
- `stockpilot/models/*`
- `tests/test_models_import.py`

不要修改或覆蓋目前的 `app.py`。

## 驗證

安裝 pytest 後執行：

```bash
python -m pytest tests/test_models_import.py -q
```

Sprint 1 驗收重點：

- Models 不 import Engine。
- 沒有計算或決策邏輯。
- 固定六種 Strategy。
- DecisionSnapshot 作為未來 UI 唯一資料來源。
