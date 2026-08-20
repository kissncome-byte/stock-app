from stockpilot.adapters import LegacyDictAdapter

def test_missing_values_remain_none():
    p={"symbol":"3274","listing_market":"上櫃","current_price":"210.5",
       "quote_valid":True,"stock_bars":[{"date":"2026-08-14","open":205,
       "high":212,"low":203,"close":210.5,"volume":None}],
       "institutional":[{"date":"2026-08-14","foreign":None,"trust":100,"dealer":0}],
       "margin":[{"date":"2026-08-14","margin_balance":None}]}
    b=LegacyDictAdapter().from_dict(p)
    assert b.stock_bars[0].volume is None
    assert b.institutional[0].foreign is None
    assert b.margin[0].margin_balance is None
