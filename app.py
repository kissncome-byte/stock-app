# 1. 撈取並計算籌碼因子
inst_df = get_inst_df(stock_id, days=10)
# 假設計算近3日三大法人買賣超合計張數
inst_3d_sum = inst_df.tail(3)["buy_or_sell_sheets_total"].sum() if not inst_df.empty else 0 

# 2. 撈取並計算營收基本面
rev_df = get_rev_df(stock_id, days=120)
# 撈最新一個月的營收年增率
latest_yoy = rev_df.iloc[-1]["revenue_year_growth_rate"] if not rev_df.empty else 0

# 3. 開始進行多因子白話文融合判斷
if plus_di > minus_di and adx_now >= 20:
    if inst_3d_sum > 0 and latest_yoy > 20:
        tech_conclusion_long = "🚀 【黃金進攻訊號】技術面強勢多頭，且法人狂買、營收大增..."
        tech_conclusion_short = "🚀 完美多頭"
    elif inst_3d_sum < 0:
        tech_conclusion_long = "⚠️ 【小心假突破！主力在出貨】雖然技術面強勢，但法人這幾天瘋狂倒貨..."
        tech_conclusion_short = "⚠️ 假突破嫌疑"
    else:
        tech_conclusion_long = "⚖️ 技術面多頭成形，但籌碼與基本面普普，屬於單兵技術性進攻..."
        tech_conclusion_short = "🚀 多頭成形"
# ...後面依此類推
