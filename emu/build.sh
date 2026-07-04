#!/bin/sh
# Build the native µ'nSP core into a shared lib loadable by ctypes.
set -e
cd "$(dirname "$0")"
CC="${CC:-cc}"
OUT=libunspcore.so
$CC -O3 -shared -fPIC -o "$OUT" unsp_core.c
echo "built $OUT"
