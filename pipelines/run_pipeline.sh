#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RAW_DIR="${RAW_DIR:-$PROJECT_ROOT/data/raw}"
LOG_DIR="${LOG_DIR:-$PROJECT_ROOT/data/logs}"
PYTHON="${PYTHON:-python3}"

MAX_NEW_PRODUCTS="${MAX_NEW_PRODUCTS:-10}"

mkdir -p "$RAW_DIR" "$LOG_DIR"

exec 9>/tmp/fashion-pipeline.lock

if ! flock -n 9; then
    echo "이미 파이프라인 실행 중 - 건너뜀"
    exit 0
fi

RUN_TS="$(date '+%Y%m%d_%H%M%S')"
TODAY="$(date '+%F')"

LOG_FILE="$LOG_DIR/fashion_pipeline_${RUN_TS}.log"
MARKER="/tmp/fashion_pipeline_${RUN_TS}.marker"
SEEN_FILE="/tmp/fashion_seen_${RUN_TS}.txt"

touch "$MARKER"
touch "$SEEN_FILE"

exec > >(tee -a "$LOG_FILE") 2>&1

cd "$PROJECT_ROOT" || exit 1

echo "================================================"
echo "FASHION DATA PIPELINE"
echo "수집 목표: ${MAX_NEW_PRODUCTS}개"
echo "RAW_DIR: $RAW_DIR"
echo "LOG_DIR: $LOG_DIR"
echo "================================================"

CRAWLER_RC=0
LOADER_FAILURES=0

echo
echo "[1/3] 무신사 수집 시작"

TARGET_COUNT=0 \
MAX_NEW_PRODUCTS="$MAX_NEW_PRODUCTS" \
DOWNLOAD_IMAGES=false \
HEADLESS=true \
RAW_DIR="$RAW_DIR" \
"$PYTHON" -u -m core.crawler &

CRAWLER_PID=$!

echo "crawler pid=$CRAWLER_PID"

load_new_products() {

    if [ ! -d "$RAW_DIR/$TODAY" ]; then
        return
    fi

    while IFS= read -r -d '' FILE; do

        if [ ! "$FILE" -nt "$MARKER" ]; then
            continue
        fi

        if grep -Fxq "$FILE" "$SEEN_FILE"; then
            continue
        fi

        if ! "$PYTHON" - "$FILE" <<'PY'
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)
except Exception:
    raise SystemExit(1)
PY
        then
            continue
        fi

        PRODUCT_DIR="$(dirname "$FILE")"
        PRODUCT_ID="$(basename "$PRODUCT_DIR")"

        echo
        echo "신규 상품 발견: $PRODUCT_ID"

        if "$PYTHON" -u -m loaders.loader --input "$PRODUCT_DIR"; then
            echo "$FILE" >> "$SEEN_FILE"
            echo "MySQL 저장 완료: $PRODUCT_ID"
        else
            echo "MySQL 저장 실패: $PRODUCT_ID"
            LOADER_FAILURES=$((LOADER_FAILURES + 1))
        fi

    done < <(
        find "$RAW_DIR/$TODAY" \
            -type f \
            -name product.json \
            -newer "$MARKER" \
            -print0 2>/dev/null
    )
}

echo
echo "[2/3] 신규 상품 감시"

while kill -0 "$CRAWLER_PID" 2>/dev/null; do
    load_new_products
    sleep 2
done

wait "$CRAWLER_PID"
CRAWLER_RC=$?

load_new_products

echo
echo "[3/3] 이미지 작업은 별도 enricher 사용"

LOADED_COUNT="$(
    wc -l < "$SEEN_FILE" 2>/dev/null || echo 0
)"

echo
echo "================================================"
echo "사이클 완료"
echo "crawler=$CRAWLER_RC"
echo "loader_failures=$LOADER_FAILURES"
echo "이번 사이클 DB 저장=$LOADED_COUNT"
echo "================================================"

rm -f "$MARKER"
rm -f "$SEEN_FILE"

if [ "$CRAWLER_RC" -ne 0 ]; then
    exit 1
fi

exit 0
