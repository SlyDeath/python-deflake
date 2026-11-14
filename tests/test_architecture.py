"""The import graph of src/deflake

The deflake command must not import pytest, Django or the code of the project it checks, and the
code that runs inside a test process must stay small and not pull in the command's logic
"""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

import deflake

PACKAGE = Path(__file__).parent.parent / "src" / "deflake"

# Module => what it may import, besides the standard library
ALLOWED: dict[str, list[str]] = {
    "models": [],
    "events": [],
    "tail": [],
    "result_file": ["events"],
    "adapters": ["events", "models"],
    "execution": ["adapters", "events", "models", "result_file", "tail"],
    "repeat": ["adapters", "execution", "models"],
    "bisect": ["adapters", "execution", "models"],
    "investigation": ["adapters", "bisect", "execution", "models", "repeat"],
    "check": ["adapters", "investigation", "models", "reports"],
    "reports": ["models"],
    "cli": ["adapters", "check", "execution", "models", "reports"],
    "__main__": ["cli"],
    "leaks": [],
    "child": ["events", "leaks"],
    "pytest_plugin": ["child", "django", "leaks", "pytest"],
    "unittest_runner": ["child", "leaks"],
    "django": ["child", "django", "leaks", "unittest_runner"],
}


def module_of(path: Path) -> str:
    """The top-level module a file of the package belongs to"""
    relative = path.relative_to(PACKAGE)

    return relative.parts[0].removesuffix(".py")


def imports_of(path: Path) -> set[str]:
    """Top-level names a file imports, deflake's own modules as deflake.NAME"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = [*path.relative_to(PACKAGE.parent).parent.parts]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            target = [*base, *(node.module.split(".") if node.module else [])]
            if node.module is None or target == ["deflake"]:
                found.update(".".join([*target, alias.name]) for alias in node.names)
            else:
                found.add(".".join(target))

    names = set()
    for name in found:
        parts = name.split(".")
        if parts[0] == "deflake" and len(parts) > 1 and not parts[1].startswith("__"):
            names.add(".".join(parts[:2]))
        else:
            names.add(parts[0])

    return names


def violations() -> list[str]:
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        module = module_of(path)
        if module == "__init__":
            allowed: list[str] = []
        elif module not in ALLOWED:
            problems.append(f"{path.relative_to(PACKAGE)}: module {module} is not in the graph")
            continue
        else:
            allowed = ALLOWED[module]
        for name in sorted(imports_of(path)):
            own = name.startswith("deflake.")
            name = name.removeprefix("deflake.")
            if name in (module, "deflake") or (not own and name in sys.stdlib_module_names):
                continue
            if name not in allowed:
                problems.append(f"{path.relative_to(PACKAGE)}: {module} imports {name}")

    return problems


def test_modules_import_only_what_the_graph_allows() -> None:
    assert violations() == []


def test_the_graph_catches_a_forbidden_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "deflake"
    package.mkdir()
    (package / "reports.py").write_text("from deflake import execution\nimport pytest\n")
    (package / "child.py").write_text("from . import leaks\nfrom .bisect import Bisect\n")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", package)
    assert violations() == [
        "child.py: child imports bisect",
        "reports.py: reports imports execution",
        "reports.py: reports imports pytest",
    ]


def test_the_package_itself_imports_nothing() -> None:
    assert imports_of(PACKAGE / "__init__.py") <= set(sys.stdlib_module_names)


def test_the_version_is_the_one_of_the_package() -> None:
    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert deflake.__version__ == project["project"]["version"]
