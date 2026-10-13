#!/usr/bin/env python3

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from core.config import STATE_DIR as BASE_STATE_DIR, LOG_DIR

from playwright.sync_api import sync_playwright

from collectors import musinsa_mass_pipeline as mp


STATE_DIR = BASE_STATE_DIR / "musinsa_mass"
REJECTED_PATH = STATE_DIR / "rejected_ids.txt"
PROGRESS_PATH = STATE_DIR / "progress.json"

STATE_DIR.mkdir(
    parents=True,
    exist_ok=True
)


def load_id_file(path):

    if not path.exists():
        return set()

    result = set()

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():

        value = line.strip()

        if value:
            result.add(value)

    return result


def append_ids(path, ids):

    ids = {
        str(x).strip()
        for x in ids
        if str(x).strip()
    }

    if not ids:
        return 0

    old = load_id_file(path)
    new = ids - old

    if not new:
        return 0

    with path.open(
        "a",
        encoding="utf-8"
    ) as f:

        for pid in sorted(new):
            f.write(pid + "\n")

    return len(new)


def product_id_of(item):

    if not isinstance(item, dict):
        return None

    for key in (
        "platform_product_id",
        "product_id",
        "goodsNo",
        "goods_no",
        "id",
    ):

        value = item.get(key)

        if value is not None:
            value = str(value).strip()

            if value:
                return value

    product = item.get("product")

    if isinstance(product, dict):

        for key in (
            "platform_product_id",
            "product_id",
            "goodsNo",
            "goods_no",
            "id",
        ):

            value = product.get(key)

            if value is not None:
                value = str(value).strip()

                if value:
                    return value

    return None


def write_progress(data):

    tmp = PROGRESS_PATH.with_suffix(
        ".json.tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        PROGRESS_PATH
    )


