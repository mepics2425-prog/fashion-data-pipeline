#!/usr/bin/env bash
set -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

cd "$PROJECT_ROOT" || exit 1

echo "================================================"
echo "FASHION IMAGE WORKER 시작"
echo "시작시간: $(date)"
echo "================================================"

while true
do
    echo
    echo "------------------------------------------------"
    echo "이미지 없는 상품 최대 100개 처리"
    echo "$(date)"
    echo "------------------------------------------------"

    "$PYTHON" -u -m enrichers.image_worker --limit 100

    RC=$?

    if [ "$RC" -ne 0 ]; then
        echo "image_worker 종료코드=$RC"
        echo "60초 후 다시 시도"
    else
        echo "이미지 워커 1사이클 완료"
    fi

    sleep 60
done
