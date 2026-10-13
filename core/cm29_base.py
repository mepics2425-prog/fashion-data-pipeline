import os
import re
import json
import random
import unicodedata
from pathlib import Path
from core.config import LOG_DIR, QUEUE_DIR
from difflib import SequenceMatcher
from urllib.parse import quote

import pymysql
from playwright.sync_api import sync_playwright


TARGET_ACCEPTED = 10
MAX_DISCOVER = 120

OUT_ACCEPTED = LOG_DIR / "29cm_test_accepted.json"
OUT_DUPLICATES = LOG_DIR / "29cm_test_duplicates.json"
OUT_REJECTED = LOG_DIR / "29cm_test_rejected.json"


SEARCH_KEYWORDS = [
    "슬랙스",
    "와이드 팬츠",
    "데님 팬츠",
    "팬츠",
    "스커트",
    "원피스",
    "셔츠",
    "블라우스",
    "니트",
    "반팔 티셔츠",
    "긴팔 티셔츠",
]


# ============================================================
# DB
# ============================================================

def db_conn():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("DE_USER") or os.environ.get("MYSQL_USER"),
        password=os.environ.get("DE_PASSWORD") or os.environ.get("MYSQL_PASSWORD"),
        database=os.environ.get("MYSQL_DB", "fashion"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_existing_products():
    conn = db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    platform,
                    platform_product_id,
                    brand,
                    product_name,
                    color
                FROM products
            """)

            return cur.fetchall()

    finally:
        conn.close()


# ============================================================
# 문자열 정규화
# ============================================================

def normalize_text(value):
    value = unicodedata.normalize(
        "NFKC",
        str(value or "")
    ).lower()

    noise = [
        "29cm 단독",
        "[29cm 단독]",
        "29cm",
        "단독",
        "exclusive",
        "new",
        "best",
        "sale",
    ]

    for word in noise:
        value = value.replace(word, " ")

    value = re.sub(
        r"[\[\](){}_/|,\-:]+",
        " ",
        value
    )

    value = re.sub(
        r"[^0-9a-z가-힣 ]+",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return value


# ============================================================
# 기존 상품 중복검사
# ============================================================

def find_duplicate(
    brand,
    product_name,
    existing_products,
):
    nb = normalize_text(brand)
    nn = normalize_text(product_name)

    if not nb or not nn:
        return None

    best = None
    best_score = 0.0

    for old in existing_products:

        ob = normalize_text(
            old.get("brand")
        )

        if not ob or ob != nb:
            continue

        on = normalize_text(
            old.get("product_name")
        )

        if not on:
            continue

        # 브랜드 + 정규화 상품명 완전 동일
        if nn == on:
            return {
                "type": "exact",
                "score": 1.0,
                "existing": old,
            }

        score = SequenceMatcher(
            None,
            nn,
            on
        ).ratio()

        if score > best_score:
            best_score = score
            best = old

    # 너무 비슷한 것도 신규로 넣지 않고 보류
    if best is not None and best_score >= 0.93:
        return {
            "type": "similar",
            "score": round(best_score, 4),
            "existing": best,
        }

    return None


# ============================================================
# 상품 ID
# ============================================================

def extract_product_id(url):
    patterns = [
        r"/products/(\d+)",
        r"/catalog/(\d+)",
    ]

    for pattern in patterns:
        m = re.search(
            pattern,
            url or "",
            re.I
        )

        if m:
            return m.group(1)

    return None


# ============================================================
# 검색 결과에서 상품 URL 발견
# ============================================================

def discover_products(page):
    found = {}
    keywords = list(SEARCH_KEYWORDS)
    random.shuffle(keywords)

    print("=" * 76)
    print("29CM 상품 후보 탐색")
    print("=" * 76)

    for keyword in keywords:

        if len(found) >= MAX_DISCOVER:
            break

        urls = [
            (
                "https://www.29cm.co.kr/"
                f"search?keyword={quote(keyword)}"
            ),
            (
                "https://shop.29cm.co.kr/"
                f"search?keyword={quote(keyword)}"
            ),
        ]

        print()
        print(f"🔎 {keyword}")

        before = len(found)

        for search_url in urls:

            try:
                page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )

                page.wait_for_timeout(1800)

                # 검색결과 더 불러오기
                for _ in range(3):
                    page.evaluate(
                        "window.scrollBy(0, document.body.scrollHeight)"
                    )
                    page.wait_for_timeout(500)

                hrefs = page.eval_on_selector_all(
                    "a[href]",
                    """
                    els => els
                        .map(a => a.href)
                        .filter(Boolean)
                    """
                )

            except Exception:
                continue

            for href in hrefs:

                pid = extract_product_id(
                    href
                )

                if not pid:
                    continue

                found[pid] = (
                    f"https://www.29cm.co.kr/products/{pid}"
                )

                if len(found) >= MAX_DISCOVER:
                    break

            # 첫 주소에서 이미 충분히 잡혔으면
            # 구형 search 주소는 열지 않음
            if len(found) > before:
                break

        print(
            f"   +{len(found) - before}개 "
            f"→ 현재 {len(found)}개"
        )

    print()
    print(
        f"✅ 후보 총 {len(found)}개"
    )

    return list(found.items())


# ============================================================
# JSON-LD
# ============================================================

def extract_jsonld(page):
    result = {}

    scripts = page.locator(
        'script[type="application/ld+json"]'
    )

    for i in range(scripts.count()):

        try:
            raw = scripts.nth(i).text_content()

            data = json.loads(
                raw
            )

        except Exception:
            continue

        items = (
            data
            if isinstance(data, list)
            else [data]
        )

        for item in items:

            if not isinstance(
                item,
                dict
            ):
                continue

            typ = item.get("@type")

            if typ == "Product":
                result = item
                return result

    return result


# ============================================================
# 브랜드
# ============================================================

def extract_brand(page, jsonld):

    brand = jsonld.get("brand")

    if isinstance(brand, dict):
        brand = brand.get("name")

    if isinstance(brand, str) and brand.strip():
        return brand.strip()

    selectors = [
        'a[href*="/brand/"]',
        'a[href*="brand"]',
    ]

    for selector in selectors:

        try:
            loc = page.locator(selector)

            for i in range(
                min(loc.count(), 10)
            ):
                text = (
                    loc.nth(i)
                    .inner_text()
                    .strip()
                )

                if 1 < len(text) < 50:
                    return text

        except Exception:
            pass

    return None


# ============================================================
# 상품명
# ============================================================

def extract_name(page, jsonld):

    name = jsonld.get("name")

    if isinstance(name, str) and name.strip():
        return name.strip()

    try:
        h1 = (
            page.locator("h1")
            .first
            .inner_text()
            .strip()
        )

        if h1:
            return h1

    except Exception:
        pass

    return None


# ============================================================
# 가격
# ============================================================

def extract_price(jsonld):

    offers = jsonld.get("offers")

    if isinstance(offers, dict):
        value = offers.get("price")

        try:
            return int(
                float(value)
            )
        except Exception:
            pass

    return None


# ============================================================
# 카테고리 판정
# ============================================================

def classify_category(body):

    text = body.lower()

    if "원피스" in text:
        return "원피스"

    if "스커트" in text:
        return "하의"

    if any(
        x in text
        for x in [
            "슬랙스",
            "팬츠",
            "바지",
            "데님",
            "쇼츠",
            "반바지",
        ]
    ):
        return "하의"

    if any(
        x in text
        for x in [
            "재킷",
            "자켓",
            "점퍼",
            "코트",
            "패딩",
            "블레이저",
        ]
    ):
        return "아우터"

    if any(
        x in text
        for x in [
            "티셔츠",
            "셔츠",
            "니트",
            "블라우스",
            "맨투맨",
            "후드",
            "가디건",
        ]
    ):
        return "상의"

    return "기타"


# ============================================================
# 실측 필드명 정규화
# ============================================================

def measurement_key(label):

    x = re.sub(
        r"\([^)]*\)",
        "",
        label or ""
    )

    x = x.replace(
        " ",
        ""
    ).lower()

    rules = [
        (
            ["총장", "총길이", "기장"],
            "total_length_cm",
        ),
        (
            ["어깨"],
            "shoulder_cm",
        ),
        (
            ["가슴"],
            "chest_flat_cm",
        ),
        (
            ["소매길이", "소매"],
            "sleeve_length_cm",
        ),
        (
            ["암홀"],
            "armhole_cm",
        ),
        (
            ["허리"],
            "waist_flat_cm",
        ),
        (
            ["엉덩이", "힙"],
            "hip_flat_cm",
        ),
        (
            ["허벅지"],
            "thigh_flat_cm",
        ),
        (
            ["밑위"],
            "rise_cm",
        ),
        (
            ["인심"],
            "inseam_cm",
        ),
        (
            ["밑단"],
            "hem_flat_cm",
        ),
    ]

    for words, key in rules:

        if any(
            word in x
            for word in words
        ):
            return key

    return None



# ============================================================
# 29CM 상품설명 펼치기
# ============================================================

def expand_29cm_description(page):

    """
    29CM은 상품설명 더보기를 눌러야
    아래 상세설명/사이즈 영역이 DOM에 나타나는 상품이 있음.
    """

    try:
        for _ in range(6):

            clicked = page.evaluate(
                """
                () => {
                    let count = 0;

                    for (
                        const el
                        of document.querySelectorAll(
                            'button, a, [role="button"]'
                        )
                    ) {
                        const text = (
                            el.innerText
                            || el.textContent
                            || ''
                        )
                        .replace(/\\s+/g, '');

                        if (
                            text.includes(
                                '상품설명더보기'
                            )
                        ) {
                            try {
                                el.click();
                                count += 1;
                            } catch (_) {}
                        }
                    }

                    return count;
                }
                """
            )

            page.evaluate(
                """
                () => window.scrollTo(
                    0,
                    document.body.scrollHeight
                )
                """
            )

            page.wait_for_timeout(450)

            if clicked == 0:
                # lazy load용으로 한번 더 아래로
                page.evaluate(
                    """
                    () => window.scrollBy(
                        0,
                        window.innerHeight
                    )
                    """
                )

        page.wait_for_timeout(500)

    except Exception:
        pass

    try:
        return page.locator(
            "body"
        ).inner_text(
            timeout=5000
        )

    except Exception:
        return ""


# ============================================================
# OCR 결과 → 실측 변환
# ============================================================

def parse_ocr_measurements(text):

    import re as _re

    if not text:
        return []

    text = text.replace(
        "\r",
        "\n"
    )

    lines = [
        _re.sub(
            r"\s+",
            " ",
            x
        ).strip()
        for x in text.splitlines()
        if x.strip()
    ]

    if not lines:
        return []


    # --------------------------------------------------------
    # 헤더에서 측정항목 순서 추출
    # --------------------------------------------------------

    patterns = [
        (
            r"총\s*(?:장|길이)|기장",
            "total_length_cm",
        ),
        (
            r"어깨(?:\s*(?:너비|폭))?",
            "shoulder_cm",
        ),
        (
            r"가슴(?:\s*(?:단면|너비|폭))?",
            "chest_flat_cm",
        ),
        (
            r"가슴\s*둘레",
            "chest_circumference_cm",
        ),
        (
            r"소매\s*(?:길이|장)",
            "sleeve_length_cm",
        ),
        (
            r"암홀|진동",
            "armhole_cm",
        ),
        (
            r"소매\s*(?:통|너비|폭)",
            "sleeve_width_cm",
        ),
        (
            r"허리(?:\s*(?:단면|너비|폭))?",
            "waist_flat_cm",
        ),
        (
            r"허리\s*둘레",
            "waist_circumference_cm",
        ),
        (
            r"(?:힙|엉덩이)(?:\s*(?:단면|너비|폭))?",
            "hip_flat_cm",
        ),
        (
            r"(?:힙|엉덩이)\s*둘레",
            "hip_circumference_cm",
        ),
        (
            r"허벅지(?:\s*(?:단면|너비|폭))?",
            "thigh_flat_cm",
        ),
        (
            r"밑위",
            "rise_cm",
        ),
        (
            r"인심|inseam",
            "inseam_cm",
        ),
        (
            r"밑단(?:\s*(?:단면|너비|폭))?",
            "hem_flat_cm",
        ),
    ]


    def find_keys(line):

        found = []

        for pattern, key in patterns:

            m = _re.search(
                pattern,
                line,
                flags=_re.I,
            )

            if m:
                found.append(
                    (
                        m.start(),
                        key,
                    )
                )

        found.sort(
            key=lambda x: x[0]
        )

        result = []

        used = set()

        for _, key in found:

            if key in used:
                continue

            used.add(key)
            result.append(key)

        return result


    def numbers(line):

        out = []

        for x in _re.findall(
            r"(?<!\d)-?\d+(?:\.\d+)?",
            line
        ):

            try:
                n = float(x)

                if 0 < n <= 300:
                    out.append(n)

            except Exception:
                pass

        return out


    SIZE_RE = _re.compile(
        r"(?<![A-Z0-9])"
        r"(XXS|XS|S|M|L|XL|XXL|2XL|3XL|"
        r"FREE|ONE\s*SIZE|OS|F)"
        r"(?![A-Z0-9])",
        flags=_re.I,
    )


    def size_labels(line):

        values = []

        for m in SIZE_RE.finditer(
            line.upper()
        ):

            label = (
                m.group(1)
                .upper()
                .replace(
                    "ONE SIZE",
                    "FREE"
                )
                .replace(
                    "OS",
                    "FREE"
                )
            )

            # F 단독은 FREE로
            if label == "F":
                label = "FREE"

            if label not in values:
                values.append(label)

        return values


    # ========================================================
    # A. 가로형
    #
    # 사이즈 총장 어깨 가슴 소매
    # S      68   50   52   21
    # M      70   53   56   23
    # ========================================================

    for idx, line in enumerate(lines):

        keys = find_keys(line)

        if len(keys) < 2:
            continue

        rows = []

        for row_line in lines[
            idx + 1:
            idx + 12
        ]:

            labels = size_labels(
                row_line
            )

            vals = numbers(
                row_line
            )

            if not labels:
                continue

            if len(vals) < len(keys):
                continue

            label = labels[0]

            row = {
                "measurement_type":
                    "garment_actual",

                "size_label":
                    label,
            }

            for key, value in zip(
                keys,
                vals[:len(keys)],
            ):
                row[key] = value

            if len(row) >= 4:
                rows.append(row)

        if rows:
            return rows


    # ========================================================
    # B. 세로형
    #
    # SIZE S M L
    # 총장 68 70 72
    # 가슴 52 56 58
    # ========================================================

    for idx, line in enumerate(lines):

        labels = size_labels(
            line
        )

        if len(labels) < 2:
            continue

        rows = {
            label: {
                "measurement_type":
                    "garment_actual",

                "size_label":
                    label,
            }
            for label in labels
        }

        found_measurement = False

        for row_line in lines[
            idx + 1:
            idx + 15
        ]:

            keys = find_keys(
                row_line
            )

            if not keys:
                continue

            vals = numbers(
                row_line
            )

            if len(vals) < len(labels):
                continue

            key = keys[0]

            for label, value in zip(
                labels,
                vals[:len(labels)],
            ):
                rows[label][key] = value

            found_measurement = True

        if found_measurement:

            out = [
                row
                for row in rows.values()
                if len(row) >= 4
            ]

            if out:
                return out


    # ========================================================
    # C. FREE / ONE SIZE 형태
    #
    # SIZE - FREE
    # 가슴 49.5
    # 밑단 50.5
    # 어깨 42
    # ...
    # ========================================================

    joined = " ".join(
        lines
    )

    free_detected = bool(
        _re.search(
            r"\b(?:FREE|ONE\s*SIZE|OS)\b",
            joined,
            flags=_re.I,
        )
    )

    if free_detected:

        row = {
            "measurement_type":
                "garment_actual",

            "size_label":
                "FREE",
        }

        for line in lines:

            keys = find_keys(
                line
            )

            vals = numbers(
                line
            )

            if not keys or not vals:
                continue

            # 라벨 줄에 있는 첫 유효 숫자 사용
            row[keys[0]] = vals[0]

        if len(row) >= 4:
            return [row]


    return []


# ============================================================
# 상세설명 이미지 후보
# ============================================================

def get_29cm_detail_images(page):

    try:

        images = page.evaluate(
            """
            () => {
                const h =
                    Math.max(
                        document.body.scrollHeight,
                        1
                    );

                return Array.from(
                    document.images
                ).map(
                    (img, i) => {
                        const r =
                            img.getBoundingClientRect();

                        return {
                            index: i,
                            url:
                                img.currentSrc
                                || img.src
                                || '',
                            width:
                                img.naturalWidth
                                || r.width
                                || 0,
                            height:
                                img.naturalHeight
                                || r.height
                                || 0,
                            y:
                                r.top
                                + window.scrollY,
                            pageHeight: h,
                        };
                    }
                );
            }
            """
        )

    except Exception:
        return []

    result = []

    seen = set()

    for x in images:

        url = str(
            x.get("url")
            or ""
        )

        if not url.startswith(
            ("http://", "https://")
        ):
            continue

        if url in seen:
            continue

        seen.add(url)

        w = float(
            x.get("width")
            or 0
        )

        h = float(
            x.get("height")
            or 0
        )

        y = float(
            x.get("y")
            or 0
        )

        ph = float(
            x.get("pageHeight")
            or 1
        )

        # 썸네일/아이콘 제거
        if w < 350 or h < 350:
            continue

        # 상세설명 아래쪽 이미지 우선
        # 또는 매우 긴 상세 이미지
        if (
            y < ph * 0.22
            and h < w * 1.7
        ):
            continue

        result.append(
            {
                "url": url,
                "width": w,
                "height": h,
                "y": y,
            }
        )

    # 페이지 아래쪽 + 긴 이미지 우선
    result.sort(
        key=lambda x: (
            x["y"],
            x["height"]
            / max(
                x["width"],
                1
            ),
        ),
        reverse=True,
    )

    return result[:8]


# ============================================================
# 상세 이미지 OCR
# ============================================================

def parse_measurement_images_ocr(page):

    import io

    try:
        import pytesseract

        from PIL import (
            Image,
            ImageOps,
        )

    except Exception as e:

        print(
            "⚠ OCR 모듈 없음:",
            type(e).__name__,
        )

        return []


    candidates = (
        get_29cm_detail_images(
            page
        )
    )

    if not candidates:
        return []


    keywords = (
        "사이즈",
        "size",
        "총장",
        "총길이",
        "가슴",
        "어깨",
        "허리",
        "힙",
        "엉덩이",
        "허벅지",
        "소매",
        "밑단",
        "암홀",
        "밑위",
    )


    for i, item in enumerate(
        candidates,
        start=1,
    ):

        url = item["url"]

        try:

            response = (
                page.context.request.get(
                    url,
                    timeout=12000,
                )
            )

            if response.status != 200:
                continue

            raw = response.body()

            if len(raw) < 10000:
                continue

            image = Image.open(
                io.BytesIO(raw)
            ).convert("RGB")


            # ----------------------------------------------
            # 상세페이지가 세로로 매우 긴 이미지면
            # 사이즈표가 아래쪽에 있는 경우가 많아서
            # 우선 하단 45%를 OCR.
            # ----------------------------------------------

            w, h = image.size

            if h > w * 2.0:

                top = int(
                    h * 0.55
                )

                image = image.crop(
                    (
                        0,
                        top,
                        w,
                        h,
                    )
                )


            # 너무 작으면 OCR 정확도 위해 확대
            w, h = image.size

            if w < 1100:

                ratio = (
                    1100
                    / float(w)
                )

                image = image.resize(
                    (
                        1100,
                        int(
                            h * ratio
                        ),
                    )
                )


            gray = ImageOps.grayscale(
                image
            )

            gray = ImageOps.autocontrast(
                gray
            )


            text = pytesseract.image_to_string(
                gray,
                lang="kor+eng",
                config="--psm 6",
            )


            low = text.lower()

            hit_count = sum(
                1
                for keyword in keywords
                if keyword.lower()
                in low
            )

            # 사이즈 관련 단어가 거의 없으면
            # 비싼 파싱 작업 없이 다음 이미지
            if hit_count < 2:
                continue


            sizes = (
                parse_ocr_measurements(
                    text
                )
            )

            if sizes:

                print(
                    f"🖼 OCR 실측 발견"
                    f" | image={i}"
                    f" | sizes={len(sizes)}"
                )

                return sizes


        except Exception as e:

            print(
                f"⚠ OCR image {i} 실패"
                f" | {type(e).__name__}"
            )

            continue


    return []




# ============================================================
# 29CM OCR 큐
# ============================================================

def collect_29cm_product_images(page):

    """
    OCR 워커에 넘길 상품 이미지 URL 수집.

    실제 OCR은 여기서 하지 않는다.
    큰 29CM 이미지는 전부 큐에 넘기고
    별도 OCR 서비스에서 분석한다.
    """

    try:
        # lazy image src가 세팅되도록 짧게 스크롤
        for _ in range(4):
            page.evaluate(
                """
                () => window.scrollBy(
                    0,
                    Math.max(
                        window.innerHeight * 1.5,
                        900
                    )
                )
                """
            )
            page.wait_for_timeout(180)

        rows = page.evaluate(
            """
            () => {
                return Array.from(
                    document.querySelectorAll('img')
                ).map((img, index) => {
                    const rect =
                        img.getBoundingClientRect();

                    return {
                        index: index,

                        url:
                            img.currentSrc
                            || img.src
                            || img.getAttribute('data-src')
                            || img.getAttribute('data-original')
                            || '',

                        width:
                            img.naturalWidth
                            || rect.width
                            || 0,

                        height:
                            img.naturalHeight
                            || rect.height
                            || 0,

                        alt:
                            img.alt
                            || '',

                        y:
                            rect.top
                            + window.scrollY
                    };
                });
            }
            """
        )

    except Exception:
        return []

    out = []
    seen = set()

    for row in rows:

        url = str(
            row.get("url")
            or ""
        ).strip()

        if not url:
            continue

        if url.startswith("//"):
            url = "https:" + url

        if not url.startswith(
            ("http://", "https://")
        ):
            continue

        low = url.lower()

        # 29CM 이미지 계열 위주
        if "29cm" not in low:
            continue

        # 사이트 UI / 아이콘 등 제거
        if any(
            x in low
            for x in [
                "logo",
                "icon",
                "favicon",
                "avatar",
                "profile",
                "sprite",
            ]
        ):
            continue

        if url in seen:
            continue

        seen.add(url)

        try:
            width = float(
                row.get("width")
                or 0
            )

            height = float(
                row.get("height")
                or 0
            )

        except Exception:
            width = 0
            height = 0

        # 지나치게 작은 UI 이미지 제거.
        # 실제 상품/상세 이미지는 대부분 이보다 큼.
        if (
            width
            and height
            and width < 280
            and height < 280
        ):
            continue

        out.append({
            "url": url,
            "width": width,
            "height": height,
            "alt": row.get("alt") or "",
            "y": row.get("y") or 0,
        })

    # 모든 관련 상품 이미지를 유지.
    # 사이트 이상 시 무한정 커지는 것만 방지.
    return out[:100]


def enqueue_29cm_ocr_job(
    product_id,
    brand,
    product_name,
    category,
    price,
    product_url,
    images,
):

    import json as _json
    from pathlib import Path
    from datetime import datetime as _datetime, timezone as _timezone

    if not images:
        return

    pid = str(product_id)

    root = QUEUE_DIR / "29cm_ocr"

    pending = (
        root
        / "pending"
        / f"{pid}.json"
    )

    done = (
        root
        / "done"
        / f"{pid}.json"
    )

    # 이미 OCR을 끝낸 상품은 다시 큐잉하지 않음.
    if done.exists():
        return

    root.joinpath(
        "pending"
    ).mkdir(
        parents=True,
        exist_ok=True,
    )

    old = {}

    if pending.exists():

        try:
            old = _json.loads(
                pending.read_text(
                    encoding="utf-8"
                )
            )

        except Exception:
            old = {}

    merged = {}

    for img in (
        old.get("images", [])
        + images
    ):

        url = str(
            img.get("url")
            or ""
        )

        if not url:
            continue

        merged[url] = img

    job = {
        "platform": "29cm",
        "platform_product_id": pid,
        "brand": brand,
        "product_name": product_name,
        "main_category": category,
        "price": price,
        "product_url": product_url,
        "images": list(
            merged.values()
        ),
        "queued_at": _datetime.now(
            _timezone.utc
        ).isoformat(),
    }

    pending.write_text(
        _json.dumps(
            job,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"📥 OCR QUEUE"
        f" | {pid}"
        f" | images={len(job['images'])}"
    )



# ============================================================
# 29CM 실측 파싱
# ============================================================

def parse_measurements(page, body):

    """
    29CM 실측 파싱

    1순위: HTML 사이즈 표
    2순위: 기존 텍스트형
       S - 총장 68 / 어깨 50 / 가슴 52 / 소매 21
    """

    sizes = []


    # ========================================================
    # 1. HTML TABLE 실측
    # ========================================================

    try:

        tables = page.evaluate(
            """
            () => {
                const result = [];

                for (
                    const table
                    of document.querySelectorAll('table')
                ) {

                    const rows = [];

                    for (
                        const tr
                        of table.querySelectorAll('tr')
                    ) {

                        const cells =
                            Array.from(
                                tr.querySelectorAll(
                                    'th, td'
                                )
                            )
                            .map(
                                x =>
                                    (x.innerText || '')
                                    .replace(/\\s+/g, ' ')
                                    .trim()
                            );

                        if (cells.length) {
                            rows.push(cells);
                        }
                    }

                    if (rows.length) {
                        result.push(rows);
                    }
                }

                return result;
            }
            """
        )

    except Exception:

        tables = []


    def numeric_value(value):

        m = re.search(
            r"-?\d+(?:\.\d+)?",
            str(value or "")
        )

        if not m:
            return None

        try:
            number = float(m.group(0))
        except Exception:
            return None

        if number <= 0:
            return None

        return number


    for table in tables:

        if len(table) < 2:
            continue

        header_index = None
        header_keys = None


        # -----------------------------------------------
        # header 찾기
        # -----------------------------------------------

        for row_index, row in enumerate(
            table[:5]
        ):

            keys = []

            recognized = 0
            has_size = False

            for cell in row:

                text = str(
                    cell or ""
                ).strip()

                compact = re.sub(
                    r"\s+",
                    "",
                    text
                ).lower()


                if (
                    "사이즈명" in compact
                    or compact in (
                        "size",
                        "사이즈"
                    )
                ):
                    keys.append(
                        "size_label"
                    )

                    has_size = True

                    continue


                key = measurement_key(
                    text
                )

                keys.append(key)

                if key:
                    recognized += 1


            if (
                has_size
                and recognized >= 2
            ):
                header_index = row_index
                header_keys = keys
                break


        if (
            header_index is None
            or not header_keys
        ):
            continue


        # -----------------------------------------------
        # data rows
        # -----------------------------------------------

        for row in table[
            header_index + 1:
        ]:

            if not row:
                continue

            size_label = None

            parsed = {
                "measurement_type":
                    "garment_actual"
            }


            for col_index, value in enumerate(
                row
            ):

                if col_index >= len(
                    header_keys
                ):
                    break


                key = header_keys[
                    col_index
                ]


                if key == "size_label":

                    label = re.sub(
                        r"\s+",
                        "",
                        str(
                            value or ""
                        )
                    ).upper()

                    if re.fullmatch(
                        r"(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|\d{2,3})",
                        label,
                        re.I,
                    ):
                        size_label = label

                    continue


                if not key:
                    continue


                number = numeric_value(
                    value
                )

                if number is not None:
                    parsed[key] = number


            if not size_label:
                continue


            parsed[
                "size_label"
            ] = size_label


            measurement_count = len([
                k
                for k in parsed
                if k.endswith("_cm")
            ])


            if measurement_count >= 2:

                sizes.append(
                    parsed
                )


    # ========================================================
    # 2. 기존 한 줄형 fallback
    # ========================================================

    row_pattern = re.compile(
        r"^(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|"
        r"\d{2,3})\s*-\s*(.+)$",
        re.I,
    )


    for raw_line in body.splitlines():

        line = re.sub(
            r"\s+",
            " ",
            raw_line
        ).strip()


        m = row_pattern.match(
            line
        )


        if not m:
            continue


        size_label = (
            m.group(1)
            .upper()
        )

        rest = m.group(2)


        row = {
            "size_label":
                size_label,

            "measurement_type":
                "garment_actual",
        }


        pieces = re.split(
            r"\s*/\s*",
            rest
        )


        for piece in pieces:

            numbers = re.findall(
                r"-?\d+(?:\.\d+)?",
                piece
            )


            if not numbers:
                continue


            value = float(
                numbers[-1]
            )


            if value <= 0:
                continue


            label = re.sub(
                r"-?\d+(?:\.\d+)?\s*$",
                "",
                piece
            ).strip()


            key = measurement_key(
                label
            )


            if key:
                row[key] = value


        measurement_count = len([
            k
            for k in row
            if k.endswith("_cm")
        ])


        if measurement_count >= 2:

            sizes.append(
                row
            )


    # ========================================================
    # 같은 사이즈 중복 제거
    # 더 많은 실측 필드가 있는 행 우선
    # ========================================================

    dedup = {}


    for row in sizes:

        label = row[
            "size_label"
        ]

        old = dedup.get(
            label
        )


        new_count = len([
            k
            for k in row
            if k.endswith("_cm")
        ])


        old_count = (
            len([
                k
                for k in old
                if k.endswith("_cm")
            ])
            if old
            else -1
        )


        if new_count > old_count:
            dedup[label] = row


    return list(
        dedup.values()
    )



# ============================================================
# 품질검사
# ============================================================

def passes_quality(
    category,
    sizes,
):

    if not sizes:
        return False, "실측 없음"

    fields = set()

    for row in sizes:
        fields.update(
            k
            for k in row
            if k.endswith("_cm")
        )

    chest = (
        "chest_flat_cm"
        in fields
    )

    total = (
        "total_length_cm"
        in fields
    )

    shoulder = (
        "shoulder_cm"
        in fields
    )

    sleeve = (
        "sleeve_length_cm"
        in fields
    )

    waist = (
        "waist_flat_cm"
        in fields
    )

    hip = (
        "hip_flat_cm"
        in fields
    )

    thigh = (
        "thigh_flat_cm"
        in fields
    )

    rise = (
        "rise_cm"
        in fields
    )

    inseam = (
        "inseam_cm"
        in fields
    )

    if category in (
        "상의",
        "아우터",
    ):
        ok = (
            chest
            and total
            and (
                shoulder
                or sleeve
            )
        )

    elif category == "원피스":
        ok = (
            chest
            and total
            and (
                waist
                or hip
                or shoulder
                or sleeve
            )
        )

    elif category == "하의":
        # 스커트/팬츠 공통으로 너무 강하게
        # 잡지 않기 위해 허리+총장을 최소 허용.
        # 팬츠 데이터가 풍부하면 아래 필드들도 같이 저장됨.
        ok = (
            waist
            and (
                total
                or inseam
                or rise
            )
        )

    else:
        ok = len(fields) >= 3

    if not ok:
        return (
            False,
            "핵심 실측 조합 부족: "
            + ",".join(sorted(fields))
        )

    return True, None


# ============================================================
# 한 상품 검사
# ============================================================

def inspect_product(
    page,
    product_id,
    url,
    existing_products,
):

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(
            2500
        )

    except Exception as e:
        return (
            "reject",
            {
                "product_id": product_id,
                "reason":
                    f"페이지 접속 실패: {e}",
            },
        )

    final_url = page.url

    if (
        str(product_id)
        not in final_url
    ):
        return (
            "reject",
            {
                "product_id": product_id,
                "reason": "상품 ID redirect 불일치",
            },
        )

    try:
        body = page.locator(
            "body"
        ).inner_text()
    except Exception:
        body = ""


    jsonld = extract_jsonld(
        page
    )

    name = extract_name(
        page,
        jsonld
    )

    brand = extract_brand(
        page,
        jsonld
    )

    if not name:
        return (
            "reject",
            {
                "product_id": product_id,
                "reason": "상품명 없음",
            },
        )

    if not brand:
        # 중복검사를 확실히 못 하는 상품은
        # 신규로 넣지 않고 보류.
        return (
            "reject",
            {
                "product_id": product_id,
                "product_name": name,
                "reason":
                    "브랜드 추출 실패 - 중복검사 불가",
            },
        )

    duplicate = find_duplicate(
        brand,
        name,
        existing_products,
    )

    if duplicate:
        return (
            "duplicate",
            {
                "product_id": product_id,
                "brand": brand,
                "product_name": name,
                "duplicate_type":
                    duplicate["type"],
                "duplicate_score":
                    duplicate["score"],
                "existing":
                    duplicate["existing"],
            },
        )
    # ========================================================
    # 29CM 상세설명 펼치기
    # ========================================================

    try:
        expanded_body = (
            expand_29cm_description(
                page
            )
        )
    except Exception:
        expanded_body = None

    if expanded_body:
        body = expanded_body

    category = classify_category(
        body
    )

    # ========================================================
    # OCR은 여기서 실행하지 않는다.
    #
    # 모든 상품 이미지 URL을 큐에 넘긴 뒤
    # 별도의 OCR 서비스가 전부 분석한다.
    # ========================================================

    try:
        ocr_images = (
            collect_29cm_product_images(
                page
            )
        )

        enqueue_29cm_ocr_job(
            product_id=product_id,
            brand=brand,
            product_name=name,
            category=category,
            price=extract_price(
                jsonld
            ),
            product_url=final_url,
            images=ocr_images,
        )

    except Exception as e:

        print(
            f"⚠ OCR QUEUE 실패"
            f" | {product_id}"
            f" | {type(e).__name__}"
        )

    # 빠른 HTML/TEXT 실측만 현재 수집기에서 처리
    sizes = parse_measurements(
        page,
        body
    )


    ok, reason = passes_quality(
        category,
        sizes,
    )

    if not ok:
        return (
            "reject",
            {
                "product_id": product_id,
                "brand": brand,
                "product_name": name,
                "category": category,
                "sizes": sizes,
                "reason": reason,
            },
        )

    result = {
        "platform": "29cm",
        "platform_product_id":
            str(product_id),
        "brand": brand,
        "product_name": name,
        "main_category":
            category,
        "price":
            extract_price(jsonld),
        "product_url":
            final_url,
        "sizes":
            sizes,
    }

    return (
        "accepted",
        result,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    existing = load_existing_products()

    print("=" * 76)
    print("29CM 신규 상품 10개 TEST")
    print("=" * 76)

    print(
        f"기존 DB 상품: "
        f"{len(existing)}개"
    )

    accepted = []
    duplicates = []
    rejected = []

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

        candidates = discover_products(
            page
        )

        for idx, (
            product_id,
            url
        ) in enumerate(
            candidates,
            start=1,
        ):

            if len(accepted) >= TARGET_ACCEPTED:
                break

            print()
            print("-" * 76)

            print(
                f"[{idx}/{len(candidates)}] "
                f"{product_id}"
            )

            # 29CM은 SPA라 이전 상품 DOM이 남는 경우가 있어
            # 상품마다 새 페이지를 사용한다.
            product_page = context.new_page()

            try:
                status, data = inspect_product(
                    product_page,
                    product_id,
                    url,
                    existing,
                )
            finally:
                product_page.close()

            if status == "accepted":

                accepted.append(
                    data
                )

                # 같은 실행에서 또 발견되는 것도
                # 중복으로 걸리게 임시 existing에 추가
                existing.append({
                    "platform": "29cm",
                    "platform_product_id":
                        data[
                            "platform_product_id"
                        ],
                    "brand":
                        data["brand"],
                    "product_name":
                        data["product_name"],
                    "color": None,
                })

                print(
                    f"✅ ACCEPT "
                    f"{data['brand']} | "
                    f"{data['product_name']}"
                )

                print(
                    f"   category="
                    f"{data['main_category']} | "
                    f"sizes={len(data['sizes'])}"
                )

                for row in data[
                    "sizes"
                ]:
                    print(
                        "   ",
                        row
                    )

            elif status == "duplicate":

                duplicates.append(
                    data
                )

                print(
                    "⏭ DUPLICATE | "
                    f"{data.get('brand')} | "
                    f"{data.get('product_name')}"
                )

                print(
                    f"   기존: "
                    f"{data['existing'].get('platform')} / "
                    f"{data['existing'].get('product_name')}"
                )

            else:

                rejected.append(
                    data
                )

                print(
                    "❌ REJECT | "
                    f"{data.get('reason')}"
                )

    OUT_ACCEPTED.write_text(
        json.dumps(
            accepted,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    OUT_DUPLICATES.write_text(
        json.dumps(
            duplicates,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    OUT_REJECTED.write_text(
        json.dumps(
            rejected,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 76)
    print("29CM TEST 완료")
    print("=" * 76)

    print(
        f"✅ 신규 품질통과 : "
        f"{len(accepted)}개"
    )

    print(
        f"⏭ 중복 제외      : "
        f"{len(duplicates)}개"
    )

    print(
        f"❌ 품질/추출 실패 : "
        f"{len(rejected)}개"
    )

    print()
    print(
        f"신규: {OUT_ACCEPTED}"
    )

    print(
        f"중복: {OUT_DUPLICATES}"
    )

    print(
        f"탈락: {OUT_REJECTED}"
    )

    print()
    print(
        "※ 아직 MySQL/OCI에는 아무것도 저장하지 않았음"
    )


if __name__ == "__main__":
    main()
