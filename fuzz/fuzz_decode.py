"""Fuzz the decoding of replies, which come from a site that may be hostile.

Any status, header and body must end in a dict or a FrappexError; any other
exception, or an error message carrying the raw traceback, is a bug.
"""

import sys

import atheris

with atheris.instrument_imports():
    from frappex_client import FrappexError
    from frappex_client._protocol import RawResponse, decode, parse_retry_after


def test_one_input(data: bytes) -> None:
    """Decode one random reply."""
    fdp = atheris.FuzzedDataProvider(data)
    status = fdp.ConsumeIntInRange(100, 599)
    headers = {
        "content-type": fdp.ConsumeUnicodeNoSurrogates(40),
        "retry-after": fdp.ConsumeUnicodeNoSurrogates(20),
    }
    body = fdp.ConsumeBytes(fdp.remaining_bytes())
    parse_retry_after(headers)
    try:
        payload = decode(RawResponse(status, headers, body))
    except FrappexError as error:
        # The server traceback must never reach str(error).
        traceback = getattr(error, "traceback", None)
        if traceback and len(traceback) > 40 and traceback in str(error):
            raise AssertionError("server traceback leaked into str(error)") from None
        return
    if not isinstance(payload, dict):
        raise AssertionError(f"decode returned {type(payload).__name__}, not dict")


def main() -> None:
    """Run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
