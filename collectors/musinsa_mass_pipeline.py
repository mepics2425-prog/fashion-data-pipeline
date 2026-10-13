#!/usr/bin/env python3

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from core.config import LOG_DIR
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from core import crawler
from loaders import loader
from loaders import musinsa_mass_loader as mass_loader


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0 Safari/537.36"
)

SEARCH_URL = (
    "https://www.musinsa.com/search/goods"
    "?keyword={keyword}"
)

SEARCH_KEYWORDS = [
    "여성 티셔츠",
    "여성 셔츠",
    "여성 니트",
    "여성 가디건",
    "여성 맨투맨",
    "여성 후드",
    "여성 블라우스",

    "여성 재킷",
    "여성 블레이저",
    "여성 집업",
    "여성 점퍼",
    "여성 바람막이",
    "여성 코트",
    "여성 패딩",
    "여성 베스트",

    "여성 데님 팬츠",
    "여성 슬랙스",
    "여성 와이드 팬츠",
    "여성 코튼 팬츠",
    "여성 카고 팬츠",
    "여성 조거 팬츠",
    "여성 스커트",
    "여성 원피스",

    "남성 티셔츠",
    "남성 셔츠",
    "남성 니트",
    "남성 가디건",
    "남성 맨투맨",
    "남성 후드",

    "남성 재킷",
    "남성 블레이저",
    "남성 집업",
    "남성 점퍼",
    "남성 바람막이",
    "남성 코트",
    "남성 패딩",
    "남성 베스트",

    "남성 데님 팬츠",
    "남성 슬랙스",
    "남성 와이드 팬츠",
    "남성 코튼 팬츠",
    "남성 카고 팬츠",
    "남성 조거 팬츠",
]


def first_value(data, *names):

    if not isinstance(data, dict):
        return None

    for name in names:

        value = data.get(name)

        if value not in (
            None,
            "",
            [],
            {},
        ):
            return value

    return None


def find_goods(obj):

    result = []

    if isinstance(obj, dict):

        goods_no = first_value(
            obj,
            "goodsNo",
            "goods_no",
        )

        name = first_value(
            obj,
            "goodsName",
            "goodsNm",
            "productName",
            "name",
        )

        if goods_no and name:
            result.append(obj)

        for value in obj.values():
            result.extend(
                find_goods(value)
            )

    elif isinstance(obj, list):

        for value in obj:
            result.extend(
                find_goods(value)
            )

    return result


def normalize_gender(value, fallback):

    value = (
        str(value).strip()
        if value is not None
        else ""
    )

    upper = value.upper()

    if upper in {
        "WOMEN",
        "WOMAN",
        "FEMALE",
        "F",
    }:
        return "여성"

    if upper in {
        "MEN",
        "MAN",
        "MALE",
        "M",
    }:
        return "남성"

    return value or fallback


def normalize_listing(item, keyword):

    pid = first_value(
        item,
        "goodsNo",
        "goods_no",
    )

    if not pid:
        return None

    pid = str(pid).strip()

    if not pid.isdigit():
        return None

    (
        keyword_gender,
        main_category,
        sub_category,
    ) = crawler.classify_keyword(
        keyword
    )

    product = {

        "platform":
            "musinsa",

        "platform_product_id":
            pid,

        "product_name":
            first_value(
                item,
                "goodsName",
                "goodsNm",
                "productName",
                "name",
            ),

        "brand":
            first_value(
                item,
                "brandName",
                "brandNm",
                "brand",
            ),

        "price":
            first_value(
                item,
                "finalPrice",
                "price",
                "salePrice",
                "normalPrice",
            ),

        "image_url":
            first_value(
                item,
                "thumbnail",
                "imageUrl",
                "thumbnailUrl",
                "image",
            ),

        "gender":
            normalize_gender(
                first_value(
                    item,
                    "displayGenderText",
                    "gender",
                ),
                keyword_gender,
            ),

        "main_category":
            main_category,

        "sub_category":
            sub_category,

        "category":
            sub_category,

        "search_keyword":
            keyword,

        "product_url":
            (
                "https://www.musinsa.com/"
                f"products/{pid}"
            ),

        "listing_raw":
            item,
    }

    return product


