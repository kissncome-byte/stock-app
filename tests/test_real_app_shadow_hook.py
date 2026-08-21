from pathlib import Path
import ast


def test_real_app_contains_shadow_hook_and_compiles_ast():
    path = Path("app.py")
    text = path.read_text(encoding="utf-8")
    ast.parse(text)

    assert "ENABLE_STOCKPILOT4_SHADOW = True" in text
    assert "ShadowIntegration().run(" in text
    assert 'st.session_state["_stockpilot4_shadow"]' in text
    assert "StockPilot 4.0 Shadow 比對" in text


def test_shadow_hook_is_after_legacy_strategy_build():
    text = Path("app.py").read_text(encoding="utf-8")
    legacy = text.index("strategy_state = build_strategy_state_machine")
    shadow = text.index("ShadowIntegration().run(")
    assert shadow > legacy
