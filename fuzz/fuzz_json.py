"""Fuzz JSON decoding: with orjson installed the result must equal json's.

orjson reads some input differently (it rounds huge integers, refuses lone
surrogates, NaN and very deep nesting); the library must hand those bodies
to the json module so callers always get the same value.
"""

import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from frappex_client import _protocol


def same(a: Any, b: Any) -> bool:
    """Compare decoded values exactly and iteratively, NaN equal to NaN."""
    pending = [(a, b)]
    while pending:
        x, y = pending.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, list):
            if len(x) != len(y):
                return False
            pending.extend(zip(x, y, strict=True))
        elif isinstance(x, dict):
            if list(x) != list(y):
                return False
            pending.extend((x[k], y[k]) for k in x)
        elif isinstance(x, float) and math.isnan(x):
            if not math.isnan(y):
                return False
        elif x != y:
            return False
    return True


def stdlib(body: bytes) -> Any:
    """Decode the way the library does without orjson."""
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def test_one_input(data: bytes) -> None:
    """Decode random bytes both ways."""
    if _protocol._orjson is None:
        raise RuntimeError("install orjson to run this fuzzer")
    if not same(_protocol._load_json(data), stdlib(data)):
        raise AssertionError("orjson path decoded differently from json")


def main() -> None:
    """Run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
