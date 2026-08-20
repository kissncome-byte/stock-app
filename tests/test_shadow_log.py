from stockpilot.services.shadow_integration import ShadowIntegration
from stockpilot.services.shadow_log import ShadowLogWriter
from test_shadow_integration import payload

def test_shadow_log_jsonl(tmp_path):
    comparison = ShadowIntegration().run(
        payload(),
        is_holding=False,
        cost=0,
        legacy_action="等待",
    )
    path = tmp_path / "shadow.jsonl"
    ShadowLogWriter().append(comparison, path)
    text = path.read_text(encoding="utf-8")
    assert '"symbol": "3037"' in text
    assert '"core_strategy"' in text