def main():

    ap = argparse.ArgumentParser()

    # 현재 DB에서 몇 개를 더 추가할지
    ap.add_argument(
        "--add",
        type=int,
        default=1000,
    )

    # 최종 DB 총 상품 수 목표.
    # 이 값을 주면 --add 대신 이것을 사용.
    ap.add_argument(
        "--target-total",
        type=int,
        default=0,
    )

    # 한 사이클에서 찾을 후보 수
    ap.add_argument(
        "--cycle-candidates",
        type=int,
        default=3000,
    )

    ap.add_argument(
        "--per-keyword",
        type=int,
        default=500,
    )

    ap.add_argument(
        "--batch-size",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--min-sizes",
        type=int,
        default=1,
    )

    ap.add_argument(
        "--min-fields",
        type=int,
        default=3,
    )

    ap.add_argument(
        "--sleep",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--max-no-progress",
        type=int,
        default=5,
    )

    args = ap.parse_args()


    started = time.perf_counter()

    db_start = mp.db_product_count()

    if args.target_total > 0:

        target_total = (
            args.target_total
        )

    else:

        target_total = (
            db_start
            +
            args.add
        )


    if target_total <= db_start:

        print(
            f"이미 목표 달성: "
            f"{db_start} >= {target_total}"
        )

        return 0


    print()
    print("=" * 72)
    print("MUSINSA CONTINUOUS MASS COLLECTOR")
    print("=" * 72)

    print(
        f"현재 DB        : {db_start}"
    )

    print(
        f"최종 목표      : {target_total}"
    )

    print(
        f"필요 신규      : "
        f"{target_total - db_start}"
    )

    print(
        f"사이클 후보    : "
        f"{args.cycle_candidates}"
    )

    print(
        f"검색어당 후보  : "
        f"{args.per_keyword}"
    )

    print(
        f"actual-size 병렬: "
        f"{args.batch_size}"
    )

    print(
        f"품질 기준      : "
        f"size>={args.min_sizes}, "
        f"fields>={args.min_fields}"
    )

    print(
        f"탈락 ID 기록   : "
        f"{REJECTED_PATH}"
    )

    print("=" * 72)


    total_saved_reported = 0
    total_rejected_reported = 0
    total_failed_reported = 0

    cycle = 0
    no_progress_cycles = 0


    with sync_playwright() as p:

        while True:

            current_db = (
                mp.db_product_count()
            )

            if current_db >= target_total:

                print()
                print(
                    "🎉 목표 DB 수량 달성"
                )

                break


            cycle += 1

            remaining = (
                target_total
                -
                current_db
            )

            # 목표가 얼마 안 남았으면
            # 불필요하게 3000개까지 찾지 않음.
            cycle_candidate_target = min(
                args.cycle_candidates,
                max(
                    300,
                    remaining * 5,
                ),
            )


            existing_ids = (
                mp.load_existing_ids()
            )

            rejected_ids = (
                load_id_file(
                    REJECTED_PATH
                )
            )

            blocked_ids = (
                set(existing_ids)
                |
                set(rejected_ids)
            )


            print()
            print("=" * 72)

            print(
                f"CYCLE {cycle}"
            )

            print("=" * 72)

            print(
                f"현재 DB       : "
                f"{current_db}"
            )

            print(
                f"남은 목표     : "
                f"{remaining}"
            )

            print(
                f"DB 기존 ID    : "
                f"{len(existing_ids)}"
            )

            print(
                f"품질 탈락 ID  : "
                f"{len(rejected_ids)}"
            )

            print(
                f"이번 후보 목표: "
                f"{cycle_candidate_target}"
            )


            archive = LOG_DIR / (
                ""
                f"musinsa_mass_continuous_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"_c{cycle:04d}.jsonl"
            )


            browser = None

            try:

                browser = (
                    p.chromium.launch(
                        headless=True
                    )
                )

                context = (
                    browser.new_context(
                        user_agent=mp.USER_AGENT,
                        locale="ko-KR",
                        viewport={
                            "width": 1440,
                            "height": 1000,
                        },
                    )
                )

                page = (
                    context.new_page()
                )


                products = (
                    mp.collect_candidates(
                        page,
                        blocked_ids,
                        cycle_candidate_target,
                        args.per_keyword,
                    )
                )


                if not products:

                    print(
                        "⚠ 신규 후보를 "
                        "찾지 못했습니다."
                    )

                    no_progress_cycles += 1

                    write_progress({
                        "updated_at":
                            datetime.now().isoformat(),

                        "cycle":
                            cycle,

                        "db_start":
                            db_start,

                        "current_db":
                            current_db,

                        "target_total":
                            target_total,

                        "remaining":
                            remaining,

                        "rejected_ids":
                            len(rejected_ids),

                        "status":
                            "no_candidates",
                    })

                    if (
                        no_progress_cycles
                        >=
                        args.max_no_progress
                    ):

                        print(
                            "연속 후보 없음 기준 초과."
                        )

                        break

                    time.sleep(
                        args.sleep
                    )

                    continue


                print(
                    f"후보 확보: "
                    f"{len(products)}개"
                )


                products = (
                    mp.fetch_actual_sizes(
                        page,
                        products,
                        args.batch_size,
                    )
                )


                # 품질 탈락 ID는 영구 상태에 남겨서
                # 다음 사이클에서 같은 상품의
                # actual-size를 또 검사하지 않는다.
                quality_rejected_ids = set()

                for item in products:

                    pid = (
                        product_id_of(item)
                    )

                    if not pid:
                        continue

                    try:

                        ok = mp.quality_ok(
                            item,
                            args.min_sizes,
                            args.min_fields,
                        )

                    except Exception:

                        # 검사 함수 자체 오류라면
                        # 영구 탈락시키지 않음.
                        continue


                    if not ok:

                        quality_rejected_ids.add(
                            pid
                        )


                new_rejected = append_ids(
                    REJECTED_PATH,
                    quality_rejected_ids,
                )


                print(
                    f"신규 품질탈락 ID 기록: "
                    f"{new_rejected}"
                )


                current_before_save = (
                    mp.db_product_count()
                )

                save_limit = max(
                    0,
                    target_total
                    -
                    current_before_save
                )


                if save_limit <= 0:

                    break


                (
                    saved,
                    rejected,
                    failed,
                ) = mp.save_to_db(
                    products,
                    save_limit,
                    archive,
                    args.min_sizes,
                    args.min_fields,
                )


                current_after_save = (
                    mp.db_product_count()
                )

                actual_growth = (
                    current_after_save
                    -
                    current_before_save
                )


                total_saved_reported += (
                    saved
                )

                total_rejected_reported += (
                    rejected
                )

                total_failed_reported += (
                    failed
                )


                print()
                print(
                    f"CYCLE {cycle} RESULT"
                )

                print(
                    f"저장 성공     : "
                    f"{saved}"
                )

                print(
                    f"DB 실제 증가  : "
                    f"{actual_growth}"
                )

                print(
                    f"품질 탈락     : "
                    f"{rejected}"
                )

                print(
                    f"저장 실패     : "
                    f"{failed}"
                )

                print(
                    f"현재 DB       : "
                    f"{current_after_save}"
                )

                print(
                    f"목표까지      : "
                    f"{max(0, target_total-current_after_save)}"
                )

                print(
                    f"archive       : "
                    f"{archive}"
                )


                if actual_growth > 0:

                    no_progress_cycles = 0

                else:

                    no_progress_cycles += 1


                write_progress({
                    "updated_at":
                        datetime.now().isoformat(),

                    "cycle":
                        cycle,

                    "db_start":
                        db_start,

                    "current_db":
                        current_after_save,

                    "target_total":
                        target_total,

                    "remaining":
                        max(
                            0,
                            target_total
                            -
                            current_after_save,
                        ),

                    "saved_this_process":
                        total_saved_reported,

                    "rejected_this_process":
                        total_rejected_reported,

                    "failed_this_process":
                        total_failed_reported,

                    "rejected_ids_total":
                        len(
                            load_id_file(
                                REJECTED_PATH
                            )
                        ),

                    "last_archive":
                        str(archive),

                    "status":
                        "running",
                })


                if (
                    no_progress_cycles
                    >=
                    args.max_no_progress
                ):

                    print()
                    print(
                        "⚠ 여러 사이클 연속 "
                        "DB 증가가 없어 종료."
                    )

                    break


            except KeyboardInterrupt:

                print()
                print(
                    "사용자 중단 요청"
                )

                break


            except Exception as e:

                print()
                print(
                    f"❌ CYCLE {cycle} 오류: "
                    f"{type(e).__name__}: {e}"
                )

                no_progress_cycles += 1

                write_progress({
                    "updated_at":
                        datetime.now().isoformat(),

                    "cycle":
                        cycle,

                    "current_db":
                        mp.db_product_count(),

                    "target_total":
                        target_total,

                    "error":
                        f"{type(e).__name__}: {e}",

                    "status":
                        "cycle_error",
                })


            finally:

                if browser is not None:

                    try:
                        browser.close()
                    except Exception:
                        pass


            if (
                mp.db_product_count()
                <
                target_total
            ):

                print(
                    f"{args.sleep}초 후 "
                    "다음 사이클..."
                )

                time.sleep(
                    args.sleep
                )


    db_final = (
        mp.db_product_count()
    )

    elapsed = (
        time.perf_counter()
        -
        started
    )


    status = (
        "completed"
        if db_final >= target_total
        else "stopped"
    )


    write_progress({
        "updated_at":
            datetime.now().isoformat(),

        "db_start":
            db_start,

        "db_final":
            db_final,

        "target_total":
            target_total,

        "db_growth":
            db_final - db_start,

        "saved_this_process":
            total_saved_reported,

        "rejected_this_process":
            total_rejected_reported,

        "failed_this_process":
            total_failed_reported,

        "elapsed_seconds":
            round(elapsed, 1),

        "status":
            status,
    })


    print()
    print("=" * 72)
    print("CONTINUOUS MASS FINAL")
    print("=" * 72)

    print(
        f"시작 DB     : {db_start}"
    )

    print(
        f"종료 DB     : {db_final}"
    )

    print(
        f"실제 증가   : "
        f"{db_final - db_start}"
    )

    print(
        f"목표 DB     : "
        f"{target_total}"
    )

    print(
        f"상태        : "
        f"{status}"
    )

    print(
        f"전체 시간   : "
        f"{elapsed:.1f}초"
    )

    print(
        f"진행상태    : "
        f"{PROGRESS_PATH}"
    )

    print("=" * 72)

    return (
        0
        if db_final >= target_total
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
