import os
import csv
import json
from pathlib import Path
from collections import Counter, defaultdict

import pymysql


from core.config import LOG_DIR

OUT_JSON = LOG_DIR / "product_quality_audit.json"
OUT_CSV = LOG_DIR / "product_quality_audit.csv"
OUT_SUMMARY = LOG_DIR / "product_quality_summary.json"


def get_conn():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("DE_USER") or os.environ.get("MYSQL_USER"),
        password=os.environ.get("DE_PASSWORD") or os.environ.get("MYSQL_PASSWORD"),
        database=os.environ.get("MYSQL_DB", "fashion"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


MEASUREMENT_COLUMNS = [
    "total_length_cm",
    "shoulder_cm",
    "chest_flat_cm",
    "chest_circumference_cm",
    "sleeve_length_cm",
    "armhole_cm",
    "sleeve_width_cm",
    "waist_flat_cm",
    "waist_circumference_cm",
    "hip_flat_cm",
    "hip_circumference_cm",
    "thigh_flat_cm",
    "rise_cm",
    "inseam_cm",
    "hem_flat_cm",
]


def valid(v):
    try:
        return v is not None and float(v) > 0
    except Exception:
        return False


def has_any(fields, *names):
    return any(x in fields for x in names)


def classify_quality(product, size_rows):
    """
    A = 핵심실측 충족 + 추가정보 풍부
    B = 신체매칭 최소 핵심실측 충족
    C = 일부 실측은 있으나 신체매칭 핵심조합 부족
    REJECT = 실측 자체가 거의 없음
    """

    main = str(product.get("main_category") or "").strip()
    sub = str(product.get("sub_category") or "").strip()
    name = str(product.get("product_name") or "").strip()

    category_text = f"{main} {sub} {name}".lower()

    fields = set()
    sizes_with_measurements = 0

    for row in size_rows:
        row_has = False

        for col in MEASUREMENT_COLUMNS:
            if valid(row.get(col)):
                fields.add(col)
                row_has = True

        if row_has:
            sizes_with_measurements += 1

    field_count = len(fields)

    if field_count < 2 or sizes_with_measurements == 0:
        return {
            "grade": "REJECT",
            "reason": "사용 가능한 실측이 2개 미만",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    chest = has_any(
        fields,
        "chest_flat_cm",
        "chest_circumference_cm",
    )

    waist = has_any(
        fields,
        "waist_flat_cm",
        "waist_circumference_cm",
    )

    hip = has_any(
        fields,
        "hip_flat_cm",
        "hip_circumference_cm",
    )

    total = "total_length_cm" in fields
    shoulder = "shoulder_cm" in fields
    sleeve = "sleeve_length_cm" in fields
    armhole = "armhole_cm" in fields
    sleeve_width = "sleeve_width_cm" in fields
    thigh = "thigh_flat_cm" in fields
    rise = "rise_cm" in fields
    inseam = "inseam_cm" in fields
    hem = "hem_flat_cm" in fields

    # --------------------------------------------------------
    # 상의
    # 최소: 가슴 + 총장 + (어깨 or 소매)
    # --------------------------------------------------------
    if main == "상의":
        core = chest and total and (shoulder or sleeve)

        if not core:
            return {
                "grade": "C",
                "reason": "상의 핵심실측 부족",
                "fields": sorted(fields),
                "field_count": field_count,
                "measurement_size_count": sizes_with_measurements,
            }

        # 가슴/총장/어깨/소매 4개 또는 핵심 + 암홀 등
        rich = (
            field_count >= 4
            and (
                (shoulder and sleeve)
                or armhole
                or sleeve_width
                or hem
            )
        )

        return {
            "grade": "A" if rich else "B",
            "reason": "상의 핵심실측 충족",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    # --------------------------------------------------------
    # 아우터
    # 최소: 가슴 + 총장 + (어깨 or 소매)
    # --------------------------------------------------------
    if main == "아우터":
        core = chest and total and (shoulder or sleeve)

        if not core:
            return {
                "grade": "C",
                "reason": "아우터 핵심실측 부족",
                "fields": sorted(fields),
                "field_count": field_count,
                "measurement_size_count": sizes_with_measurements,
            }

        rich = (
            field_count >= 4
            and (
                (shoulder and sleeve)
                or armhole
                or sleeve_width
                or hem
            )
        )

        return {
            "grade": "A" if rich else "B",
            "reason": "아우터 핵심실측 충족",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    # --------------------------------------------------------
    # 스커트
    # 최소: 허리 + 총장
    # A: 힙/밑단까지 존재
    # --------------------------------------------------------
    if main == "하의" and "스커트" in category_text:
        core = waist and total

        if not core:
            return {
                "grade": "C",
                "reason": "스커트 핵심실측 부족",
                "fields": sorted(fields),
                "field_count": field_count,
                "measurement_size_count": sizes_with_measurements,
            }

        rich = hip or hem

        return {
            "grade": "A" if rich else "B",
            "reason": "스커트 핵심실측 충족",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    # --------------------------------------------------------
    # 팬츠
    # 최소:
    # 허리
    # + 힙/허벅지
    # + 총장/인심/밑위
    # --------------------------------------------------------
    if main == "하의":
        core = (
            waist
            and (hip or thigh)
            and (total or inseam or rise)
        )

        if not core:
            return {
                "grade": "C",
                "reason": "팬츠 핵심실측 부족",
                "fields": sorted(fields),
                "field_count": field_count,
                "measurement_size_count": sizes_with_measurements,
            }

        rich = (
            field_count >= 4
            and (
                hip
                and thigh
                or inseam
                or rise
                or hem
            )
        )

        return {
            "grade": "A" if rich else "B",
            "reason": "팬츠 핵심실측 충족",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    # --------------------------------------------------------
    # 원피스
    # 최소:
    # 가슴 + 총장 + 허리/힙/어깨
    # --------------------------------------------------------
    if main == "원피스":
        core = (
            chest
            and total
            and (waist or hip or shoulder)
        )

        if not core:
            return {
                "grade": "C",
                "reason": "원피스 핵심실측 부족",
                "fields": sorted(fields),
                "field_count": field_count,
                "measurement_size_count": sizes_with_measurements,
            }

        rich = field_count >= 4

        return {
            "grade": "A" if rich else "B",
            "reason": "원피스 핵심실측 충족",
            "fields": sorted(fields),
            "field_count": field_count,
            "measurement_size_count": sizes_with_measurements,
        }

    # 분류가 애매한 의류
    if field_count >= 4:
        grade = "B"
        reason = "기타 의류 - 실측 4개 이상"
    else:
        grade = "C"
        reason = "기타 의류 - 핵심조합 미정"

    return {
        "grade": grade,
        "reason": reason,
        "fields": sorted(fields),
        "field_count": field_count,
        "measurement_size_count": sizes_with_measurements,
    }


def main():
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    platform_product_id,
                    brand,
                    product_name,
                    main_category,
                    sub_category,
                    product_url
                FROM products
                ORDER BY id
            """)

            products = cur.fetchall()

            cur.execute("""
                SELECT
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
                    hem_flat_cm
                FROM product_sizes
                ORDER BY product_id, id
            """)

            sizes = cur.fetchall()

    finally:
        conn.close()

    by_product = defaultdict(list)

    for row in sizes:
        by_product[row["product_id"]].append(row)

    results = []
    grade_counter = Counter()
    category_grade = defaultdict(Counter)

    for p in products:
        quality = classify_quality(
            p,
            by_product.get(p["id"], [])
        )

        result = {
            "db_id": p["id"],
            "platform_product_id": p["platform_product_id"],
            "brand": p["brand"],
            "product_name": p["product_name"],
            "main_category": p["main_category"],
            "sub_category": p["sub_category"],
            "product_url": p["product_url"],
            **quality,
        }

        results.append(result)

        grade_counter[quality["grade"]] += 1
        category_grade[
            p["main_category"] or "미분류"
        ][quality["grade"]] += 1

    OUT_JSON.write_text(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    with OUT_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as f:
        writer = csv.writer(f)

        writer.writerow([
            "platform_product_id",
            "brand",
            "product_name",
            "main_category",
            "sub_category",
            "grade",
            "reason",
            "field_count",
            "measurement_size_count",
            "fields",
            "product_url",
        ])

        for r in results:
            writer.writerow([
                r["platform_product_id"],
                r["brand"],
                r["product_name"],
                r["main_category"],
                r["sub_category"],
                r["grade"],
                r["reason"],
                r["field_count"],
                r["measurement_size_count"],
                ",".join(r["fields"]),
                r["product_url"],
            ])

    summary = {
        "total": len(results),
        "grade": dict(grade_counter),
        "category_grade": {
            category: dict(counts)
            for category, counts
            in category_grade.items()
        },
    }

    OUT_SUMMARY.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print("=" * 70)
    print("기존 상품 품질 재평가 완료")
    print("=" * 70)
    print(f"전체   : {len(results)}개")
    print(f"A급    : {grade_counter['A']}개")
    print(f"B급    : {grade_counter['B']}개")
    print(f"C급    : {grade_counter['C']}개")
    print(f"REJECT : {grade_counter['REJECT']}개")

    print()
    print("카테고리별")
    print("-" * 70)

    for category, counts in sorted(category_grade.items()):
        print(
            f"{category:8} "
            f"A={counts['A']:4} "
            f"B={counts['B']:4} "
            f"C={counts['C']:4} "
            f"REJECT={counts['REJECT']:4}"
        )

    print()
    print(f"JSON    : {OUT_JSON}")
    print(f"CSV     : {OUT_CSV}")
    print(f"SUMMARY : {OUT_SUMMARY}")


if __name__ == "__main__":
    main()
