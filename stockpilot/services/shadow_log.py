from __future__ import annotations
from datetime import datetime
from pathlib import Path
import json

from .shadow_integration import ShadowComparison


class ShadowLogWriter:
    def append(self, comparison: ShadowComparison, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": comparison.symbol,
            "legacy_action": comparison.legacy_action,
            "core_strategy": comparison.core_strategy.value,
            "same_direction": comparison.same_direction,
            "differences": list(comparison.differences),
            "trend_state": comparison.snapshot.trend_state.value,
            "market_state": comparison.snapshot.market_state.value,
            "current_price": comparison.snapshot.current_price,
        }
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
