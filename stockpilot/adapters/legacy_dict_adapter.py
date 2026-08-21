from __future__ import annotations
from datetime import date, datetime
from stockpilot.models import RawDailyBar, RawInstitutionalRecord, RawMarginRecord, RawMarketBundle, RawQuote

class LegacyDictAdapter:
    def from_dict(self, payload):
        return RawMarketBundle(
            symbol=str(payload.get("symbol","")),
            company_name=payload.get("company_name"),
            listing_market=str(payload.get("listing_market","")),
            quote=RawQuote(
                symbol=str(payload.get("symbol","")),
                current_price=self._f(payload.get("current_price")),
                timestamp=self._dt(payload.get("quote_timestamp")),
                source=str(payload.get("quote_source","legacy")),
                valid=bool(payload.get("quote_valid",False)),
            ),
            stock_bars=self._bars(payload.get("stock_bars",[])),
            index_symbol=payload.get("index_symbol"),
            index_bars=self._bars(payload.get("index_bars",[])),
            institutional=self._inst(payload.get("institutional",[])),
            margin=self._margin(payload.get("margin",[])),
            atr=self._f(payload.get("atr")),
            ma20=self._f(payload.get("ma20")),
            ma60=self._f(payload.get("ma60")),
            recent_resistance=self._f(payload.get("recent_resistance")),
            breakout_level=self._f(payload.get("breakout_level")),
            recent_swing_low=self._f(payload.get("recent_swing_low")),
            platform_floor=self._f(payload.get("platform_floor")),
            original_target=self._f(payload.get("original_target")),
            fair_value=self._f(payload.get("fair_value")),
            tick_size=self._f(payload.get("tick_size")) or 0.5,
            international_risk_flag=bool(payload.get("international_risk_flag",False)),
            data_warnings=tuple(payload.get("data_warnings",())),
        )

    def _bars(self, rows):
        out=[]
        for r in rows:
            d=self._date(r.get("date"))
            if d is not None:
                out.append(RawDailyBar(d,self._f(r.get("open")),self._f(r.get("high")),
                    self._f(r.get("low")),self._f(r.get("close")),self._f(r.get("volume"))))
        return tuple(out)

    def _inst(self, rows):
        out=[]
        for r in rows:
            d=self._date(r.get("date"))
            if d is not None:
                out.append(RawInstitutionalRecord(d,self._f(r.get("foreign")),
                    self._f(r.get("trust")),self._f(r.get("dealer"))))
        return tuple(out)

    def _margin(self, rows):
        out=[]
        for r in rows:
            d=self._date(r.get("date"))
            if d is not None:
                out.append(RawMarginRecord(d,self._f(r.get("margin_balance")),
                    self._f(r.get("short_balance"))))
        return tuple(out)

    def _f(self,v):
        try: return None if v is None else float(v)
        except (TypeError,ValueError): return None

    def _date(self,v):
        if isinstance(v,datetime): return v.date()
        if isinstance(v,date): return v
        if isinstance(v,str):
            try: return date.fromisoformat(v[:10])
            except ValueError: return None
        return None

    def _dt(self,v):
        if isinstance(v,datetime): return v
        if isinstance(v,str):
            try: return datetime.fromisoformat(v)
            except ValueError: return None
        return None
