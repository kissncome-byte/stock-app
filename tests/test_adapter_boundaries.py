from pathlib import Path
import ast

def mods(path):
    tree=ast.parse(path.read_text(encoding="utf-8"))
    out=set()
    for n in ast.walk(tree):
        if isinstance(n,ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n,ast.ImportFrom) and n.module:
            out.add(n.module)
    return out

def test_adapters_do_not_import_streamlit_or_engines():
    for p in Path("stockpilot/adapters").glob("*.py"):
        m=mods(p)
        assert "streamlit" not in m
        assert not any(x.startswith("stockpilot.engines") for x in m)
