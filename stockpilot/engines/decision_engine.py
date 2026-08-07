from __future__ import annotations
from stockpilot.models import Action, ChipState, DecisionEngineInput, DecisionResult, EvidenceDirection, MarketState, Strategy, TrendState, VolumeState

class DecisionEngineError(ValueError): pass

class DecisionEngine:
    def evaluate(self, d: DecisionEngineInput) -> DecisionResult:
        self._validate(d)
        ev=tuple(d.market.evidence)+tuple(d.trend.evidence)+tuple(d.chip.evidence)+tuple(d.volume.evidence)
        sup=tuple(x for x in ev if x.direction is EvidenceDirection.SUPPORT); opp=tuple(x for x in ev if x.direction is EvidenceDirection.OPPOSE); neu=tuple(x for x in ev if x.direction not in {EvidenceDirection.SUPPORT,EvidenceDirection.OPPOSE})
        p=d.portfolio; px=p.current_price; structural=d.prices.structural_exit.value; moving=d.prices.moving_protection.value; confirm=d.prices.confirmation.value
        if p.is_holding and px is not None and structural is not None and px <= structural:
            return self._r(Strategy.EXIT,'退出','已跌破結構退出價，原持有理由失效。',sup,opp,neu,None,None,'已觸發結構退出條件。','結構風險優先。')
        if d.market.state is MarketState.RISK_OFF_HARD:
            if not p.is_holding: return self._r(Strategy.WAIT,'等待','大盤高風險，暫不建立新部位。',sup,opp,neu,self._t(confirm,'站回'),None,'大盤風險閘門尚未解除。','大盤硬性風險閘門。')
            s=Strategy.EXIT if d.trend.formal_state is TrendState.DOWNTREND else Strategy.REDUCE
            return self._r(s,'退出' if s is Strategy.EXIT else '減碼','大盤高風險，優先降低曝險。',sup,opp,neu,self._t(moving,'跌破'),self._t(structural,'跌破'),'大盤風險解除後再評估。','大盤硬性風險閘門。')
        if not p.is_holding:
            if d.market.state in {MarketState.RISK_OFF,MarketState.UNAVAILABLE} or d.trend.formal_state in {TrendState.DOWNTREND,TrendState.BEAR_RALLY}:
                return self._r(Strategy.WAIT,'等待','市場或正式趨勢尚未支持建立部位。',sup,opp,neu,self._t(confirm,'收盤站上'),None,'未達建立條件。','先等待市場與趨勢改善。')
            ok=(d.trend.formal_state in {TrendState.STRONG_UPTREND,TrendState.UPTREND_PULLBACK} and d.chip.state in {ChipState.SUPPORT,ChipState.NEUTRAL} and d.volume.state in {VolumeState.CONFIRM,VolumeState.NEUTRAL} and self._entry_ok(d))
            return self._r(Strategy.BUILD if ok else Strategy.WAIT,'建立' if ok else '等待','條件允許建立部位。' if ok else '證據尚不足以建立部位。',sup,opp,neu,self._t(confirm,'確認'),self._t(moving,'跌破'),'大盤轉弱或正式趨勢降級。','多項獨立證據一致。' if ok else '至少一項條件未通過。')
        trend=d.trend.formal_state
        if trend is TrendState.DOWNTREND:
            return self._r(Strategy.REDUCE,'減碼','正式趨勢已轉弱，不等待回本才處理。',sup,opp,neu,self._t(moving,'跌破'),self._t(structural,'跌破'),'跌破結構價則退出。','成本不改變空頭趨勢。')
        if trend is TrendState.BEAR_RALLY:
            return self._r(Strategy.REDUCE,'減碼','目前屬空頭反彈，反彈不等於趨勢恢復。',sup,opp,neu,self._t(confirm,'反彈站不上'),self._t(structural,'跌破'),'正式趨勢升級後才解除。','弱勢結構優先。')
        if d.market.state is MarketState.RISK_OFF or d.chip.state is ChipState.OPPOSE or d.volume.state is VolumeState.WARNING:
            return self._r(Strategy.HOLD_NO_ADD,'續抱不加碼','正式趨勢尚在，但已有市場／籌碼／量價警訊。',sup,opp,neu,self._t(moving,'跌破'),self._t(confirm,'重新站穩'),'正式趨勢降級或結構失效則減碼／退出。','單一警訊先降低進攻性，不直接翻空。')
        if not self._edge_ok(d):
            return self._r(Strategy.REDUCE,'減碼','趨勢未完全破壞，但剩餘報酬相對風險不足。',sup,opp,neu,self._t(confirm,'反彈'),self._t(moving,'跌破'),'重新建立足夠風險報酬後再評估。','持有價值不足，不因解套心理延後。')
        return self._r(Strategy.HOLD,'續抱','正式趨勢、風險與報酬仍支持持有。',sup,opp,neu,self._t(moving,'跌破'),self._t(confirm,'站穩'),'正式趨勢降級或結構失效。','目前沒有足夠反證推翻策略。')
    def _entry_ok(self,d):
        c=d.portfolio.current_price; lo=d.prices.entry_zone_low.value; hi=d.prices.entry_zone_high.value; rr=d.prices.reward_risk_ratio
        return c is not None and lo is not None and hi is not None and lo<=c<=hi and (rr is None or rr>=1.5)
    def _edge_ok(self,d):
        rr=d.prices.reward_risk_ratio; rew=d.prices.reward_pct; risk=d.prices.risk_pct
        return True if rr is None or rew is None or risk is None else not (rr<1.0 or (rew<3 and risk>rew))
    def _t(self,p,v): return None if p is None else f'{v} {p:.2f} 元'
    def _r(self,s,title,detail,sup,opp,neu,pt,st,inv,rat):
        total=len(sup)+len(opp); agr=(max(len(sup),len(opp))/total) if total else 0.0
        return DecisionResult(s,Action(title,detail),sup,opp,neu,pt,st,inv,rat,agr)
    def _validate(self,d):
        p=d.portfolio
        if p.current_price is not None and p.current_price<=0: raise DecisionEngineError('current_price must be positive')
        if p.cost is not None and p.cost<0: raise DecisionEngineError('cost cannot be negative')
        if not p.is_holding and p.cost not in (None,0): raise DecisionEngineError('non-holder cannot have holding cost')