def load_existing_ids():

    conn = loader.build_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT platform_product_id
                FROM products
                WHERE platform = 'musinsa'
                """
            )

            return {
                str(row[0])
                for row in cur.fetchall()
                if row and row[0]
            }

    finally:
        conn.close()


def db_product_count():

    conn = loader.build_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*)
                FROM products
                WHERE platform = 'musinsa'
                """
            )

            return int(
                cur.fetchone()[0]
            )

    finally:
        conn.close()


def collect_candidates(
    page,
    existing_ids,
    candidate_target,
    per_keyword_limit,
):

    products = {}

    print()
    print("=" * 72)
    print("1. PLP 대량 후보 수집")
    print("=" * 72)

    for keyword in SEARCH_KEYWORDS:

        if len(products) >= candidate_target:
            break

        keyword_ids = set()
        api_pages = set()

        print()
        print(
            f"🔎 {keyword}",
            flush=True,
        )

        def handle_response(response):

            if len(products) >= candidate_target:
                return

            url = response.url

            if (
                "/api2/dp/v2/plp/goods"
                not in url
            ):
                return

            page_match = re.search(
                r"[?&]page=(\d+)",
                url,
            )

            api_page = (
                int(page_match.group(1))
                if page_match
                else None
            )

            if (
                api_page is not None
                and api_page in api_pages
            ):
                return

            try:

                if response.status != 200:
                    return

                payload = response.json()

            except Exception:
                return

            if api_page is not None:
                api_pages.add(api_page)

            goods = find_goods(
                payload
            )

            before = len(products)

            for item in goods:

                if (
                    len(products)
                    >= candidate_target
                ):
                    break

                if (
                    len(keyword_ids)
                    >= per_keyword_limit
                ):
                    break

                product = normalize_listing(
                    item,
                    keyword,
                )

                if not product:
                    continue

                pid = product[
                    "platform_product_id"
                ]

                if pid in existing_ids:
                    continue

                if pid in products:
                    continue

                products[pid] = product
                keyword_ids.add(pid)

            added = (
                len(products)
                - before
            )

            if added:

                print(
                    f"   📦 PLP page={api_page} "
                    f"+{added} "
                    f"| 검색어={len(keyword_ids)}/"
                    f"{per_keyword_limit} "
                    f"| 전체={len(products)}/"
                    f"{candidate_target}",
                    flush=True,
                )

        page.on(
            "response",
            handle_response,
        )

        try:

            url = SEARCH_URL.format(
                keyword=quote(keyword)
            )

            try:

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )

            except Exception as e:

                print(
                    f"   ⚠ 진입 실패: {e}",
                    flush=True,
                )

                continue

            page.wait_for_timeout(
                1000
            )

            stagnant = 0
            previous = len(products)

            for step in range(
                1,
                121,
            ):

                if (
                    len(products)
                    >= candidate_target
                    or len(keyword_ids)
                    >= per_keyword_limit
                ):
                    break

                try:

                    links = page.locator(
                        'a[href*="/products/"], '
                        'a[href*="/app/goods/"]'
                    )

                    link_count = (
                        links.count()
                    )

                    if link_count > 0:

                        links.nth(
                            link_count - 1
                        ).evaluate(
                            """
                            el =>
                            el.scrollIntoView({
                                behavior: 'instant',
                                block: 'center'
                            })
                            """
                        )

                    page.mouse.wheel(
                        0,
                        900,
                    )

                    page.wait_for_timeout(
                        700
                    )

                except Exception as e:

                    print(
                        f"   ⚠ scroll: {e}",
                        flush=True,
                    )

                    break

                now = len(products)

                added = (
                    now - previous
                )

                if added:
                    stagnant = 0
                else:
                    stagnant += 1

                previous = now

                if (
                    step == 1
                    or step % 5 == 0
                    or added > 0
                ):

                    max_page = (
                        max(api_pages)
                        if api_pages
                        else 1
                    )

                    print(
                        f"   STEP {step:03d} "
                        f"+{added} "
                        f"PLP={max_page} "
                        f"검색어={len(keyword_ids)}/"
                        f"{per_keyword_limit} "
                        f"전체={len(products)}/"
                        f"{candidate_target}",
                        flush=True,
                    )

                if stagnant == 7:

                    try:

                        page.mouse.wheel(
                            0,
                            -1400,
                        )

                        page.wait_for_timeout(
                            400
                        )

                        page.mouse.wheel(
                            0,
                            2500,
                        )

                        page.wait_for_timeout(
                            1200
                        )

                    except Exception:
                        pass

                if stagnant >= 15:
                    break

        finally:

            try:
                page.remove_listener(
                    "response",
                    handle_response,
                )
            except Exception:
                pass

        print(
            f"   ✅ {keyword}: "
            f"{len(keyword_ids)}개",
            flush=True,
        )

    print()
    print(
        f"✅ 신규 후보 총 "
        f"{len(products)}개"
    )

    return list(
        products.values()
    )


