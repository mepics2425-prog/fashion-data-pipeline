#!/usr/bin/env python3

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from loaders import loader


from core.config import LOG_DIR

DEFAULT_INPUT = str(LOG_DIR / "musinsa_mass_probe_1000.jsonl")

SIZE_COLUMNS = loader.SIZE_COLUMNS


def clean(value):
    if value is None:
        return None

    value = str(value).strip()

    return value or None


def parse_price(value):
    if value is None:
        return None

    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()

        return int(float(value))

    except (TypeError, ValueError):
        return None


def iter_jsonl(path, limit=None):

    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"파일 없음: {path}"
        )

    count = 0

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:

        for line_no, line in enumerate(
            f,
            start=1,
        ):

            line = line.strip()

            if not line:
                continue

            try:
                item = json.loads(line)

            except Exception as e:
                print(
                    f"[line {line_no}] "
                    f"JSON 오류: {e}",
                    file=sys.stderr,
                )
                continue

            yield line_no, item

            count += 1

            if (
                limit is not None
                and count >= limit
            ):
                break


def build_loader_raw(item):

    pid = clean(
        item.get(
            "platform_product_id"
        )
    )

    brand = clean(
        item.get("brand")
    )

    product_name = clean(
        item.get("product_name")
    )

    product_url = clean(
        item.get("product_url")
    )

    search_keyword = clean(
        item.get("search_keyword")
    )

    gender = clean(
        item.get("gender")
    )

    main_category = clean(
        item.get("main_category")
    )

    sub_category = clean(
        item.get("sub_category")
    )

    # probe에서 category는 확인용으로
    # sub_category와 동일하게 저장했음.
    if not sub_category:
        sub_category = clean(
            item.get("category")
        )

    missing = []

    if not pid:
        missing.append(
            "platform_product_id"
        )

    if not brand:
        missing.append(
            "brand"
        )

    if not product_name:
        missing.append(
            "product_name"
        )

    if not main_category:
        missing.append(
            "main_category"
        )

    if not product_url:
        missing.append(
            "product_url"
        )

    if missing:
        raise ValueError(
            "필수값 누락: "
            + ", ".join(missing)
        )


    # ========================================================
    # actual_size_parsed
    #
    # JSON 저장 시 tuple이 list로 바뀌므로:
    #
    # [
    #   size_labels,
    #   measurements
    # ]
    # ========================================================

    parsed = item.get(
        "actual_size_parsed"
    )

    size_labels = []
    measurements = {}

    if (
        isinstance(parsed, (list, tuple))
        and len(parsed) == 2
    ):

        if isinstance(
            parsed[0],
            list,
        ):
            size_labels = (
                parsed[0]
            )

        if isinstance(
            parsed[1],
            dict,
        ):
            measurements = (
                parsed[1]
            )


    if (
        not size_labels
        and measurements
    ):
        size_labels = list(
            measurements.keys()
        )


    images = []

    image_url = clean(
        item.get("image_url")
    )

    if image_url:

        images.append({
            "index": 1,
            "source_url": image_url,
            "image_type": "thumbnail",
        })


    raw = {

        "source": {
            "platform":
                clean(
                    item.get(
                        "platform"
                    )
                )
                or "musinsa",
        },

        "collection": {
            "search_keyword":
                search_keyword,

            "collected_at":
                datetime.now()
                .isoformat(
                    timespec="seconds"
                ),
        },

        "classification": {
            "gender":
                gender,

            "main_category":
                main_category,

            "sub_category":
                sub_category,
        },

        "size_data": {
            "size_labels":
                size_labels,

            "measurements":
                measurements,

            "measurement_type":
                "garment_actual",
        },

        "product": {
            "product_id":
                pid,

            "platform_product_id":
                pid,

            "brand":
                brand,

            "product_name":
                product_name,

            "price":
                parse_price(
                    item.get("price")
                ),

            # mass 목록에는
            # 신뢰 가능한 color 정보 없음
            "color":
                None,

            "product_url":
                product_url,
        },

        # mass 단계에서는 아래 값을
        # 추측해서 채우지 않는다.
        "garment_attributes": {
            "material": None,
            "fit": None,
            "stretch": None,
            "thickness": None,
        },

        "model_info": [],

        "media": {
            "images":
                images,
        },

        "raw": {
            "mass_listing":
                item.get(
                    "listing_raw"
                ),

            "actual_size_api":
                item.get(
                    "actual_size_raw"
                ),

            "has_actual_size":
                item.get(
                    "has_actual_size"
                ),
        },
    }

    return raw


