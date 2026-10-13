#!/bin/bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

cd "$PROJECT_ROOT" || exit 1

while true
do
    echo
    echo "=========================================="
    echo "29CM HTTP FAST START $(date)"
    echo "=========================================="

    "$PYTHON" -u -m collectors.cm29_http_fast

    EXIT_CODE=$?

    echo
    echo "29CM HTTP FAST END exit=$EXIT_CODE"
    echo "60초 후 다음 배치"

    sleep 60
done