def fetch_actual_sizes(
    page,
    products,
    batch_size,
):

    print()
    print("=" * 72)
    print("2. actual-size 병렬 수집")
    print("=" * 72)

    for start in range(
        0,
        len(products),
        batch_size,
    ):

        chunk = products[
            start:
            start + batch_size
        ]

        ids = [
            x[
                "platform_product_id"
            ]
            for x in chunk
        ]

        try:

            results = page.evaluate(
                """
                async (ids) => {

                    return await Promise.all(
                        ids.map(
                            async (productId) => {

                                const url =
                                    `https://goods-detail.musinsa.com/api2/goods/${productId}/actual-size`;

                                try {

                                    const r =
                                        await fetch(
                                            url,
                                            {
                                                credentials:
                                                    "include"
                                            }
                                        );

                                    let data = null;

                                    try {
                                        data =
                                            await r.json();
                                    }
                                    catch (_) {}

                                    return {
                                        productId:
                                            String(productId),

                                        status:
                                            r.status,

                                        ok:
                                            r.ok,

                                        data:
                                            data
                                    };
                                }

                                catch (e) {

                                    return {
                                        productId:
                                            String(productId),

                                        status: 0,

                                        ok: false,

                                        error:
                                            String(e)
                                    };
                                }
                            }
                        )
                    );
                }
                """,
                ids,
            )

        except Exception as e:

            print(
                f"⚠ batch 실패: {e}",
                flush=True,
            )

            results = []

        result_map = {
            str(
                x.get("productId")
            ): x
            for x in (
                results or []
            )
        }

        for product in chunk:

            pid = product[
                "platform_product_id"
            ]

            result = (
                result_map.get(pid)
                or {}
            )

            raw = result.get(
                "data"
            )

            product[
                "actual_size_status"
            ] = result.get(
                "status"
            )

            product[
                "actual_size_raw"
            ] = raw

            try:

                parsed = (
                    crawler
                    .parse_actual_size_measurements(
                        raw
                    )
                )

            except Exception as e:

                product[
                    "actual_size_parse_error"
                ] = str(e)

                parsed = (
                    [],
                    {},
                )

            product[
                "actual_size_parsed"
            ] = parsed

            product[
                "has_actual_size"
            ] = bool(
                parsed
                and isinstance(
                    parsed,
                    (list, tuple)
                )
                and len(parsed) == 2
                and (
                    parsed[0]
                    or parsed[1]
                )
            )

        done = min(
            start + batch_size,
            len(products),
        )

        print(
            f"📏 {done}/{len(products)}",
            flush=True,
        )

    return products


def quality_ok(
    item,
    min_sizes,
    min_fields,
):

    parsed = item.get(
        "actual_size_parsed"
    )

    if (
        not isinstance(
            parsed,
            (list, tuple),
        )
        or len(parsed) != 2
    ):
        return False

    labels = (
        parsed[0]
        if isinstance(
            parsed[0],
            list,
        )
        else []
    )

    measurements = (
        parsed[1]
        if isinstance(
            parsed[1],
            dict,
        )
        else {}
    )

    if len(labels) < min_sizes:
        return False

    max_fields = 0

    for m in measurements.values():

        if not isinstance(m, dict):
            continue

        count = sum(
            1
            for value in m.values()
            if value not in (
                None,
                "",
                0,
                0.0,
                "0",
                "0.0",
            )
        )

        max_fields = max(
            max_fields,
            count,
        )

    return (
        max_fields
        >= min_fields
    )


