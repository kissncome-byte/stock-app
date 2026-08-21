from pathlib import Path
import ast


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_orchestrator_does_not_import_streamlit_or_ui():
    path = Path("stockpilot/services/decision_orchestrator.py")
    modules = imported_modules(path)
    assert "streamlit" not in modules
    assert not any(module.startswith("stockpilot.ui") for module in modules)
