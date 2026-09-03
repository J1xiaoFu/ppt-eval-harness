"""Dependency-free runner for the repository's plain-assert test functions.

CI may use pytest, but this runner keeps the greenfield project verifiable in
restricted environments where optional development packages are unavailable.
"""

from __future__ import annotations

import importlib.util
import inspect
import math
import re
import sys
import tempfile
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Sequence
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


class _Raises:
    def __init__(
        self,
        expected: type[BaseException] | tuple[type[BaseException], ...],
        *,
        match: str | None = None,
    ) -> None:
        self.expected = expected
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> _Raises:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback_value: object,
    ) -> bool:
        del traceback_value
        if exc_type is None or exc is None:
            raise AssertionError(f"did not raise {self.expected!r}")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match is not None and re.search(self.match, str(exc)) is None:
            raise AssertionError(
                f"exception message {str(exc)!r} does not match {self.match!r}"
            )
        self.value = exc
        return True


class _Approx:
    def __init__(self, expected: int | float) -> None:
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise TypeError("dependency-free pytest.approx supports only scalar numbers")
        self.expected = float(expected)

    def __eq__(self, actual: object) -> bool:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        value = float(actual)
        if value == self.expected:
            return True
        if math.isnan(value) or math.isnan(self.expected):
            return False
        if math.isinf(value) or math.isinf(self.expected):
            return False
        tolerance = max(1e-12, 1e-6 * abs(self.expected))
        return abs(value - self.expected) <= tolerance

    def __repr__(self) -> str:
        return f"approx({self.expected!r})"


def _importorskip(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name or name.startswith(f"{exc.name}."):
            raise SkipTest(f"optional dependency {name!r} is unavailable") from exc
        raise


def _install_pytest_facade() -> None:
    class _Mark:
        @staticmethod
        def parametrize(
            argnames: str | Sequence[str],
            argvalues: Sequence[Any],
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            names = (
                tuple(item.strip() for item in argnames.split(",") if item.strip())
                if isinstance(argnames, str)
                else tuple(str(item).strip() for item in argnames)
            )
            if not names:
                raise ValueError("parametrize requires at least one argument name")

            def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
                setattr(
                    function,
                    "__ppt_eval_parametrize__",
                    (names, tuple(argvalues)),
                )
                return function

            return decorate

    facade = ModuleType("pytest")
    facade.approx = _Approx  # type: ignore[attr-defined]
    facade.importorskip = _importorskip  # type: ignore[attr-defined]
    facade.mark = _Mark()  # type: ignore[attr-defined]
    facade.raises = _Raises  # type: ignore[attr-defined]
    sys.modules["pytest"] = facade


def load_module(path: Path) -> ModuleType:
    name = f"local_tests.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    _install_pytest_facade()
    failures: list[str] = []
    passed = 0
    skipped = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            module = load_module(path)
        except SkipTest as exc:
            skipped += 1
            print(f"SKIP {path.name}: {exc}")
            continue
        except Exception:
            failures.append(f"{path.name}::<module>\n{traceback.format_exc()}")
            print(f"FAIL {path.name}::<module>")
            continue
        for name, function in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(function):
                continue
            if (
                inspect.iscoroutinefunction(function)
                or inspect.isgeneratorfunction(function)
                or inspect.isasyncgenfunction(function)
            ):
                failures.append(
                    f"{path.name}::{name}: async/generator tests are unsupported"
                )
                continue
            signature = inspect.signature(function)
            parametrized = getattr(function, "__ppt_eval_parametrize__", None)
            parameter_names: tuple[str, ...] = ()
            parameter_values: tuple[Any, ...] = (None,)
            if parametrized is not None:
                parameter_names, parameter_values = parametrized
            unknown = set(signature.parameters) - {"tmp_path", *parameter_names}
            if unknown:
                failures.append(f"{path.name}::{name}: unsupported fixtures {sorted(unknown)}")
                continue
            for parameter_index, raw_values in enumerate(parameter_values):
                case_name = (
                    name
                    if parametrized is None
                    else f"{name}[{parameter_index}]"
                )
                try:
                    with tempfile.TemporaryDirectory(prefix="ppt-eval-test-") as temp:
                        kwargs = (
                            {"tmp_path": Path(temp)}
                            if "tmp_path" in signature.parameters
                            else {}
                        )
                        if parametrized is not None:
                            values = (
                                tuple(raw_values)
                                if len(parameter_names) > 1
                                and isinstance(raw_values, Sequence)
                                and not isinstance(raw_values, (str, bytes))
                                else (raw_values,)
                            )
                            if len(values) != len(parameter_names):
                                raise ValueError(
                                    "parametrize value count does not match argument names"
                                )
                            kwargs.update(zip(parameter_names, values, strict=True))
                        function(**kwargs)
                    passed += 1
                    print(f"PASS {path.name}::{case_name}")
                except SkipTest as exc:
                    skipped += 1
                    print(f"SKIP {path.name}::{case_name}: {exc}")
                except Exception:
                    failures.append(
                        f"{path.name}::{case_name}\n{traceback.format_exc()}"
                    )
                    print(f"FAIL {path.name}::{case_name}")
    print(f"\n{passed} passed, {skipped} skipped, {len(failures)} failed")
    if failures:
        print("\n\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
