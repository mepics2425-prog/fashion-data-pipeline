import os
import re
import json
import time
import html
import threading
from datetime import datetime, timezone
from pathlib import Path
from core.config import STATE_DIR as BASE_STATE_DIR, LOG_DIR as BASE_LOG_DIR, QUEUE_DIR, PROJECT_ROOT
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from loaders import loader
from core import cm29_base as base


# ============================================================
# 설정
# ============================================================

SEARCH_API = (
    "https://display-bff-api.29cm.co.kr"
    "/api/v1/listing/items"
    "?colorchipVariant=treatment"
)

PRODUCT_URL = (
    "https://www.29cm.co.kr/products/{}"
)

PRODUCT_URL_OLD = (
    "https://product.29cm.co.kr/catalog/{}"
)

WORKERS = 8

# 한 번에 후보 약 3000개 확보
DISCOVER_TARGET = 3000

# 검색 페이지는 계속 앞으로 이동
MAX_SEARCH_PAGE = 10

STATE_DIR = BASE_STATE_DIR
LOG_DIR = BASE_LOG_DIR

PAGE_STATE_FILE = (
    STATE_DIR / "29cm_http_pages.json"
)

RESULT_LOG = (
    LOG_DIR / "29cm_http_fast.jsonl"
)

OCR_PENDING = QUEUE_DIR / "29cm_ocr" / "pending"

OCR_DONE = QUEUE_DIR / "29cm_ocr" / "done"

for p in [
    STATE_DIR,
    LOG_DIR,
    OCR_PENDING,
]:
    p.mkdir(
        parents=True,
        exist_ok=True,
    )


KEYWORDS = [
    # 상의
    "여성 티셔츠",
    "여성 반팔티",
    "여성 긴팔티",
    "여성 셔츠",
    "여성 블라우스",
    "여성 니트",
    "여성 가디건",
    "여성 맨투맨",
    "여성 후드",
    "여성 베스트",

    # 아우터
    "여성 자켓",
    "여성 재킷",
    "여성 점퍼",
    "여성 코트",

    # 하의
    "여성 팬츠",
    "여성 슬랙스",
    "여성 데님",
    "여성 청바지",
    "여성 스커트",

    # 원피스
    "여성 원피스",
]


SEARCH_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://www.29cm.co.kr",
    "referer": "https://www.29cm.co.kr/",
    "user-agent":
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/142 Safari/537.36",
}


PAGE_HEADERS = {
    "accept":
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8",

    "accept-language":
        "ko-KR,ko;q=0.9,en;q=0.8",

    "user-agent":
        SEARCH_HEADERS["user-agent"],

    "referer":
        "https://www.29cm.co.kr/",
}


thread_local = threading.local()

# ============================================================
# 29CM SEARCH API용 Playwright BrowserContext
#
# 일반 requests.post는 29CM에서 403이 발생하므로
# 실제 브라우저 세션의 쿠키/컨텍스트를 공유하는
# context.request를 사용한다.
# ============================================================

_SEARCH_PW = None
_SEARCH_BROWSER = None
_SEARCH_CONTEXT = None


def get_29cm_search_context():

    global _SEARCH_PW
    global _SEARCH_BROWSER
    global _SEARCH_CONTEXT

    if _SEARCH_CONTEXT is not None:
        return _SEARCH_CONTEXT


    print(
        "🌐 29CM 브라우저 세션 생성..."
    )

    _SEARCH_PW = (
        sync_playwright()
        .start()
    )

    _SEARCH_BROWSER = (
        _SEARCH_PW.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
    )

    _SEARCH_CONTEXT = (
        _SEARCH_BROWSER.new_context(
            locale="ko-KR",

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/142.0.0.0 "
                "Safari/537.36"
            ),

            extra_http_headers={
                "Accept-Language":
                    "ko-KR,ko;q=0.9,en;q=0.8",
            },
        )
    )


    # --------------------------------------------------------
    # 29CM 홈페이지를 한 번 열어
    # 정상 브라우저 쿠키/세션 확보
    # --------------------------------------------------------

    page = (
        _SEARCH_CONTEXT
        .new_page()
    )

    try:

        page.goto(
            "https://www.29cm.co.kr/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        page.wait_for_timeout(
            1200
        )

        print(
            "✅ 29CM 브라우저 세션 준비"
        )

    except Exception as e:

        print(
            "⚠ 29CM 홈 초기화:"
            f" {type(e).__name__}: {e}"
        )

    finally:

        try:
            page.close()
        except Exception:
            pass


    return _SEARCH_CONTEXT





