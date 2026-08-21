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

def test_engines_do_not_import_other_engines_or_streamlit() -> None:
    for path in Path("stockpilot/engines").glob("*_engine.py"):
        modules = imported_modules(path)
        assert "streamlit" not in modules
        assert not any(m.startswith("stockpilot.engines") for m in modules)