def save_to_db(
    products,
    target,
    archive_path,
    min_sizes,
    min_fields,
):

    print()
    print("=" * 72)
    print("3. 안전 MySQL 적재")
    print("=" * 72)

    conn = loader.build_conn()

    saved = 0
    rejected = 0
    failed = 0

    archive_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        with archive_path.open(
            "a",
            encoding="utf-8",
        ) as archive:

            for index, item in enumerate(
                products,
                start=1,
            ):

                if saved >= target:
                    break

                if not quality_ok(
                    item,
                    min_sizes,
                    min_fields,
                ):

                    rejected += 1

                    continue

                try:

                    (
                        raw,
                        product,
                        sizes,
                        images,
                    ) = mass_loader.normalize_item(
                        item
                    )

                    (
                        db_id,
                        size_count,
                        cell_count,
                        image_count,
                    ) = mass_loader.safe_upsert(
                        conn,
                        raw,
                        product,
                        sizes,
                        images,
                    )

                    archive.write(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )

                    archive.flush()

                    saved += 1

                    print(
                        f"✅ [{saved}/{target}] "
                        f"pid={product['platform_product_id']} "
                        f"db_id={db_id} "
                        f"sizes={size_count} "
                        f"cells={cell_count} "
                        f"images={image_count}",
                        flush=True,
                    )

                except Exception as e:

                    conn.rollback()

                    failed += 1

                    print(
                        f"❌ [{index}] "
                        f"pid="
                        f"{item.get('platform_product_id')} "
                        f"{e}",
                        flush=True,
                    )

    finally:
        conn.close()

    return (
        saved,
        rejected,
        failed,
    )


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--target",
        type=int,
        default=100,
    )

    ap.add_argument(
        "--per-keyword",
        type=int,
        default=200,
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

    args = ap.parse_args()

    started = time.perf_counter()

    db_before = db_product_count()

    existing_ids = load_existing_ids()

    print("=" * 72)
    print("MUSINSA MASS PIPELINE")
    print("=" * 72)

    print(
        f"목표 신규 저장 : "
        f"{args.target}"
    )

    print(
        f"기존 무신사 DB : "
        f"{db_before}"
    )

    print(
        f"기존 ID 메모리 : "
        f"{len(existing_ids)}"
    )

    print(
        f"검색어당 후보  : "
        f"{args.per_keyword}"
    )

    print(
        f"실측 batch     : "
        f"{args.batch_size}"
    )

    print(
        f"통과 기준      : "
        f"size>={args.min_sizes}, "
        f"fields>={args.min_fields}"
    )

    # 약간 여유 있게 후보 확보
    candidate_target = (
        args.target
        + max(
            100,
            args.target // 5,
        )
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    archive = LOG_DIR / (
        ""
        f"musinsa_mass_saved_{timestamp}.jsonl"
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        page = context.new_page()

        products = collect_candidates(
            page,
            existing_ids,
            candidate_target,
            args.per_keyword,
        )

        if not products:

            print(
                "신규 후보 없음"
            )

            browser.close()

            return 0

        products = fetch_actual_sizes(
            page,
            products,
            args.batch_size,
        )

        (
            saved,
            rejected,
            failed,
        ) = save_to_db(
            products,
            args.target,
            archive,
            args.min_sizes,
            args.min_fields,
        )

        browser.close()

    db_after = db_product_count()

    elapsed = (
        time.perf_counter()
        - started
    )

    print()
    print("=" * 72)
    print("FINAL RESULT")
    print("=" * 72)

    print(
        f"DB 전       : {db_before}"
    )

    print(
        f"DB 후       : {db_after}"
    )

    print(
        f"DB 순증가   : "
        f"{db_after - db_before}"
    )

    print(
        f"저장 성공   : {saved}"
    )

    print(
        f"품질 탈락   : {rejected}"
    )

    print(
        f"저장 실패   : {failed}"
    )

    print(
        f"전체 시간   : "
        f"{elapsed:.1f}초"
    )

    if elapsed > 0:

        print(
            f"저장 속도   : "
            f"{saved / elapsed:.2f}개/초"
        )

    print(
        f"백업 JSONL  : {archive}"
    )

    print("=" * 72)


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