# ============================================================
# 공통
# ============================================================

def get_session():

    if not hasattr(
        thread_local,
        "session"
    ):
        session = requests.Session()
        session.headers.update(
            PAGE_HEADERS
        )

        thread_local.session = (
            session
        )

    return thread_local.session


def load_json(path, default):

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default


def save_json(path, data):

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(path)


def log_result(data):

    with RESULT_LOG.open(
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            json.dumps(
                data,
                ensure_ascii=False,
            )
            + "\n"
        )


def normalize_space(value):

    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


# ============================================================
# 검색 페이지 상태
# ============================================================

page_state = load_json(
    PAGE_STATE_FILE,
    {}
)


def take_next_page(keyword):

    current = int(
        page_state.get(
            keyword,
            1
        )
    )

    next_page = (
        current + 1
    )

    if next_page > MAX_SEARCH_PAGE:
        next_page = 1

    page_state[keyword] = (
        next_page
    )

    save_json(
        PAGE_STATE_FILE,
        page_state
    )

    return current


# ============================================================
# 기존 DB
# ============================================================

def load_existing():

    conn = loader.build_conn()

    result = {}

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    p.platform_product_id,
                    COUNT(ps.id)
                FROM products p
                LEFT JOIN product_sizes ps
                  ON ps.product_id = p.id
                WHERE p.platform = '29cm'
                GROUP BY
                    p.id,
                    p.platform_product_id
                """
            )

            for pid, count in cur.fetchall():

                result[
                    str(pid)
                ] = int(
                    count or 0
                )

    finally:
        conn.close()

    return result


# ============================================================
# 검색 API
# ============================================================




def search_keyword(
    keyword,
    page,
):

    payload = {
        "keyword":
            keyword,

        "pageType":
            "SRP",

        "sortType":
            "RECOMMENDED",

        "facets": {},

        "pageRequest": {
            "page":
                page,

            "size":
                50,
        },
    }


    try:

        context = (
            get_29cm_search_context()
        )


        response = (
            context.request.post(
                SEARCH_API,

                data=payload,

                headers={
                    "accept":
                        "*/*",

                    "content-type":
                        "application/json",

                    "origin":
                        "https://www.29cm.co.kr",

                    "referer":
                        "https://www.29cm.co.kr/",
                },

                timeout=15000,
            )
        )


        if response.status != 200:

            print(
                f"⚠ SEARCH "
                f"{keyword}"
                f" page={page}"
                f" HTTP {response.status}"
            )

            return []


        payload = (
            response.json()
        )


    except Exception as e:

        print(
            f"⚠ SEARCH ERROR"
            f" {keyword}"
            f" page={page}"
            f" | {type(e).__name__}: {e}"
        )

        return []


    data = (
        payload.get("data")
        or {}
    )

    items = (
        data.get("list")
        or []
    )


    result = []


    for item in items:

        if not isinstance(
            item,
            dict
        ):
            continue


        pid = (
            item.get("itemId")
            or item.get("productId")
            or item.get("id")
        )


        if not pid:
            continue


        info = (
            item.get("itemInfo")
            or {}
        )


        event = (
            item.get("itemEvent")
            or {}
        )


        props = (
            event.get(
                "eventProperties"
            )
            or {}
        )


        soldout = (
            props.get("isSoldout")
            or info.get("isSoldout")
            or False
        )


        if soldout:
            continue


        name = (
            info.get("productName")
            or props.get("itemName")
            or ""
        )


        brand = (
            props.get("brandName")
            or info.get("brandName")
            or ""
        )


        price = (
            props.get("price")
            or info.get("price")
        )


        categories = []


        for source in [
            props,
            info,
            item,
        ]:

            for key, value in (
                source.items()
            ):

                if (
                    "category"
                    in key.lower()
                    and value
                ):

                    categories.append(
                        str(value)
                    )


        result.append({
            "product_id":
                str(pid),

            "brand":
                brand,

            "product_name":
                name,

            "price":
                price,

            "keyword":
                keyword,

            "api_category":
                " ".join(
                    categories
                ),
        })


    return result



def discover_products(
    existing,
):

    import csv
    import os

    target_csv = Path(
        os.getenv(
            "CM29_TARGET_CSV",
            str(PROJECT_ROOT / "data" / "29cm_products_db_import_405.csv")
        )
    )

    target_limit = int(
        os.getenv(
            "CM29_TARGET_LIMIT",
            "0"
        )
        or 0
    )

    if not target_csv.exists():

        raise FileNotFoundError(
            f"타깃 CSV 없음: {target_csv}"
        )

    products = []

    with target_csv.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:

        reader = csv.DictReader(f)

        for row in reader:

            pid = str(
                row.get(
                    "platform_product_id"
                )
                or ""
            ).strip()

            if not pid:
                continue

            # 이미 실측 데이터가 있는 상품은 건너뜀
            if (
                existing.get(
                    pid,
                    0
                ) > 0
            ):
                continue

            main_category = str(
                row.get(
                    "main_category"
                )
                or ""
            ).strip()

            sub_category = str(
                row.get(
                    "sub_category"
                )
                or ""
            ).strip()

            item = {
                "product_id":
                    pid,

                "brand":
                    str(
                        row.get("brand")
                        or "UNKNOWN"
                    ).strip(),

                "product_name":
                    str(
                        row.get(
                            "product_name"
                        )
                        or ""
                    ).strip(),

                "api_category":
                    (
                        main_category
                        + " "
                        + sub_category
                    ).strip(),

                "product_url":
                    str(
                        row.get(
                            "product_url"
                        )
                        or ""
                    ).strip(),
            }

            products.append(
                item
            )

            if (
                target_limit > 0
                and
                len(products)
                >= target_limit
            ):
                break

    print(
        f"🎯 405 CSV 타깃 후보 "
        f"{len(products):,}개"
    )

    if target_limit:
        print(
            f"🧪 테스트 제한 "
            f"{target_limit}개"
        )

    return products


# ============================================================
# 문자열 escape 해제
# ============================================================

def decode_page_text(value):

    value = html.unescape(
        str(value or "")
    )

    # JSON 내부의 \uAC00 같은 문자열
    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m:
            chr(
                int(
                    m.group(1),
                    16
                )
            ),
        value,
    )

    value = value.replace(
        "\\/",
        "/"
    )

    value = value.replace(
        "\\n",
        "\n"
    )

    value = value.replace(
        "\\t",
        " "
    )

    return value


# ============================================================
# 실측 필드
# ============================================================

METRIC_RULES = [
    (
        [
            r"총\s*장",
            r"총\s*길이",
            r"기장",
            r"옷\s*길이",
            r"length",
        ],
        "total_length_cm",
    ),
    (
        [
            r"어깨",
            r"shoulder",
        ],
        "shoulder_cm",
    ),
    (
        [
            r"가슴\s*단면",
            r"가슴\s*너비",
            r"가슴\s*폭",
            r"가슴",
            r"chest",
            r"bust",
        ],
        "chest_flat_cm",
    ),
    (
        [
            r"소매\s*길이",
            r"소매\s*장",
            r"sleeve",
        ],
        "sleeve_length_cm",
    ),
    (
        [
            r"암홀",
            r"진동",
            r"armhole",
        ],
        "armhole_cm",
    ),
    (
        [
            r"소매\s*통",
            r"소매\s*너비",
            r"소매\s*폭",
        ],
        "sleeve_width_cm",
    ),
    (
        [
            r"허리\s*단면",
            r"허리\s*너비",
            r"허리\s*폭",
            r"허리",
            r"waist",
        ],
        "waist_flat_cm",
    ),
    (
        [
            r"힙\s*단면",
            r"엉덩이\s*단면",
            r"힙\s*너비",
            r"엉덩이",
            r"힙",
            r"hip",
        ],
        "hip_flat_cm",
    ),
    (
        [
            r"허벅지",
            r"thigh",
        ],
        "thigh_flat_cm",
    ),
    (
        [
            r"밑위",
            r"rise",
        ],
        "rise_cm",
    ),
    (
        [
            r"인심",
            r"inseam",
        ],
        "inseam_cm",
    ),
    (
        [
            r"밑단",
            r"hem",
        ],
        "hem_flat_cm",
    ),
]


SIZE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(XXS|XS|S|M|L|XL|XXL|2XL|3XL|"
    r"FREE|ONE\s*SIZE|OS)"
    r"(?![A-Za-z0-9])",
    re.I,
)


def normalize_size(value):

    value = (
        str(value)
        .upper()
        .strip()
    )

    if value in {
        "OS",
        "ONE SIZE",
    }:
        return "FREE"

    return value


def metric_key(text):

    text = str(
        text or ""
    )

    # 둘레는 단면으로 넣지 않음
    compact = re.sub(
        r"\s+",
        "",
        text.lower(),
    )

    if "가슴둘레" in compact:
        return (
            "chest_circumference_cm"
        )

    if "허리둘레" in compact:
        return (
            "waist_circumference_cm"
        )

    if (
        "힙둘레" in compact
        or "엉덩이둘레" in compact
    ):
        return (
            "hip_circumference_cm"
        )

    for patterns, key in (
        METRIC_RULES
    ):

        for pattern in patterns:

            if re.search(
                pattern,
                text,
                re.I,
            ):
                return key

    return None


def numbers(text):

    result = []

    for raw in re.findall(
        r"(?<!\d)-?\d+(?:\.\d+)?",
        str(text or ""),
    ):

        try:
            value = float(raw)

        except Exception:
            continue

        # 의류 실측으로 말이 되는 범위
        if 1 <= value <= 300:
            result.append(
                value
            )

    return result


# ============================================================
# HTML TABLE 파싱
# ============================================================

def parse_tables(soup):

    all_results = []

    for table in soup.find_all(
        "table"
    ):

        rows = []

        for tr in table.find_all(
            "tr"
        ):

            cells = [
                normalize_space(
                    x.get_text(
                        " ",
                        strip=True
                    )
                )
                for x in tr.find_all(
                    ["th", "td"]
                )
            ]

            if cells:
                rows.append(cells)

        if len(rows) < 2:
            continue


        # ----------------------------------------------------
        # 형태 A
        # 사이즈 | 총장 | 어깨 | 가슴
        # S      | 68  | 50  | 52
        # ----------------------------------------------------

        header = rows[0]

        keys = [
            metric_key(x)
            for x in header[1:]
        ]

        valid_keys = sum(
            1
            for x in keys
            if x
        )

        if valid_keys >= 2:

            parsed = []

            for row in rows[1:]:

                if len(row) < 2:
                    continue

                m = SIZE_RE.search(
                    row[0]
                )

                if not m:
                    continue

                size = normalize_size(
                    m.group(1)
                )

                result = {
                    "size_label":
                        size,

                    "measurement_type":
                        "garment_actual",
                }

                for key, cell in zip(
                    keys,
                    row[1:],
                ):

                    if not key:
                        continue

                    vals = numbers(
                        cell
                    )

                    if vals:
                        result[
                            key
                        ] = vals[0]

                if len(result) >= 4:
                    parsed.append(
                        result
                    )

            if parsed:
                return parsed


        # ----------------------------------------------------
        # 형태 B
        # 사이즈 | S | M | L
        # 총장   | 68|70 |72
        # 가슴   | 52|54 |56
        # ----------------------------------------------------

        labels = []

        for cell in header[1:]:

            m = SIZE_RE.search(
                cell
            )

            if m:
                labels.append(
                    normalize_size(
                        m.group(1)
                    )
                )

        if len(labels) >= 1:

            mapped = {
                label: {
                    "size_label":
                        label,

                    "measurement_type":
                        "garment_actual",
                }
                for label in labels
            }

            hit = 0

            for row in rows[1:]:

                if len(row) < 2:
                    continue

                key = metric_key(
                    row[0]
                )

                if not key:
                    continue

                vals = []

                for cell in row[1:]:

                    ns = numbers(
                        cell
                    )

                    vals.append(
                        ns[0]
                        if ns
                        else None
                    )

                if len(vals) < len(
                    labels
                ):
                    continue

                for label, value in zip(
                    labels,
                    vals,
                ):

                    if value is not None:

                        mapped[
                            label
                        ][key] = value

                hit += 1

            if hit >= 2:

                parsed = [
                    row
                    for row in (
                        mapped.values()
                    )
                    if len(row) >= 4
                ]

                if parsed:
                    return parsed

    return all_results


# ============================================================
# 일반 텍스트 파싱
# ============================================================

def parse_text(text):

    text = decode_page_text(
        text
    )

    # JSON/HTML이 한 줄일 때도
    # 주요 단어 앞에 강제 줄바꿈
    for word in [
        "총장",
        "총길이",
        "어깨",
        "가슴",
        "허리",
        "힙",
        "엉덩이",
        "허벅지",
        "소매",
        "암홀",
        "밑위",
        "밑단",
        "인심",
    ]:
        text = text.replace(
            word,
            "\n" + word
        )

    lines = [
        normalize_space(x)
        for x in text.splitlines()
        if normalize_space(x)
    ]


    # --------------------------------------------------------
    # 1. SIZE S M L / 총장 68 70 72 형태
    # --------------------------------------------------------

    for i, line in enumerate(lines):

        labels = [
            normalize_size(
                m.group(1)
            )
            for m in SIZE_RE.finditer(
                line
            )
        ]

        labels = list(
            dict.fromkeys(
                labels
            )
        )

        if len(labels) < 2:
            continue

        if not (
            "size" in line.lower()
            or "사이즈" in line
        ):
            continue

        rows = {
            label: {
                "size_label":
                    label,

                "measurement_type":
                    "garment_actual",
            }
            for label in labels
        }

        metric_hits = 0

        for detail in lines[
            i + 1:
            i + 18
        ]:

            key = metric_key(
                detail
            )

            if not key:
                continue

            vals = numbers(
                detail
            )

            if len(vals) < len(
                labels
            ):
                continue

            for label, value in zip(
                labels,
                vals[:len(labels)],
            ):

                rows[
                    label
                ][key] = value

            metric_hits += 1

        if metric_hits >= 2:

            result = [
                row
                for row in rows.values()
                if len(row) >= 4
            ]

            if result:
                return result


    # --------------------------------------------------------
    # 2. 한 행에
    # S 총장 68 어깨 50 가슴 52 ...
    # --------------------------------------------------------

    result = []

    for line in lines:

        m = SIZE_RE.search(
            line
        )

        if not m:
            continue

        size = normalize_size(
            m.group(1)
        )

        # 너무 긴 JSON 전체 줄 방지
        if len(line) > 1200:
            continue

        row = {
            "size_label":
                size,

            "measurement_type":
                "garment_actual",
        }

        for patterns, key in (
            METRIC_RULES
        ):

            for pattern in patterns:

                match = re.search(
                    pattern
                    + r".{0,30}?"
                    + r"(-?\d+(?:\.\d+)?)",
                    line,
                    re.I,
                )

                if match:

                    try:
                        value = float(
                            match.group(1)
                        )

                    except Exception:
                        continue

                    if 1 <= value <= 300:
                        row[
                            key
                        ] = value

                    break

        if len(row) >= 4:
            result.append(
                row
            )

    if result:
        return result


    # --------------------------------------------------------
    # 3. FREE / ONE SIZE
    # --------------------------------------------------------

    full = "\n".join(
        lines
    )

    free_match = re.search(
        r"\b(?:FREE|ONE\s*SIZE|OS)\b",
        full,
        re.I,
    )

    if free_match:

        # FREE 등장 근처만 본다.
        pos = free_match.start()

        window = full[
            max(0, pos - 1200):
            pos + 2500
        ]

        row = {
            "size_label":
                "FREE",

            "measurement_type":
                "garment_actual",
        }

        for patterns, key in (
            METRIC_RULES
        ):

            for pattern in patterns:

                m = re.search(
                    pattern
                    + r".{0,35}?"
                    + r"(-?\d+(?:\.\d+)?)",
                    window,
                    re.I,
                )

                if not m:
                    continue

                try:
                    value = float(
                        m.group(1)
                    )

                except Exception:
                    continue

                if 1 <= value <= 300:

                    row[
                        key
                    ] = value

                break

        # 최소 실측 2종 이상
        if len(row) >= 4:
            return [row]


    return []


# ============================================================
# 이미지 URL
# ============================================================

def extract_images(
    soup,
    raw_html,
):

    result = []
    seen = set()

    def add(url):

        if not url:
            return

        url = decode_page_text(
            url
        ).strip()

        # HTML/script escape 잔여물 제거
        url = url.rstrip("\\\\")

        if url.startswith("//"):
            url = "https:" + url

        if not url.startswith(
            ("http://", "https://")
        ):
            return

        if (
            "29cm"
            not in url.lower()
        ):
            return

        if any(
            x in url.lower()
            for x in [
                "logo",
                "icon",
                "favicon",
                "sprite",
                "avatar",
                "og_image",
            ]
        ):
            return

        if url in seen:
            return

        seen.add(url)

        result.append({
            "url":
                url,

            # OCR 워커가 순서를 보존하도록
            "y":
                len(result),

            "width":
                0,

            "height":
                0,
        })


    for img in soup.find_all(
        "img"
    ):

        for attr in [
            "src",
            "data-src",
            "data-original",
            "data-lazy-src",
        ]:

            add(
                img.get(attr)
            )


    escaped = decode_page_text(
        raw_html
    )

    for m in re.findall(
        r'https?://[^"\'\s<>]+',
        escaped
    ):

        low = m.lower()

        if (
            "29cm" in low
            and any(
                ext in low
                for ext in [
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    "image",
                ]
            )
        ):
            add(m)

    return result[:80]


# ============================================================
# OCR queue
# ============================================================

def enqueue_ocr(
    item,
    category,
    images,
):

    if not images:
        return False

    pid = item[
        "product_id"
    ]

    pending = (
        OCR_PENDING
        / f"{pid}.json"
    )

    done = (
        OCR_DONE
        / f"{pid}.json"
    )


    # 과거 done이 있어도 실측을 못 찾은 결과면 재검사 허용
    if done.exists():

        try:

            old = json.loads(
                done.read_text(
                    encoding="utf-8"
                )
            )

            if old.get(
                "quality_ok"
            ):
                return False

        except Exception:
            pass


    job = {
        "platform":
            "29cm",

        "platform_product_id":
            pid,

        "brand":
            item.get("brand"),

        "product_name":
            item.get(
                "product_name"
            ),

        "main_category":
            category,

        "price":
            item.get("price"),

        "product_url":
            PRODUCT_URL.format(
                pid
            ),

        "images":
            images,

        "queued_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source":
            "http_fast_fallback",
    }

    pending.write_text(
        json.dumps(
            job,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return True


# ============================================================
# 개별 상품 HTTP 파싱
# ============================================================

def fetch_product(item):

    pid = item[
        "product_id"
    ]

    session = get_session()

    # CSV/DB에 저장된 실제 상품 URL을 최우선 사용
    item_url = str(
        item.get("product_url")
        or ""
    ).strip()

    urls = [
        item_url,

        PRODUCT_URL.format(
            pid
        ),

        PRODUCT_URL_OLD.format(
            pid
        ),
    ]

    # 빈 URL / 중복 URL 제거
    urls = list(
        dict.fromkeys(
            url
            for url in urls
            if url
        )
    )

    last_error = None

    for url in urls:

        try:

            # --------------------------------------------------
            # 29CM Cloudflare 대응: Playwright로 상세페이지 로드
            # --------------------------------------------------
            from playwright.sync_api import sync_playwright

            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                        ],
                    )

                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/142.0.0.0 Safari/537.36"
                        ),
                        locale="ko-KR",
                        viewport={
                            "width": 1440,
                            "height": 1200,
                        },
                    )

                    page = context.new_page()

                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    page.wait_for_timeout(2500)

                    # 29CM 상세 영역 lazy-load 대응
                    for _ in range(15):
                        page.evaluate(
                            "window.scrollBy(0, 1200)"
                        )
                        page.wait_for_timeout(350)

                    page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                    page.wait_for_timeout(1500)

                    status = (
                        response.status
                        if response
                        else None
                    )

                    raw_html = page.content()

                    browser.close()

            except Exception as e:

                last_error = (
                    "PLAYWRIGHT_"
                    + type(e).__name__
                    + ": "
                    + str(e)
                )

                continue


            if status != 200:

                last_error = (
                    f"HTTP {status}"
                )

                continue

            if len(raw_html) < 500:

                last_error = (
                    "HTML too short"
                )

                continue

            soup = BeautifulSoup(
                raw_html,
                "html.parser"
            )


            # ----------------------------------------------
            # HTML TABLE 먼저
            # ----------------------------------------------

            sizes = parse_tables(
                soup
            )


            # ----------------------------------------------
            # 화면 텍스트 + script/hydration JSON
            # ----------------------------------------------

            visible = soup.get_text(
                "\n",
                strip=True,
            )

            script_text = "\n".join(
                script.get_text(
                    "\n",
                    strip=False
                )
                for script
                in soup.find_all(
                    "script"
                )
            )

            all_text = (
                visible
                + "\n"
                + script_text
                + "\n"
                + raw_html
            )


            if not sizes:

                sizes = parse_text(
                    all_text
                )


            category_text = (
                item.get(
                    "product_name",
                    ""
                )
                + "\n"
                + item.get(
                    "api_category",
                    ""
                )
                + "\n"
                + visible[:8000]
            )

            try:

                category = (
                    base.classify_category(
                        category_text
                    )
                )

            except Exception:

                category = "기타"


            quality_ok = False
            quality_reason = (
                "실측 없음"
            )

            if sizes:

                try:

                    quality_ok, quality_reason = (
                        base.passes_quality(
                            category,
                            sizes,
                        )
                    )

                except Exception as e:

                    quality_reason = (
                        "QUALITY_"
                        + type(e).__name__
                    )


            images = []

            if not quality_ok:

                images = extract_images(
                    soup,
                    raw_html,
                )


            return {
                "item":
                    item,

                "url":
                    url,

                "sizes":
                    sizes,

                "category":
                    category,

                "quality_ok":
                    quality_ok,

                "quality_reason":
                    quality_reason,

                "images":
                    images,

                "html_length":
                    len(raw_html),

                "error":
                    None,
            }


        except Exception as e:

            last_error = (
                f"{type(e).__name__}: {e}"
            )


    return {
        "item":
            item,

        "sizes":
            [],

        "category":
            "기타",

        "quality_ok":
            False,

        "quality_reason":
            "HTTP_FAIL",

        "images":
            [],

        "error":
            last_error,
    }


# ============================================================
# loader raw
# ============================================================

def make_raw(
    result,
):

    item = result[
        "item"
    ]

    sizes = result[
        "sizes"
    ]

    labels = []
    measurements = {}

    for row in sizes:

        label = str(
            row.get(
                "size_label"
            )
            or ""
        ).strip()

        if not label:
            continue

        labels.append(
            label
        )

        measurements[
            label
        ] = {
            key: value
            for key, value
            in row.items()
            if key not in {
                "size_label",
                "measurement_type",
            }
        }


    return {
        "source": {
            "platform":
                "29cm",
        },

        "collection": {
            "search_keyword":
                item.get(
                    "keyword"
                ),

            "collected_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),
        },

        "classification": {
            "gender":
                "여성",

            "main_category":
                result.get(
                    "category"
                )
                or "기타",

            "sub_category":
                None,
        },

        "product": {
            "product_id":
                item[
                    "product_id"
                ],

            "brand":
                item.get("brand")
                or "UNKNOWN",

            "product_name":
                item.get(
                    "product_name"
                )
                or (
                    "29CM "
                    + item[
                        "product_id"
                    ]
                ),

            "price":
                item.get("price"),

            "color":
                None,

            "product_url":
                PRODUCT_URL.format(
                    item[
                        "product_id"
                    ]
                ),
        },

        "garment_attributes": {
            "material":
                None,

            "fit":
                None,

            "stretch":
                None,

            "thickness":
                None,
        },

        "size_data": {
            "measurement_type":
                "garment_actual",

            "size_labels":
                labels,

            "measurements":
                measurements,
        },

        "media": {
            "images": [],
        },

        "model_info": [],
    }


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    existing = load_existing()

    print(
        f"DB 기존 29CM: "
        f"{len(existing):,}"
    )

    candidates = discover_products(
        existing
    )

    if not candidates:

        print(
            "새 후보 없음"
        )

        return


    print()
    print(
        f"🚀 HTTP 동시 파싱 시작"
        f" | workers={WORKERS}"
        f" | candidates="
        f"{len(candidates):,}"
    )


    accepted = 0
    queued = 0
    failed = 0
    checked = 0

    conn = loader.build_conn()

    try:

        with ThreadPoolExecutor(
            max_workers=WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    fetch_product,
                    item,
                ):
                item

                for item
                in candidates
            }


            for future in as_completed(
                futures
            ):

                checked += 1

                try:

                    result = (
                        future.result()
                    )

                except Exception as e:

                    failed += 1

                    print(
                        f"❌ FUTURE "
                        f"{type(e).__name__}"
                    )

                    continue


                item = result[
                    "item"
                ]

                pid = item[
                    "product_id"
                ]


                if result[
                    "quality_ok"
                ]:

                    try:

                        raw = make_raw(
                            result
                        )

                        loader.upsert_one(
                            conn,
                            raw,
                        )

                        accepted += 1

                        print(
                            f"✅ HTTP SIZE "
                            f"{pid}"
                            f" | {result['category']}"
                            f" | sizes="
                            f"{len(result['sizes'])}"
                        )

                    except Exception as e:

                        failed += 1

                        print(
                            f"❌ DB "
                            f"{pid}"
                            f" | "
                            f"{type(e).__name__}"
                            f": {e}"
                        )


                else:

                    print(
                        f"🔍 DEBUG {pid}"
                        f" | quality={result.get('quality_ok')}"
                        f" | reason={result.get('quality_reason')}"
                        f" | sizes={len(result.get('sizes') or [])}"
                        f" | images={len(result.get('images') or [])}"
                        f" | url={result.get('url')}"
                    )

                    if enqueue_ocr(
                        item,
                        result[
                            "category"
                        ],
                        result[
                            "images"
                        ],
                    ):

                        queued += 1


                log_result({
                    "time":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),

                    "product_id":
                        pid,

                    "brand":
                        item.get(
                            "brand"
                        ),

                    "name":
                        item.get(
                            "product_name"
                        ),

                    "category":
                        result.get(
                            "category"
                        ),

                    "quality_ok":
                        result.get(
                            "quality_ok"
                        ),

                    "reason":
                        result.get(
                            "quality_reason"
                        ),

                    "sizes":
                        len(
                            result.get(
                                "sizes"
                            )
                            or []
                        ),

                    "ocr_queued":
                        not result.get(
                            "quality_ok"
                        )
                        and bool(
                            result.get(
                                "images"
                            )
                        ),

                    "error":
                        result.get(
                            "error"
                        ),
                })


                if (
                    checked % 50
                    == 0
                ):

                    elapsed = (
                        time.time()
                        - started
                    )

                    print()
                    print(
                        "----- STATUS -----"
                    )

                    print(
                        f"checked="
                        f"{checked:,}"
                        f" | HTTP적재="
                        f"{accepted:,}"
                        f" | OCR큐="
                        f"{queued:,}"
                        f" | fail="
                        f"{failed:,}"
                        f" | "
                        f"{elapsed / 60:.1f}분"
                    )

                    print(
                        "------------------"
                    )
                    print()


    finally:

        conn.close()


    elapsed = (
        time.time()
        - started
    )

    print()
    print("=" * 80)

    print(
        f"✅ BATCH END"
        f" | checked="
        f"{checked:,}"
        f" | HTTP적재="
        f"{accepted:,}"
        f" | OCR큐="
        f"{queued:,}"
        f" | fail="
        f"{failed:,}"
        f" | "
        f"{elapsed / 60:.1f}분"
    )

    print("=" * 80)


if __name__ == "__main__":
    main()
