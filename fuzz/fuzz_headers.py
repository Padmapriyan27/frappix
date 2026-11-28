"""Fuzz the validation of caller-supplied extra headers.

Headers that pass must never smuggle a line break or replace a header the
library manages (credentials, framing, compression).
"""

import sys

import atheris

with atheris.instrument_imports():
    from frappex_client import ConfigurationError
    from frappex_client._config import validate_extra_headers

MANAGED = {"authorization", "cookie", "content-length", "transfer-encoding", "accept-encoding"}


def test_one_input(data: bytes) -> None:
    """Validate random headers."""
    fdp = atheris.FuzzedDataProvider(data)
    headers = {
        fdp.ConsumeUnicode(20): fdp.ConsumeUnicode(40) for _ in range(fdp.ConsumeIntInRange(0, 4))
    }
    try:
        checked = validate_extra_headers(headers)
    except ConfigurationError:
        return
    for key, value in checked.items():
        if any(ch in key + value for ch in "\r\n\x00"):
            raise AssertionError(f"line break accepted in {key!r}")
        if key.lower() in MANAGED:
            raise AssertionError(f"managed header {key!r} accepted")


def main() -> None:
    """Run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