def normalize_item(item):

    raw = build_loader_raw(
        item
    )

    # 기존 loader.py의 정규화 로직을 그대로 재사용
    product = loader.normalize_product(
        raw
    )

    all_sizes = loader.normalize_sizes(
        raw
    )

    # 값 하나도 없는 빈 size row는 넣지 않는다.
    sizes = []

    for size in all_sizes:

        measurement_count = sum(
            1
            for col in SIZE_COLUMNS
            if size.get(col)
            is not None
        )

        if measurement_count == 0:
            continue

        size[
            "_measurement_count"
        ] = measurement_count

        sizes.append(
            size
        )


    images = loader.normalize_images(
        raw
    )

    return (
        raw,
        product,
        sizes,
        images,
    )


def safe_upsert(
    conn,
    raw,
    product,
    sizes,
    images,
):

    with conn.cursor() as cur:

        # ====================================================
        # PRODUCTS
        #
        # 중요:
        # color/material/fit/stretch/thickness는
        # UPDATE 문에 아예 없음.
        #
        # 따라서 기존 상세 데이터 보존.
        # ====================================================

        cur.execute(
            """
            INSERT INTO products
            (
                platform,
                platform_product_id,
                brand,
                product_name,
                gender,
                main_category,
                sub_category,
                price,
                product_url,
                search_keyword,
                raw,
                collected_at,
                updated_at
            )
            VALUES
            (
                %s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                %s,%s,NULL
            )

            ON DUPLICATE KEY UPDATE

                id =
                    LAST_INSERT_ID(id),

                brand =
                    COALESCE(
                        NULLIF(
                            VALUES(brand),
                            ''
                        ),
                        brand
                    ),

                product_name =
                    COALESCE(
                        NULLIF(
                            VALUES(product_name),
                            ''
                        ),
                        product_name
                    ),

                price =
                    COALESCE(
                        VALUES(price),
                        price
                    ),

                product_url =
                    COALESCE(
                        NULLIF(
                            VALUES(product_url),
                            ''
                        ),
                        product_url
                    ),

                gender =
                    CASE
                        WHEN gender IS NULL
                          OR gender = ''
                          OR gender = 'unknown'
                        THEN VALUES(gender)

                        ELSE gender
                    END,

                main_category =
                    COALESCE(
                        main_category,
                        VALUES(main_category)
                    ),

                sub_category =
                    COALESCE(
                        sub_category,
                        VALUES(sub_category)
                    ),

                search_keyword =
                    COALESCE(
                        search_keyword,
                        VALUES(search_keyword)
                    ),

                raw =
                    COALESCE(
                        raw,
                        VALUES(raw)
                    ),

                collected_at =
                    COALESCE(
                        collected_at,
                        VALUES(collected_at)
                    ),

                updated_at =
                    NOW()
            """,
            (
                product[
                    "platform"
                ],

                product[
                    "platform_product_id"
                ],

                product[
                    "brand"
                ],

                product[
                    "product_name"
                ],

                product[
                    "gender"
                ],

                product[
                    "main_category"
                ],

                product[
                    "sub_category"
                ],

                product[
                    "price"
                ],

                product[
                    "product_url"
                ],

                product[
                    "search_keyword"
                ],

                json.dumps(
                    raw,
                    ensure_ascii=False,
                ),

                product[
                    "collected_at"
                ],
            ),
        )

        product_db_id = (
            cur.lastrowid
        )


        # ====================================================
        # PRODUCT_SIZES
        #
        # 기존 non-NULL 실측값은 절대 덮지 않음.
        # 비어 있는 셀만 새 API 값으로 채움.
        #
        # DELETE 없음.
        # ====================================================

        if sizes:

            rows = []

            for s in sizes:

                rows.append((
                    product_db_id,

                    s[
                        "size_label"
                    ],

                    s[
                        "measurement_type"
                    ],

                    s[
                        "total_length_cm"
                    ],

                    s[
                        "shoulder_cm"
                    ],

                    s[
                        "chest_flat_cm"
                    ],

                    s[
                        "chest_circumference_cm"
                    ],

                    s[
                        "sleeve_length_cm"
                    ],

                    s[
                        "armhole_cm"
                    ],

                    s[
                        "sleeve_width_cm"
                    ],

                    s[
                        "waist_flat_cm"
                    ],

                    s[
                        "waist_circumference_cm"
                    ],

                    s[
                        "hip_flat_cm"
                    ],

                    s[
                        "hip_circumference_cm"
                    ],

                    s[
                        "thigh_flat_cm"
                    ],

                    s[
                        "rise_cm"
                    ],

                    s[
                        "inseam_cm"
                    ],

                    s[
                        "hem_flat_cm"
                    ],

                    (
                        json.dumps(
                            s.get("extra"),
                            ensure_ascii=False,
                        )
                        if s.get("extra")
                        else None
                    ),
                ))


            cur.executemany(
                """
                INSERT INTO product_sizes
                (
                    product_id,
                    size_label,
                    measurement_type,

                    total_length_cm,
                    shoulder_cm,

                    chest_flat_cm,
                    chest_circumference_cm,

                    sleeve_length_cm,
                    armhole_cm,
                    sleeve_width_cm,

                    waist_flat_cm,
                    waist_circumference_cm,

                    hip_flat_cm,
                    hip_circumference_cm,

                    thigh_flat_cm,
                    rise_cm,
                    inseam_cm,
                    hem_flat_cm,

                    extra
                )

                VALUES
                (
                    %s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,
                    %s,%s,
                    %s,%s,
                    %s,%s,%s,%s,
                    %s
                )

                ON DUPLICATE KEY UPDATE

                    total_length_cm =
                        COALESCE(
                            total_length_cm,
                            VALUES(
                                total_length_cm
                            )
                        ),

                    shoulder_cm =
                        COALESCE(
                            shoulder_cm,
                            VALUES(
                                shoulder_cm
                            )
                        ),

                    chest_flat_cm =
                        COALESCE(
                            chest_flat_cm,
                            VALUES(
                                chest_flat_cm
                            )
                        ),

                    chest_circumference_cm =
                        COALESCE(
                            chest_circumference_cm,
                            VALUES(
                                chest_circumference_cm
                            )
                        ),

                    sleeve_length_cm =
                        COALESCE(
                            sleeve_length_cm,
                            VALUES(
                                sleeve_length_cm
                            )
                        ),

                    armhole_cm =
                        COALESCE(
                            armhole_cm,
                            VALUES(
                                armhole_cm
                            )
                        ),

                    sleeve_width_cm =
                        COALESCE(
                            sleeve_width_cm,
                            VALUES(
                                sleeve_width_cm
                            )
                        ),

                    waist_flat_cm =
                        COALESCE(
                            waist_flat_cm,
                            VALUES(
                                waist_flat_cm
                            )
                        ),

                    waist_circumference_cm =
                        COALESCE(
                            waist_circumference_cm,
                            VALUES(
                                waist_circumference_cm
                            )
                        ),

                    hip_flat_cm =
                        COALESCE(
                            hip_flat_cm,
                            VALUES(
                                hip_flat_cm
                            )
                        ),

                    hip_circumference_cm =
                        COALESCE(
                            hip_circumference_cm,
                            VALUES(
                                hip_circumference_cm
                            )
                        ),

                    thigh_flat_cm =
                        COALESCE(
                            thigh_flat_cm,
                            VALUES(
                                thigh_flat_cm
                            )
                        ),

                    rise_cm =
                        COALESCE(
                            rise_cm,
                            VALUES(
                                rise_cm
                            )
                        ),

                    inseam_cm =
                        COALESCE(
                            inseam_cm,
                            VALUES(
                                inseam_cm
                            )
                        ),

                    hem_flat_cm =
                        COALESCE(
                            hem_flat_cm,
                            VALUES(
                                hem_flat_cm
                            )
                        ),

                    extra =
                        COALESCE(
                            extra,
                            VALUES(extra)
                        )
                """,
                rows,
            )


        # ====================================================
        # PRODUCT_IMAGES
        #
        # 현재 mass probe에는 thumbnail 1장.
        #
        # 기존 image_order=1이 이미 있으면
        # 기존 URL을 보존한다.
        #
        # DELETE 없음.
        # ====================================================

        if images:

            rows = [
                (
                    product_db_id,

                    x[
                        "image_order"
                    ],

                    x[
                        "source_url"
                    ],

                    x[
                        "image_type"
                    ],
                )

                for x in images
            ]

            cur.executemany(
                """
                INSERT INTO product_images
                (
                    product_id,
                    image_order,
                    source_url,
                    image_type
                )

                VALUES
                (
                    %s,%s,%s,%s
                )

                ON DUPLICATE KEY UPDATE

                    source_url =
                        CASE
                            WHEN source_url IS NULL
                              OR source_url = ''
                            THEN VALUES(source_url)

                            ELSE source_url
                        END,

                    image_type =
                        COALESCE(
                            image_type,
                            VALUES(image_type)
                        )
                """,
                rows,
            )


        # product_models는
        # mass 단계에서 절대 건드리지 않는다.


    conn.commit()

    measurement_cells = sum(
        s.get(
            "_measurement_count",
            0,
        )
        for s in sizes
    )

    return (
        product_db_id,
        len(sizes),
        measurement_cells,
        len(images),
    )


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--input",
        default=DEFAULT_INPUT,
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=None,
    )

    ap.add_argument(
        "--write",
        action="store_true",
        help="실제 MySQL 저장",
    )

    args = ap.parse_args()


    rows = list(
        iter_jsonl(
            args.input,
            args.limit,
        )
    )


    if not rows:

        print(
            "입력 상품 없음"
        )

        return 2


    print("=" * 72)

    print(
        "MUSINSA MASS SAFE LOADER"
    )

    print("=" * 72)

    print(
        f"입력: {args.input}"
    )

    print(
        f"상품: {len(rows)}개"
    )

    print(
        "모드: "
        + (
            "DB WRITE"
            if args.write
            else "DRY-RUN"
        )
    )

    print(
        "삭제 작업: 없음"
    )

    print(
        "기존 실측 덮어쓰기: 없음"
    )

    print(
        "기존 상세속성 NULL 덮어쓰기: 없음"
    )

    print("=" * 72)


    if not args.write:

        ok = 0
        failed = 0

        for idx, (
            line_no,
            item,
        ) in enumerate(
            rows,
            start=1,
        ):

            try:

                (
                    raw,
                    product,
                    sizes,
                    images,
                ) = normalize_item(
                    item
                )

                measurement_cells = sum(
                    s.get(
                        "_measurement_count",
                        0,
                    )
                    for s in sizes
                )

                print(
                    f"[{idx}/{len(rows)}] "
                    f"OK "
                    f"pid={product['platform_product_id']} "
                    f"brand={product['brand']} "
                    f"sizes={len(sizes)} "
                    f"cells={measurement_cells} "
                    f"images={len(images)}"
                )

                ok += 1

            except Exception as e:

                print(
                    f"[{idx}/{len(rows)}] "
                    f"ERROR "
                    f"line={line_no}: "
                    f"{e}",
                    file=sys.stderr,
                )

                failed += 1


        print()

        print(
            f"DRY-RUN 완료: "
            f"성공={ok} "
            f"실패={failed}"
        )

        return (
            1
            if failed
            else 0
        )


    conn = loader.build_conn()

    ok = 0
    failed = 0

    try:

        for idx, (
            line_no,
            item,
        ) in enumerate(
            rows,
            start=1,
        ):

            try:

                (
                    raw,
                    product,
                    sizes,
                    images,
                ) = normalize_item(
                    item
                )

                (
                    db_id,
                    size_count,
                    cell_count,
                    image_count,
                ) = safe_upsert(
                    conn,
                    raw,
                    product,
                    sizes,
                    images,
                )

                print(
                    f"[{idx}/{len(rows)}] "
                    f"OK "
                    f"pid={product['platform_product_id']} "
                    f"db_id={db_id} "
                    f"sizes={size_count} "
                    f"cells={cell_count} "
                    f"images={image_count}"
                )

                ok += 1

            except Exception as e:

                conn.rollback()

                print(
                    f"[{idx}/{len(rows)}] "
                    f"ERROR "
                    f"line={line_no}: "
                    f"{e}",
                    file=sys.stderr,
                )

                failed += 1

    finally:

        conn.close()


    print()
    print("=" * 72)

    print(
        f"완료: "
        f"성공={ok} "
        f"실패={failed} "
        f"총={len(rows)}"
    )

    print("=" * 72)

    return (
        1
        if failed
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
