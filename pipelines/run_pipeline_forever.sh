#!/usr/bin/env bash

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

while true
do
    echo
    echo "================================================"
    echo "새 수집 사이클: $(date '+%F %T')"
    echo "================================================"

    MAX_NEW_PRODUCTS=10 ./pipelines/run_pipeline.sh

    echo
    echo "다음 사이클까지 10초 대기..."
    sleep 10
done
