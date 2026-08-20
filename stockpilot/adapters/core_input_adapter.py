from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from stockpilot.models import (
    ChipEngineInput, DailyBar, InstitutionalFlow, ListingMarket, MarginRecord,
    MarketEngineInput, OrchestratorInput, PortfolioContext, PriceEngineInput,
    RawDailyBar, RawInstitutionalRecord, RawMarginRecord, RawMarketBundle,
    TrendEngineInput, VolumeEngineInput,
)

class CoreInputAdapterError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class DataQualityReport:
    score: float
    warnings: tuple[str, ...]
    missing: tuple[str, ...]

class CoreInputAdapter:
    TAIPEI_TZ = ZoneInfo("Asia/Taipei")

    def build(self, bundle: RawMarketBundle, *, is_holding: bool,
              cost: float | None, schema_version: str = "4.0.0") -> OrchestratorInput:
        listing = self._listing_market(bundle.listing_market)
        expected_index = "TAIEX" if listing is ListingMarket.LISTED else "TPEx"
        index_symbol = bundle.index_symbol or expected_index

        stock_bars = self._convert_bars(bundle.stock_bars, allow_empty=False)
        index_bars = self._convert_bars(bundle.index_bars, allow_empty=True)
        institutional = self._convert_institutional(bundle.institutional)
        margin = self._convert_margin(bundle.margin)

        current_price = self._current_price(bundle)
        as_of = self._quote_time(bundle)
        quality = self._quality_report(
            bundle, stock_bars, index_bars, institutional, margin, expected_index
        )

        chip_dates = tuple(bar.trading_date for bar in stock_bars[-25:])
        chip_closes = tuple(bar.close for bar in stock_bars[-25:])

        return OrchestratorInput(
            schema_version=schema_version,
            symbol=bundle.symbol,
            company_name=bundle.company_name,
            as_of=as_of,
            price_input=PriceEngineInput(
                current_price=current_price,
                atr=self._positive_or_none(bundle.atr),
                ma20=self._positive_or_none(bundle.ma20),
                ma60=self._positive_or_none(bundle.ma60),
                recent_resistance=self._positive_or_none(bundle.recent_resistance),
                breakout_level=self._positive_or_none(bundle.breakout_level),
                recent_swing_low=self._positive_or_none(bundle.recent_swing_low),
                platform_floor=self._positive_or_none(bundle.platform_floor),
                original_target=self._positive_or_none(bundle.original_target),
                fair_value=self._positive_or_none(bundle.fair_value),
                tick_size=self._positive_tick(bundle.tick_size),
            ),
            trend_input=TrendEngineInput(bars=stock_bars),
            market_input=MarketEngineInput(
                listing_market=listing,
                index_bars=index_bars,
                reference_index=index_symbol,
                expected_reference_index=expected_index,
                international_risk_flag=bundle.international_risk_flag,
                lookback_days=120,
            ),
            chip_input=ChipEngineInput(
                institutional_flows=institutional,
                margin_records=margin,
                closes=chip_closes,
                dates=chip_dates,
            ),
            volume_input=VolumeEngineInput(
                bars=stock_bars[-60:],
                lookback_days=min(60, len(stock_bars)),
            ),
            portfolio=PortfolioContext(
                is_holding=is_holding,
                cost=cost,
                current_price=current_price,
            ),
            data_quality_score=quality.score,
            data_warnings=quality.warnings,
        )

    def _listing_market(self, value: str) -> ListingMarket:
        v = value.strip().lower()
        if v in {"listed","twse","上市"}:
            return ListingMarket.LISTED
        if v in {"otc","tpex","上櫃"}:
            return ListingMarket.OTC
        raise CoreInputAdapterError(f"unsupported listing market: {value}")

    def _current_price(self, bundle: RawMarketBundle) -> float:
        price = bundle.quote.current_price if bundle.quote.valid else None
        if price is None and bundle.stock_bars:
            price = bundle.stock_bars[-1].close
        if price is None or price <= 0 or not math.isfinite(float(price)):
            raise CoreInputAdapterError("current price is unavailable or invalid")
        return float(price)

    def _quote_time(self, bundle: RawMarketBundle) -> datetime:
        ts = bundle.quote.timestamp
        if ts is None:
            return datetime.now(self.TAIPEI_TZ)
        if ts.tzinfo is None:
            return ts.replace(tzinfo=self.TAIPEI_TZ)
        return ts.astimezone(self.TAIPEI_TZ)

    def _convert_bars(self, raw_bars, *, allow_empty):
        out=[]
        for raw in sorted(raw_bars, key=lambda x:x.trading_date):
            vals=(raw.open,raw.high,raw.low,raw.close)
            if any(v is None for v in vals):
                continue
            o,h,l,c=(float(v) for v in vals)
            if any(v<=0 or not math.isfinite(v) for v in (o,h,l,c)):
                continue
            vol = None if raw.volume is None else float(raw.volume)
            if vol is not None and (vol<=0 or not math.isfinite(vol)):
                vol=None
            out.append(DailyBar(raw.trading_date,o,h,l,c,vol))
        if not out and not allow_empty:
            raise CoreInputAdapterError("daily bars unavailable")
        return tuple(out)

    def _convert_institutional(self, records):
        return tuple(
            InstitutionalFlow(
                r.trading_date,
                self._finite_or_none(r.foreign),
                self._finite_or_none(r.trust),
                self._finite_or_none(r.dealer),
            )
            for r in sorted(records,key=lambda x:x.trading_date)
        )

    def _convert_margin(self, records):
        return tuple(
            MarginRecord(
                r.trading_date,
                self._nonnegative_or_none(r.margin_balance),
                self._nonnegative_or_none(r.short_balance),
            )
            for r in sorted(records,key=lambda x:x.trading_date)
        )

    def _quality_report(self, bundle, stock_bars, index_bars, institutional, margin, expected_index):
        missing=[]
        warnings=list(bundle.data_warnings)
        if len(stock_bars)<65: missing.append("stock_history_65d")
        if len(index_bars)<65: missing.append("market_history_65d")
        if len(institutional)<20: missing.append("institutional_20d")
        if len(margin)<21: missing.append("margin_21d")
        if bundle.index_symbol not in (None, expected_index):
            warnings.append(f"市場指數錯配：預期 {expected_index}，收到 {bundle.index_symbol}")
        if not bundle.quote.valid:
            warnings.append("即時報價無效，現價改用最新完成日線收盤。")
        penalties={"stock_history_65d":30,"market_history_65d":20,"institutional_20d":15,"margin_21d":10}
        score=max(0.0,100.0-sum(penalties[m] for m in missing))
        return DataQualityReport(score, tuple(dict.fromkeys(warnings)), tuple(missing))

    def _positive_or_none(self, value):
        if value is None: return None
        value=float(value)
        return value if value>0 and math.isfinite(value) else None

    def _positive_tick(self, value):
        value=float(value)
        if value<=0 or not math.isfinite(value):
            raise CoreInputAdapterError("tick_size must be positive")
        return value

    def _finite_or_none(self, value):
        if value is None: return None
        value=float(value)
        return value if math.isfinite(value) else None

    def _nonnegative_or_none(self, value):
        value=self._finite_or_none(value)
        return value if value is not None and value>=0 else None
