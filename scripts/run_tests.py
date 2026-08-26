"""Dependency-free runner for the repository's plain-assert test functions.

CI may use pytest, but this runner keeps the greenfield project verifiable in
restricted environments where optional development packages are unavailable.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def load_module(path: Path):
    name = f"local_tests.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures: list[str] = []
    passed = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = load_module(path)
        for name, function in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(function):
                continue
            signature = inspect.signature(function)
            unknown = set(signature.parameters) - {"tmp_path"}
            if unknown:
                failures.append(f"{path.name}::{name}: unsupported fixtures {sorted(unknown)}")
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="ppt-eval-test-") as temp:
                    kwargs = {"tmp_path": Path(temp)} if "tmp_path" in signature.parameters else {}
                    function(**kwargs)
                passed += 1
                print(f"PASS {path.name}::{name}")
            except Exception:
                failures.append(f"{path.name}::{name}\n{traceback.format_exc()}")
                print(f"FAIL {path.name}::{name}")
    print(f"\n{passed} passed, {len(failures)} failed")
    if failures:
        print("\n\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
