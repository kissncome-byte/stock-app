from stockpilot.engines import DecisionEngine
from stockpilot.models import *

def lv(v): return PriceLevel(v,'test',v is not None)
def prices(rr=2.0, structural=85.0): return PriceLevels(lv(95),lv(102),lv(105),lv(92),lv(structural),lv(116),lv(125),16,8,rr)
def market(s): return MarketResult(s,'TAIEX',(),True)
def trend(s): return TrendResult(s,s,5,(),(s,))
def chip(s): return ChipResult(s,(),True)
def volume(s): return VolumeResult(s,'test',(),True)
def inp(holding,cost=None,current=100,ms=MarketState.NEUTRAL,ts=TrendState.UPTREND_PULLBACK,cs=ChipState.NEUTRAL,vs=VolumeState.NEUTRAL,rr=2.0,structural=85):
    return DecisionEngineInput(market(ms),trend(ts),chip(cs),volume(vs),prices(rr,structural),PortfolioContext(holding,cost,current))

def test_build_strong_setup(): assert DecisionEngine().evaluate(inp(False,0,100,MarketState.RISK_ON,TrendState.STRONG_UPTREND,ChipState.SUPPORT,VolumeState.CONFIRM)).strategy is Strategy.BUILD
def test_wait_riskoff_nonholder(): assert DecisionEngine().evaluate(inp(False,0,100,MarketState.RISK_OFF)).strategy is Strategy.WAIT
def test_losing_downtrend_reduces():
    r=DecisionEngine().evaluate(inp(True,120,100,ts=TrendState.DOWNTREND)); assert r.strategy is Strategy.REDUCE and '不等待回本' in r.action.detail
def test_profitable_downtrend_also_reduces(): assert DecisionEngine().evaluate(inp(True,60,100,ts=TrendState.DOWNTREND)).strategy is Strategy.REDUCE
def test_cost_does_not_change_same_trend_decision(): assert DecisionEngine().evaluate(inp(True,130)).strategy is DecisionEngine().evaluate(inp(True,70)).strategy
def test_structural_break_forces_exit(): assert DecisionEngine().evaluate(inp(True,100,80,structural=85)).strategy is Strategy.EXIT
def test_chip_warning_not_exit(): assert DecisionEngine().evaluate(inp(True,90,cs=ChipState.OPPOSE)).strategy is Strategy.HOLD_NO_ADD
def test_poor_rr_reduces(): assert DecisionEngine().evaluate(inp(True,105,rr=0.7)).strategy is Strategy.REDUCE
