"""Fuzz the request paths built from caller-supplied names.

DocType names, document names, method names and file URLs often come from
data other people control. Whatever they contain, the path must stay on the
intended endpoint, or the call must be refused with ConfigurationError.
"""

import sys

import atheris

with atheris.instrument_imports():
    from frappex_client import ConfigurationError
    from frappex_client._protocol import file_path, method_path, resource_path

FORBIDDEN = ("?", "#", "\\", "//", "/../", "/./", "\r", "\n", "\x00")


def check(path: str, prefix: str) -> None:
    if not path.startswith(prefix):
        raise AssertionError(f"{path!r} left {prefix!r}")
    if any(token in path for token in FORBIDDEN) or path.endswith(("/..", "/.")):
        raise AssertionError(f"unsafe path {path!r}")


def test_one_input(data: bytes) -> None:
    """Build paths from random names."""
    fdp = atheris.FuzzedDataProvider(data)
    doctype = fdp.ConsumeUnicode(30)
    name = fdp.ConsumeUnicode(30)
    method = fdp.ConsumeUnicode(30)
    file_url = fdp.ConsumeUnicode(60)
    try:
        path = resource_path(doctype, name)
        check(path, "/api/resource/")
        if path.count("/") != 4:
            raise AssertionError(f"a name escaped its segment: {path!r}")
    except ConfigurationError:
        pass
    try:
        check(method_path(method), "/api/method/")
    except ConfigurationError:
        pass
    try:
        path = file_path(file_url)
        if not path.startswith(("/files/", "/private/files/")):
            raise AssertionError(f"download outside the file locations: {path!r}")
        check(path, "/")
    except ConfigurationError:
        pass


def main() -> None:
    """Run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
