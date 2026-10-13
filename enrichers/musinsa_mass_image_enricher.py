#!/usr/bin/env python3

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from core import crawler
from loaders import loader


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0 Safari/537.36"
)


def load_products(path):

    products = {}
    path = Path(path)

    with path.open(
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            item = json.loads(line)

            pid = str(
                item.get(
                    "platform_product_id"
                ) or ""
            ).strip()

            if not pid:
                continue

            products[pid] = {
                "pid": pid,

                "url": (
                    item.get("product_url")
                    or
                    f"https://www.musinsa.com/products/{pid}"
                )
            }

    return list(
        products.values()
    )


def extract_image_urls_mass(
    page,
    product_json,
    page_url
):

    """
    예전 crawler.py 이미지 수집 방식 복원.

    1. JSON-LD image
    2. 상세페이지 DOM img
    3. 300x300 미만 제거
    4. 현재 상품과 무관한 이미지 제거
    5. 중복 제거
    """

    urls = []

    product_match = re.search(
        r"/products/(\d+)",
        page_url or ""
    )

    product_id = (
        product_match.group(1)
        if product_match
        else None
    )


    # ========================================================
    # 1. JSON-LD 이미지
    # ========================================================

    json_images = (
        product_json.get("image")
        if isinstance(product_json, dict)
        else None
    )


    if isinstance(
        json_images,
        str
    ):

        urls.append(
            json_images
        )


    elif isinstance(
        json_images,
        list
    ):

        for item in json_images:

            if isinstance(
                item,
                str
            ):

                urls.append(
                    item
                )

            elif isinstance(
                item,
                dict
            ):

                url = (
                    item.get("url")
                    or
                    item.get("contentUrl")
                )

                if url:
                    urls.append(
                        url
                    )


    # ========================================================
    # 2. DOM 이미지
    # ========================================================

    try:

        dom_images = (
            page.eval_on_selector_all(
                "img",
                """
                els =>
                els.map(img => ({
                    src:
                        img.currentSrc
                        || img.src
                        || '',

                    w:
                        img.naturalWidth
                        || 0,

                    h:
                        img.naturalHeight
                        || 0
                }))
                """
            )
        )

    except Exception:

        dom_images = []


    for item in dom_images:

        src = item.get(
            "src"
        )

        width = (
            item.get("w", 0)
            or 0
        )

        height = (
            item.get("h", 0)
            or 0
        )

        if (
            src
            and width >= 300
            and height >= 300
        ):

            urls.append(
                urljoin(
                    page_url,
                    src
                )
            )


    # ========================================================
    # 3. 현재 상품과 관계없는 이미지 제거
    # ========================================================

    cleaned = []

    for url in urls:

        if not url:
            continue

        if not (
            crawler
            .is_relevant_product_image_url(
                url,
                product_id
            )
        ):
            continue

        cleaned.append(
            url
        )


    # ========================================================
    # 4. 중복 제거
    # ========================================================

    return (
        crawler
        .unique_keep_order(
            cleaned
        )
    )


def save_images(
    conn,
    pid,
    image_urls
):

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT id
            FROM products
            WHERE platform='musinsa'
              AND platform_product_id=%s
            LIMIT 1
            """,
            (pid,)
        )

        row = cur.fetchone()

        if not row:
            return 0

        db_id = row[0]

        rows = []

        for index, url in enumerate(
            image_urls[
                :crawler.MAX_IMAGES
            ],
            start=1
        ):

            rows.append(
                (
                    db_id,
                    index,
                    url,
                    None,
                )
            )

        if not rows:
            return 0


        # 기존 행은 같은 image_order 기준 갱신.
        # 다른 이미지 행 삭제는 하지 않음.
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
                    VALUES(source_url),

                image_type =
                    COALESCE(
                        image_type,
                        VALUES(image_type)
                    )
            """,
            rows
        )

    conn.commit()

    return len(rows)


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--input",
        required=True
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=None
    )

    args = ap.parse_args()


    products = load_products(
        args.input
    )

    if args.limit:

        products = products[
            :args.limit
        ]


    print(
        f"대상 상품: {len(products)}개"
    )

    print(
        f"상품당 최대 이미지: "
        f"{crawler.MAX_IMAGES}장"
    )

    print(
        "이미지 추출: JSON-LD + 상세 DOM"
    )

    print(
        "기존 이미지 삭제: 없음"
    )


    conn = loader.build_conn()

    success = 0
    failed = 0
    no_image = 0
    total_images = 0


    try:

        with sync_playwright() as p:

            browser = (
                p.chromium.launch(
                    headless=True
                )
            )

            context = (
                browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ko-KR",

                    viewport={
                        "width": 1440,
                        "height": 1000
                    }
                )
            )

            page = (
                context.new_page()
            )


            for idx, product in enumerate(
                products,
                start=1
            ):

                pid = product["pid"]
                url = product["url"]

                try:

                    ok = (
                        crawler
                        .goto_product_fast(
                            page,
                            url
                        )
                    )

                    if not ok:

                        raise RuntimeError(
                            "상품페이지 진입 실패"
                        )


                    # lazy-load 이미지까지 나오게
                    # 기존 상품 수집기의 상세 로딩 함수 재사용
                    (
                        crawler
                        .load_product_content_after_size_pass(
                            page
                        )
                    )


                    product_json = (
                        crawler
                        .extract_json_ld_product(
                            page
                        )
                    )


                    image_urls = (
                        extract_image_urls_mass(
                            page,
                            product_json,
                            url
                        )
                    )


                    image_urls = (
                        image_urls[
                            :crawler.MAX_IMAGES
                        ]
                    )


                    count = save_images(
                        conn,
                        pid,
                        image_urls
                    )


                    if count == 0:

                        no_image += 1

                        print(
                            f"[{idx}/{len(products)}] "
                            f"⚠️ {pid} "
                            f"images=0",
                            flush=True
                        )

                    else:

                        success += 1
                        total_images += count

                        print(
                            f"[{idx}/{len(products)}] "
                            f"✅ {pid} "
                            f"images={count}",
                            flush=True
                        )


                except Exception as e:

                    conn.rollback()

                    failed += 1

                    print(
                        f"[{idx}/{len(products)}] "
                        f"❌ {pid}: {e}",
                        flush=True
                    )


            browser.close()


    finally:

        conn.close()


    print()
    print("=" * 60)

    print(
        f"이미지 저장 상품 : {success}"
    )

    print(
        f"이미지 0장 상품  : {no_image}"
    )

    print(
        f"실패 상품        : {failed}"
    )

    print(
        f"저장 이미지      : {total_images}"
    )

    if success:

        print(
            f"상품당 평균      : "
            f"{total_images / success:.2f}장"
        )

    print("=" * 60)


if __name__ == "__main__":
    main()
