import numpy as np
import pandas as pd
from analysis.indicators import prepare_indicators
from agents.trend_agent import run_trend_agent
from agents.money_agent import run_money_agent
from agents.risk_agent import run_risk_agent
from agents.compass_agent import run_compass


def sample_df(up=True, n=280):
    base = np.linspace(100, 180, n) if up else np.linspace(180, 100, n)
    wave = np.sin(np.arange(n)/8) * 2
    close = base + wave
    return pd.DataFrame({"date":pd.date_range("2025-01-01", periods=n, freq="B"), "open":close-.3, "high":close+1.2, "low":close-1.2, "close":close, "volume":np.linspace(1_000_000,1_500_000,n)})


def test_uptrend_is_not_bearish():
    df=prepare_indicators(sample_df(True))
    assert run_trend_agent(df).score >= 55


def test_downtrend_is_not_bullish():
    df=prepare_indicators(sample_df(False))
    assert run_trend_agent(df).score < 55


def test_compass_output():
    df=prepare_indicators(sample_df(True))
    t=run_trend_agent(df); m=run_money_agent(df); r,p=run_risk_agent(df)
    d=run_compass("2330","測試",float(df.iloc[-1].close),[t,m,r],p)
    assert 0 <= d.confidence <= 100
    assert d.trade_plan.invalidation_price < d.current_price
