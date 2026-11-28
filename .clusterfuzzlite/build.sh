#!/bin/bash -eu
# Build every fuzz target in fuzz/ for ClusterFuzzLite.
# Dependencies are installed from hashes exported from uv.lock.
python3 -m pip install --require-hashes --no-deps -r .clusterfuzzlite/requirements.txt
python3 -m pip install --no-deps .
for fuzzer in fuzz/fuzz_*.py; do
    compile_python_fuzzer "$fuzzer"
done
