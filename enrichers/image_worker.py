import os
import re
import io
import sys
import hashlib
import argparse
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

import pymysql
import oci
from PIL import Image
from playwright.sync_api import sync_playwright


DEFAULT_BUCKET = "fashion-data"
MIN_WIDTH = 700
MIN_HEIGHT = 700
TARGET_WIDTH = 1200


# ============================================================
# DB
# ============================================================

def db_conn():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER"),
        password=os.environ.get("MYSQL_PASSWORD"),
        database=os.environ.get("MYSQL_DB", "fashion"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ============================================================
# OCI
# ============================================================

def oci_client():
    signer = (
        oci.auth.signers
        .InstancePrincipalsSecurityTokenSigner()
    )

    client = (
        oci.object_storage
        .ObjectStorageClient(
            {},
            signer=signer
        )
    )

    namespace = client.get_namespace().data

    return client, namespace


# ============================================================
# URL
# ============================================================

def change_width(url, width=TARGET_WIDTH):
    p = urlsplit(url)

    q = dict(
        parse_qsl(
            p.query,
            keep_blank_values=True
        )
    )

    q["w"] = str(width)

    return urlunsplit((
        p.scheme,
        p.netloc,
        p.path,
        urlencode(q),
        p.fragment,
    ))


def extension_from_content_type(content_type):
    value = (
        content_type
        .split(";")[0]
        .strip()
        .lower()
    )

    if value in (
        "image/jpeg",
        "image/jpg",
    ):
        return "jpg"

    if value == "image/png":
        return "png"

    if value == "image/webp":
        return "webp"

    if value == "image/gif":
        return "gif"

    return "jpg"


# ============================================================
# 대상 상품
# ============================================================

def get_targets(conn, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id,
                p.platform,
                p.platform_product_id,
                p.brand,
                p.product_name,
                p.product_url,
                p.main_category
            FROM products p
            WHERE NOT EXISTS (
                SELECT 1
                FROM product_images pi
                WHERE pi.product_id = p.id
            )
            ORDER BY p.id DESC
            LIMIT %s
            """,
            (limit,)
        )

        return cur.fetchall()


# ============================================================
# 왼쪽 공식 Thumbnail N 추출
# ============================================================

def extract_gallery_urls(page, product_id):

    page.evaluate(
        "window.scrollTo(0, 0)"
    )

    page.wait_for_timeout(400)

    locator = page.locator(
        'img[alt^="Thumbnail "]'
    )

    found = {}

    for i in range(locator.count()):

        img = locator.nth(i)

        try:
            if not img.is_visible():
                continue

            alt = (
                img.get_attribute("alt")
                or ""
            ).strip()

            m = re.fullmatch(
                r"Thumbnail\s*(\d+)",
                alt,
                re.I,
            )

            if not m:
                continue

            index = int(m.group(1))

            src = (
                img.get_attribute("src")
                or ""
            ).strip()

            if not src:
                continue

            # 다른 상품 사진 방지
            if str(product_id) not in src:
                continue

            if src.startswith("//"):
                src = "https:" + src

            found[index] = src

        except Exception:
            continue

    return [
        (index, found[index])
        for index in sorted(found)
    ]


# ============================================================
# 실제 이미지 검증
# ============================================================

def verify_image(
    context,
    product_url,
    product_id,
    raw_url,
):

    if str(product_id) not in raw_url:
        return None, "상품ID 불일치"

    high_url = change_width(
        raw_url,
        TARGET_WIDTH
    )

    try:
        response = context.request.get(
            high_url,
            timeout=30000,
            headers={
                "Referer": product_url
            },
        )
    except Exception as e:
        return None, f"HTTP 요청 오류: {e}"

    if response.status != 200:
        return None, f"HTTP {response.status}"

    content_type = (
        response.headers
        .get("content-type", "")
        .split(";")[0]
        .strip()
        .lower()
    )

    if not content_type.startswith("image/"):
        return None, (
            f"이미지가 아닌 응답: "
            f"{content_type}"
        )

    try:
        body = response.body()
    except Exception as e:
        return None, f"body 읽기 실패: {e}"

    if len(body) < 30000:
        return None, (
            f"파일 너무 작음: "
            f"{len(body):,} bytes"
        )

    try:
        image = Image.open(
            io.BytesIO(body)
        )

        width, height = image.size

    except Exception as e:
        return None, f"이미지 파싱 실패: {e}"

    if (
        width < MIN_WIDTH
        or
        height < MIN_HEIGHT
    ):
        return None, (
            f"해상도 부족: "
            f"{width}x{height}"
        )

    sha256 = hashlib.sha256(
        body
    ).hexdigest()

    ext = extension_from_content_type(
        content_type
    )

    return {
        "url": high_url,
        "body": body,
        "bytes": len(body),
        "width": width,
        "height": height,
        "sha256": sha256,
        "content_type": content_type,
        "ext": ext,
    }, None


# ============================================================
# OCI + DB 저장
# ============================================================

def save_product_images(
    conn,
    obj_client,
    namespace,
    bucket,
    product,
    images,
):

    product_db_id = product["id"]

    platform = (
        product.get("platform")
        or "musinsa"
    )

    product_id = str(
        product["platform_product_id"]
    )

    uploaded = []

    # 먼저 OCI
    for order, image in enumerate(
        images,
        start=1
    ):

        ext = image["ext"]

        object_key = (
            f"fashion/"
            f"{platform}/"
            f"{product_id}/"
            f"{order}.{ext}"
        )

        obj_client.put_object(
            namespace_name=namespace,
            bucket_name=bucket,
            object_name=object_key,
            put_object_body=image["body"],
            content_type=image["content_type"],
        )

        uploaded.append({
            "image_order": order,
            "object_key": object_key,
            "source_url": image["url"],
            "bytes": image["bytes"],
        })

    # OCI 전부 성공한 경우에만 DB
    try:
        with conn.cursor() as cur:

            # 0-image 상품만 대상으로 하지만
            # 혹시 경쟁 상태가 생겨도 중복방지
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM product_images
                WHERE product_id = %s
                """,
                (product_db_id,)
            )

            cnt = cur.fetchone()["cnt"]

            if cnt > 0:
                conn.rollback()

                return False, (
                    "저장 직전 확인 결과 "
                    "이미 이미지가 존재함"
                )

            for row in uploaded:

                cur.execute(
                    """
                    INSERT INTO product_images (
                        product_id,
                        image_order,
                        object_key,
                        source_url,
                        image_type,
                        bytes,
                        uploaded_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        NOW()
                    )
                    """,
                    (
                        product_db_id,
                        row["image_order"],
                        row["object_key"],
                        row["source_url"],
                        "official_gallery",
                        row["bytes"],
                    )
                )

        conn.commit()

        return True, None

    except Exception:
        conn.rollback()
        raise


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
    )

    args = parser.parse_args()

    conn = db_conn()

    targets = get_targets(
        conn,
        args.limit
    )

    print("=" * 80)
    print("MUSINSA IMAGE WORKER")
    print("=" * 80)

    print(
        f"이번 테스트 대상: "
        f"{len(targets)}개"
    )

    print(
        "조건: product_images 0장인 상품만"
    )

    print()

    obj_client, namespace = (
        oci_client()
    )

    print(
        f"OCI namespace={namespace}"
    )

    print(
        f"bucket={args.bucket}"
    )

    print()

    success_count = 0
    fail_count = 0

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1100,
            },
            locale="ko-KR",
        )

        page = context.new_page()

        for n, product in enumerate(
            targets,
            start=1
        ):

            product_id = str(
                product[
                    "platform_product_id"
                ]
            )

            product_url = (
                product["product_url"]
                or
                f"https://www.musinsa.com/products/{product_id}"
            )

            print()
            print("=" * 80)

            print(
                f"[{n}/{len(targets)}] "
                f"{product_id}"
            )

            print(
                f"{product['brand']} | "
                f"{product['product_name']}"
            )

            print(
                f"카테고리: "
                f"{product['main_category']}"
            )

            print(
                f"URL: {product_url}"
            )

            try:
                page.goto(
                    product_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                page.wait_for_timeout(
                    2200
                )

            except Exception as e:
                print(
                    f"❌ 페이지 접속 실패: {e}"
                )

                fail_count += 1
                continue

            # redirect 시 상품 ID 검증
            if product_id not in page.url:
                print(
                    "❌ 다른 상품 페이지로 이동 "
                    "→ 저장 안 함"
                )

                fail_count += 1
                continue

            gallery = extract_gallery_urls(
                page,
                product_id
            )

            print(
                f"🖼 왼쪽 공식 Thumbnail: "
                f"{len(gallery)}개"
            )

            if not gallery:
                print(
                    "❌ 공식 갤러리 없음"
                )

                fail_count += 1
                continue

            verified = []
            hashes = set()
            product_failed = False

            for pos, (
                thumb_index,
                raw_url
            ) in enumerate(
                gallery,
                start=1
            ):

                print(
                    f"  [{pos}/{len(gallery)}] "
                    f"Thumbnail {thumb_index}"
                )

                image, error = verify_image(
                    context,
                    product_url,
                    product_id,
                    raw_url,
                )

                if error:
                    print(
                        f"     ❌ {error}"
                    )

                    product_failed = True
                    break

                if (
                    image["sha256"]
                    in hashes
                ):
                    print(
                        "     ❌ 실제 파일 중복"
                    )

                    product_failed = True
                    break

                hashes.add(
                    image["sha256"]
                )

                verified.append(
                    image
                )

                print(
                    f"     ✅ "
                    f"{image['width']}x"
                    f"{image['height']} | "
                    f"{image['bytes']:,} bytes"
                )

            # 하나라도 틀리면 상품 전체 이미지 저장 안 함
            if (
                product_failed
                or
                len(verified)
                != len(gallery)
            ):
                print(
                    "❌ 갤러리 일부 검증 실패 "
                    "→ OCI/DB 저장 안 함"
                )

                fail_count += 1
                continue

            try:
                ok, error = (
                    save_product_images(
                        conn,
                        obj_client,
                        namespace,
                        args.bucket,
                        product,
                        verified,
                    )
                )

            except Exception as e:
                print(
                    f"❌ OCI/DB 저장 오류: {e}"
                )

                fail_count += 1
                continue

            if not ok:
                print(
                    f"⚠️ 저장 건너뜀: {error}"
                )

                fail_count += 1
                continue

            print(
                f"✅ 저장 완료: "
                f"{len(verified)}장"
            )

            success_count += 1

        browser.close()

    conn.close()

    print()
    print("=" * 80)
    print("IMAGE WORKER 완료")
    print("=" * 80)

    print(
        f"성공 상품: "
        f"{success_count}개"
    )

    print(
        f"실패/보류: "
        f"{fail_count}개"
    )

    print()
    print(
        "※ 기존 이미지가 있던 상품은 "
        "전혀 수정하지 않았음"
    )


if __name__ == "__main__":
    main()
