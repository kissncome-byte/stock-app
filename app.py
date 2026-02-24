# ✅＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
# ✅【要補上的 code ②】放置位置：請貼在你程式最底部（你現在 try/except 區塊「後面」）
#    （也就是你現有 `except Exception as e:` 這段結束之後，貼上即可）
# ✅目的：新增「自動篩選建議」區塊，不會改動你原本的個股分析流程
# ✅＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

st.divider()
st.header("🔎 v11.8 自動篩選建議（近 5 日平均成交金額 → 深度掃描）")

with st.expander("⚙️ 自動篩選設定（不需要自選清單）", expanded=True):
    scan_market = st.selectbox("市場", ["上市+上櫃", "只上市", "只上櫃"], index=0)
    top_n = st.number_input("第一階段：取近5日平均成交金額 Top N", 50, 800, 200, 50)
    deep_n = st.number_input("第二階段：深度掃描檔數（從Top N再取前M檔）", 20, 400, 120, 20)
    only_tradeable = st.checkbox("只顯示 Tradeable", value=True)
    st.caption("流程：先用近5日平均成交金額快速縮小候選 → 再用你現有的 Liquidity / Space / RR Gate 深掃。")

scan_run = st.button("開始自動篩選", type="primary")

if scan_run:
    with st.spinner("正在建立候選池（近5日平均成交金額）並執行深度掃描..."):
        try:
            api2 = DataLoader()
            if FINMIND_TOKEN:
                api2.login_by_token(FINMIND_TOKEN)

            df_amt = fetch_avg_amount_5d(market=scan_market, need_days=5)
            if df_amt.empty:
                st.error("❌ 無法取得近5日平均成交金額清單（可能休市或資料源暫時失效）。")
                st.stop()

            days_used = int(df_amt["days_used"].iloc[0]) if "days_used" in df_amt.columns and not df_amt.empty else 0
            st.caption(f"近5日平均：本次實際使用交易日數 = {days_used} 天")

            # 第一階段：Top N 候選池
            df_pool = df_amt.sort_values("avg_amount_5d", ascending=False).head(int(top_n)).reset_index(drop=True)
            # 第二階段：深掃前 M 檔
            df_short = df_pool.head(int(deep_n)).reset_index(drop=True)

            # 補產業（拿得到就補，拿不到不影響）
            info2 = api2.taiwan_stock_info()
            ind_map = {}
            name_map = {}
            if info2 is not None and not info2.empty and "stock_id" in info2.columns:
                info2["stock_id"] = info2["stock_id"].astype(str).str.strip()
                if "industry_category" in info2.columns:
                    ind_map = dict(zip(info2["stock_id"], info2["industry_category"]))
                if "stock_name" in info2.columns:
                    name_map = dict(zip(info2["stock_id"], info2["stock_name"]))

            rows = []
            prog = st.progress(0)

            for i, r in enumerate(df_short.itertuples(index=False), start=1):
                sid = str(r.stock_id).strip()

                df_d = scan_get_daily(api2, sid, days=365)
                feat = scan_compute_features(df_d, liq_gate_val=liq_gate)
                if not feat:
                    prog.progress(i / len(df_short))
                    continue

                plans = scan_evaluate_plans(
                    feat,
                    space_atr_mult_val=float(space_atr_mult),
                    space_tick_buffer_val=int(space_tick_buffer),
                )

                # ✅ Tradeable 定義不動你原本規則：Liquidity + Space + RR1（硬門檻）
                brk_tradeable = feat["liq_ok"] and plans["brk"]["space_ok"] and (plans["brk"]["rr1"] >= 2.0)
                pb_tradeable  = feat["liq_ok"] and plans["pb"]["space_ok"]  and (plans["pb"]["rr1"]  >= 3.0)

                if only_tradeable and not (brk_tradeable or pb_tradeable):
                    prog.progress(i / len(df_short))
                    continue

                rows.append({
                    "stock_id": sid,
                    "stock_name": name_map.get(sid, getattr(r, "stock_name", "") or ""),
                    "industry": ind_map.get(sid, ""),
                    "price": feat["price"],
                    "atr_ratio": feat["atr_ratio"],
                    "liq20E": feat["ma20_amount"],
                    "avg_amount_5d": float(getattr(r, "avg_amount_5d", 0.0)),
                    "BRK_tradeable": brk_tradeable,
                    "BRK_RR1": plans["brk"]["rr1"],
                    "BRK_RR2": plans["brk"]["rr2"],
                    "PB_tradeable": pb_tradeable,
                    "PB_RR1": plans["pb"]["rr1"],
                    "PB_RR2": plans["pb"]["rr2"],
                })

                prog.progress(i / len(df_short))

            out = pd.DataFrame(rows)

            if out.empty:
                st.warning("本次自動篩選沒有符合條件的候選。")
            else:
                out = out.sort_values(
                    by=["BRK_tradeable", "PB_tradeable", "BRK_RR2", "PB_RR2", "atr_ratio", "liq20E", "avg_amount_5d"],
                    ascending=False
                )
                st.subheader("✅ 候選清單（可直接拿代號回到上方做個股診斷）")
                st.dataframe(out, use_container_width=True)

                st.caption("提示：你可以把候選 stock_id 複製到上方『股票代號』，按下啟動旗艦診斷做個股深解。")

        except Exception as e:
            st.error(f"自動篩選執行出錯: {e}")
