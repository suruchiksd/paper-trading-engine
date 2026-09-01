import ast
from pathlib import Path


def test_production_code_has_no_kite_order_api_invocations():
    """Static guard; it supplements review but cannot prove behavior of dependencies."""
    root = Path(__file__).parents[1]
    forbidden = {"place_order", "modify_order", "exit_order", "place_gtt", "modify_gtt", "delete_gtt"}
    violations = []
    for path in list((root / "src").rglob("*.py")) + list((root / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden:
                violations.append(f"{path}:{node.lineno}:{node.func.attr}")
    assert violations == []
