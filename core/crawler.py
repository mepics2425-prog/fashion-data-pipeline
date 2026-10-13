import csv
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# 0. 기본 설정
# ============================================================

# 로컬 테스트에서는 E:\fashion-server\data, 서버에서는 /data 사용
DEFAULT_DATA_DIR = (
    Path(r"E:\fashion-server\data")
    if os.name == "nt"
    else Path("/data")
)

DATA_DIR = Path(
    os.getenv("FASHION_DATA_DIR", str(DEFAULT_DATA_DIR))
)
RAW_DIR = DATA_DIR / "raw"
IMAGE_DIR = DATA_DIR / "images"
LOG_DIR = DATA_DIR / "logs"
STATE_DIR = DATA_DIR / "state"

for directory in (RAW_DIR, IMAGE_DIR, LOG_DIR, STATE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# 기존 코드와 호환되는 이름. 서버에서는 원본 JSON 루트가 /data/raw 입니다.
BASE_DIR = RAW_DIR

# 서버에서는 TARGET_COUNT=0 이면 총량 제한 없이 후보를 계속 검사합니다.
# 테스트 시 MAX_NEW_PRODUCTS=5 처럼 이번 실행 신규 승인 수만 제한할 수 있습니다.
TARGET_COUNT = int(os.getenv("TARGET_COUNT", "0"))
MAX_NEW_PRODUCTS = int(os.getenv("MAX_NEW_PRODUCTS", "0"))

# 기본은 이미지 URL만 저장. 실제 파일은 image_uploader_v2.py가 OCI로 올립니다.
DOWNLOAD_IMAGES = os.getenv("DOWNLOAD_IMAGES", "false").strip().lower() in {"1", "true", "yes", "y"}

# SIZE-FIRST 통과 기준
MIN_SIZE_COUNT = 1
MIN_MEASUREMENT_FIELDS = 3

MAX_IMAGES = 30
MAX_LINKS_PER_QUERY = 150

SEARCH_SCROLL_STEPS = 12
PRODUCT_SCROLL_STEPS = 5

HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in {"1", "true", "yes", "y"}
PAGE_TIMEOUT_MS = 45_000

DELAY_MIN = 1.5
DELAY_MAX = 3.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0 Safari/537.36"
)


# ============================================================
# 저장 파일
# ============================================================

PROCESSED_URLS_PATH = STATE_DIR / "processed_urls.json"
RUN_STATS_PATH = LOG_DIR / "run_stats.json"
RUN_HISTORY_PATH = LOG_DIR / "run_history.json"

ALL_JSONL_PATH = RAW_DIR / "_all_size_first_products.jsonl"
ALL_CSV_PATH = RAW_DIR / "_all_size_first_products.csv"
SUMMARY_PATH = RAW_DIR / "_summary.json"


# ============================================================
# 1. 이번 추가 수집 검색어
#
# 기존 500개에서 부족했던 종류 중심
# 브랜드 우선 X
# ============================================================

SEARCH_KEYWORDS = ['와이드 팬츠', '데님 팬츠', '데님', '슬랙스', '코튼 팬츠', '카고 팬츠', '조거 팬츠', '트레이닝 팬츠', '팬츠', '바지', '쇼츠', '반바지', '스커트', '롱 스커트', '미니 스커트', '와이드 팬츠', '데님 팬츠', '데님', '슬랙스', '코튼 팬츠', '카고 팬츠', '조거 팬츠', '트레이닝 팬츠', '팬츠', '바지', '쇼츠', '반바지', '스커트', '롱 스커트', '미니 스커트', '와이드 팬츠', '데님 팬츠', '데님', '슬랙스', '코튼 팬츠', '카고 팬츠', '조거 팬츠', '트레이닝 팬츠', '팬츠', '바지', '쇼츠', '반바지', '스커트', '롱 스커트', '미니 스커트', '와이드 팬츠', '데님 팬츠', '데님', '슬랙스', '코튼 팬츠', '카고 팬츠', '조거 팬츠', '트레이닝 팬츠', '팬츠', '바지', '쇼츠', '반바지', '스커트', '롱 스커트', '미니 스커트', '와이드 팬츠', '데님 팬츠', '데님', '슬랙스', '코튼 팬츠', '카고 팬츠', '조거 팬츠', '트레이닝 팬츠', '팬츠', '바지', '쇼츠', '반바지', '스커트', '롱 스커트', '미니 스커트', '원피스', '롱 원피스', '미니 원피스', '셔츠 원피스', '니트 원피스', '원피스', '롱 원피스', '미니 원피스', '셔츠 원피스', '니트 원피스', '원피스', '롱 원피스', '미니 원피스', '셔츠 원피스', '니트 원피스', '원피스', '롱 원피스', '미니 원피스', '셔츠 원피스', '니트 원피스', '셔츠', '블라우스', '니트', '맨투맨', '스웨트셔츠', '후드', '후드티', '긴소매 티셔츠', '반소매 티셔츠', '티셔츠', '셔츠', '블라우스', '니트', '맨투맨', '스웨트셔츠', '후드', '후드티', '긴소매 티셔츠', '반소매 티셔츠', '티셔츠', '셔츠', '블라우스', '니트', '맨투맨', '스웨트셔츠', '후드', '후드티', '긴소매 티셔츠', '반소매 티셔츠', '티셔츠', '재킷', '자켓', '가디건']


MUSINSA_SEARCH_URL = (
    "https://www.musinsa.com/search/goods?gf=A&keyword={keyword}"
)


# ============================================================
# 2. 공통 함수
# ============================================================

def now_string():
    return datetime.now().isoformat(timespec="seconds")


def clean_text(value):

    if value is None:
        return None

    value = re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()

    return value or None


def to_number(value):

    if value is None:
        return None

    text = (
        str(value)
        .strip()
        .replace(",", "")
    )

    if text in {"", "-", "—", "N/A", "n/a"}:
        return None

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text
    )

    if not match:
        return None

    number = float(match.group())

    if number.is_integer():
        return int(number)

    return number


def unique_keep_order(values):

    result = []
    seen = set()

    for value in values:

        value = clean_text(value)

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)
        result.append(value)

    return result


def safe_name(value):

    value = clean_text(value) or "unknown"

    value = re.sub(
        r'[<>:"/\\|?*]+',
        "_",
        value
    )

    value = value.strip(" ._")

    return value[:100] or "unknown"


def sleep_polite():

    seconds = random.uniform(
        0.3,
        0.7
    )

    print(f"    {seconds:.1f}초 대기")

    time.sleep(seconds)


def format_duration(seconds):

    seconds = int(max(seconds or 0, 0))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}시간 {minutes}분 {secs}초"

    if minutes:
        return f"{minutes}분 {secs}초"

    return f"{secs}초"


def target_reached(current_total, accepted_this_run=0):
    if MAX_NEW_PRODUCTS > 0 and accepted_this_run >= MAX_NEW_PRODUCTS:
        return True
    if TARGET_COUNT > 0 and current_total >= TARGET_COUNT:
        return True
    return False


def target_display():
    parts = []
    parts.append(f"총 {TARGET_COUNT}개" if TARGET_COUNT > 0 else "총량 제한 없음")
    if MAX_NEW_PRODUCTS > 0:
        parts.append(f"이번 실행 신규 {MAX_NEW_PRODUCTS}개")
    return " / ".join(parts)


# ============================================================
# 3. JSON 안전 저장
# ============================================================

def load_json_file(path, default):

    try:

        if not path.exists():
            return default

        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default


def save_json_file(path, data):

    temp_path = Path(str(path) + ".tmp")

    temp_path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    temp_path.replace(path)


# ============================================================
# 4. 진행상황
# ============================================================

def load_processed_urls():

    data = load_json_file(
        PROCESSED_URLS_PATH,
        {}
    )

    return data if isinstance(data, dict) else {}


def save_processed_urls(data):

    save_json_file(
        PROCESSED_URLS_PATH,
        data
    )


def mark_processed(
    processed,
    url,
    product_id=None,
    status="processed",
    reason=None
):

    processed[url] = {

        "product_id":
            str(product_id)
            if product_id
            else None,

        "status":
            status,

        "reason":
            reason,

        "processed_at":
            now_string()
    }

    save_processed_urls(processed)


def load_run_history():

    data = load_json_file(
        RUN_HISTORY_PATH,
        []
    )

    return data if isinstance(data, list) else []


def save_current_run_stats(stats):

    save_json_file(
        RUN_STATS_PATH,
        stats
    )


def append_run_history(stats):

    history = load_run_history()

    history.append(stats)

    save_json_file(
        RUN_HISTORY_PATH,
        history
    )


# ============================================================
# 5. 카테고리
# ============================================================

def classify_keyword(keyword):

    keyword = keyword or ""

    if "여성" in keyword:
        gender = "여성"

    elif "남성" in keyword:
        gender = "남성"

    else:
        gender = "unknown"


    # 구체적인 표현을 위에,
    # 넓은 표현을 아래에 둔다.
    category_rules = [

        # ----------------------------------------------------
        # 원피스
        # ----------------------------------------------------
        ("셔츠 원피스", "원피스", "셔츠 원피스"),
        ("니트 원피스", "원피스", "니트 원피스"),
        ("롱 원피스", "원피스", "롱 원피스"),
        ("미니 원피스", "원피스", "미니 원피스"),
        ("원피스", "원피스", "원피스"),


        # ----------------------------------------------------
        # 하의 - 스커트
        # ----------------------------------------------------
        ("롱 스커트", "하의", "롱 스커트"),
        ("미니 스커트", "하의", "미니 스커트"),
        ("스커트", "하의", "스커트"),


        # ----------------------------------------------------
        # 하의 - 팬츠
        # ----------------------------------------------------
        ("와이드 팬츠", "하의", "와이드 팬츠"),
        ("데님 팬츠", "하의", "데님 팬츠"),
        ("코튼 팬츠", "하의", "코튼 팬츠"),
        ("카고 팬츠", "하의", "카고 팬츠"),
        ("조거 팬츠", "하의", "조거 팬츠"),
        ("트레이닝 팬츠", "하의", "트레이닝 팬츠"),

        ("슬랙스", "하의", "슬랙스"),

        ("반바지", "하의", "반바지"),
        ("쇼츠", "하의", "쇼츠"),

        ("데님", "하의", "데님 팬츠"),
        ("팬츠", "하의", "팬츠"),
        ("바지", "하의", "팬츠"),


        # ----------------------------------------------------
        # 아우터
        # ----------------------------------------------------
        ("블레이저", "아우터", "블레이저"),
        ("블레이져", "아우터", "블레이저"),
        ("바람막이", "아우터", "바람막이"),
        ("패딩", "아우터", "패딩"),
        ("코트", "아우터", "코트"),
        ("재킷", "아우터", "재킷"),
        ("자켓", "아우터", "재킷"),
        ("점퍼", "아우터", "점퍼"),
        ("집업", "아우터", "집업"),
        ("베스트", "아우터", "베스트"),


        # ----------------------------------------------------
        # 상의
        # ----------------------------------------------------
        ("긴소매 티셔츠", "상의", "긴소매 티셔츠"),
        ("긴팔 티셔츠", "상의", "긴소매 티셔츠"),
        ("반소매 티셔츠", "상의", "반소매 티셔츠"),
        ("반팔 티셔츠", "상의", "반소매 티셔츠"),

        ("스웨트셔츠", "상의", "스웨트셔츠"),
        ("맨투맨", "상의", "맨투맨"),

        ("후드티", "상의", "후드"),
        ("후드", "상의", "후드"),

        ("블라우스", "상의", "블라우스"),
        ("가디건", "상의", "가디건"),
        ("니트", "상의", "니트"),

        # 셔츠보다 티셔츠를 먼저 검사해야
        # '티셔츠'가 그냥 '셔츠'로 잡히지 않는다.
        ("티셔츠", "상의", "티셔츠"),
        ("셔츠", "상의", "셔츠"),
    ]


    for (
        word,
        main_category,
        sub_category

    ) in category_rules:

        if word in keyword:

            return (
                gender,
                main_category,
                sub_category
            )


    return (
        gender,
        "기타",
        keyword or "unknown"
    )



# ============================================================
# CLOTHING_ONLY_GUARD_V1
# 자동 수집기는 의류만 허용
# 가방 / 신발 / 액세서리 등은 AI 인터뷰 수집과 분리
# ============================================================

ALLOWED_CLOTHING_MAIN_CATEGORIES = {
    "상의",
    "하의",
    "아우터",
    "원피스",
}

NON_CLOTHING_PRODUCT_WORDS = [
    # 가방
    "가방",
    "백팩",
    "크로스백",
    "숄더백",
    "토트백",
    "메신저백",
    "보스턴백",
    "더플백",
    "웨이스트백",
    "에코백",
    "클러치백",
    "미니백",
    "파우치",

    # 신발
    "신발",
    "슈즈",
    "스니커즈",
    "운동화",
    "로퍼",
    "샌들",
    "슬리퍼",
    "구두",
    "플랫슈즈",
    "플랫 슈즈",
    "클로그",
    "레인부츠",
    "워커",
    "하이힐",
    "펌프스",
    "뮬",

    # 기타 패션잡화
    "모자",
    "볼캡",
    "버킷햇",
    "비니",
    "벨트",
    "지갑",
    "카드지갑",
    "키링",
    "목걸이",
    "팔찌",
    "반지",
    "귀걸이",
    "선글라스",
    "안경",
    "스카프",
    "머플러",
    "장갑",
    "양말",
]


def clothing_only_guard(
    product_name,
    product_json,
    search_keyword
):
    # 1차: 우리가 지정한 검색 카테고리부터 의류인지 확인
    (
        _gender,
        main_category,
        _sub_category
    ) = classify_keyword(search_keyword)

    if main_category not in ALLOWED_CLOTHING_MAIN_CATEGORIES:
        return (
            False,
            f"비의류 검색 카테고리 차단: {main_category}"
        )

    # 2차: 실제 상품명 + 사이트 JSON-LD category 검사
    source_category = None

    if isinstance(product_json, dict):
        source_category = clean_text(
            product_json.get("category")
        )

    check_text = " ".join(
        str(x)
        for x in [
            product_name,
            source_category,
        ]
        if x
    ).lower()

    for word in NON_CLOTHING_PRODUCT_WORDS:
        if word.lower() in check_text:
            return (
                False,
                f"비의류 상품 차단: {word}"
            )

    # '부츠컷 팬츠'는 의류이므로 단순 '부츠' 문자열은 차단하지 않음.
    # 실제 부츠류는 레인부츠/워커/슈즈 등 위 키워드와 사이트 category로 차단.

    return True, "의류 상품"


# ============================================================
# 6. 페이지 이동
# ============================================================

def get_page_text(page):

    try:

        return page.locator(
            "body"
        ).inner_text(
            timeout=2000
        )

    except Exception:
        return ""


def goto_page(
    page,
    url,
    scroll_steps=PRODUCT_SCROLL_STEPS
):

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        page.wait_for_timeout(300)

        for _ in range(scroll_steps):

            page.mouse.wheel(
                0,
                1600
            )

            page.wait_for_timeout(120)


        text = get_page_text(page).lower()

        blocked_words = [

            "access denied",
            "you don't have permission",
            "captcha",
            "비정상적인 접근",
        ]

        if any(
            word in text
            for word in blocked_words
        ):

            print("    접근 제한 페이지 감지")

            return False


        return True


    except PlaywrightTimeoutError:

        print(f"    타임아웃: {url}")

        return False


    except Exception as e:

        print(f"    페이지 오류: {e}")

        return False


# ============================================================
# 7. 상품 후보 탐색
# ============================================================

def collect_product_links(
    page,
    existing_ids=None,
    candidate_target=1000
):

    existing_ids = {
        str(x)
        for x in (existing_ids or set())
    }

    candidate_target = max(
        1,
        int(candidate_target or 1000)
    )

    # 환경변수로 필요할 때 강제로 더 깊게 탐색 가능
    env_target = os.getenv(
        "MUSINSA_CANDIDATE_TARGET"
    )

    if env_target:
        try:
            candidate_target = max(
                candidate_target,
                int(env_target)
            )
        except Exception:
            pass

    # 한 검색어가 전체 후보를 독식하지 않도록 제한
    try:
        per_keyword_limit = max(
            1,
            int(
                os.getenv(
                    "MUSINSA_PER_KEYWORD_LIMIT",
                    "200"
                )
            )
        )
    except Exception:
        per_keyword_limit = 200

    all_candidates = []
    seen = set()

    keywords = list(
        dict.fromkeys(SEARCH_KEYWORDS)
    )

    random.shuffle(keywords)

    # 60개 단위 PLP 응답을 충분히 깊게 받을 수 있도록
    MAX_SCROLL_ROUNDS = 180

    # 스크롤 트리거가 잠시 멈추는 경우가 있으므로
    # 기존보다 여유 있게 둔다.
    STAGNANT_LIMIT = 14

    print(
        f"[PLP 깊은 후보 탐색] "
        f"DB에 없는 상품 {candidate_target}개 목표",
        flush=True
    )

    print(
        f"검색어 중복 제거 후: {len(keywords)}개 / "
        f"검색어당 최대 {per_keyword_limit}개",
        flush=True
    )

    def extract_goods_ids(obj):

        result = []

        if isinstance(obj, dict):

            for key, value in obj.items():

                if key in (
                    "goodsNo",
                    "goods_no",
                    "goodsId",
                    "goods_id",
                ):
                    if value is not None:

                        product_id = str(value)

                        if product_id.isdigit():
                            result.append(product_id)

                result.extend(
                    extract_goods_ids(value)
                )

        elif isinstance(obj, list):

            for item in obj:
                result.extend(
                    extract_goods_ids(item)
                )

        return result

    def add_product_id(
        product_id,
        keyword,
        keyword_ids
    ):

        if len(all_candidates) >= candidate_target:
            return False

        if len(keyword_ids) >= per_keyword_limit:
            return False

        product_id = str(product_id).strip()

        if not product_id.isdigit():
            return False

        # 기존 DB 승인 상품 제외
        if product_id in existing_ids:
            return False

        url = (
            "https://www.musinsa.com/"
            f"products/{product_id}"
        )

        if url in seen:
            return False

        seen.add(url)
        keyword_ids.add(product_id)

        all_candidates.append({
            "product_id_hint": product_id,
            "url": url,
            "search_keyword": keyword
        })

        return True

    for keyword in keywords:

        if len(all_candidates) >= candidate_target:
            break

        search_url = MUSINSA_SEARCH_URL.format(
            keyword=quote(keyword)
        )

        print(
            f"🔎 {keyword}",
            flush=True
        )

        keyword_ids = set()
        api_pages = set()

        def handle_response(response):

            url = response.url

            if (
                "/api2/dp/v2/plp/goods"
                not in url
            ):
                return

            page_match = re.search(
                r"[?&]page=(\d+)",
                url
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

                data = response.json()

            except Exception:
                return

            if api_page is not None:
                api_pages.add(api_page)

            ids = extract_goods_ids(data)

            before = len(all_candidates)

            for product_id in ids:

                add_product_id(
                    product_id,
                    keyword,
                    keyword_ids
                )

                if (
                    len(all_candidates)
                    >= candidate_target
                    or len(keyword_ids)
                    >= per_keyword_limit
                ):
                    break

            added = (
                len(all_candidates) - before
            )

            if added > 0:

                print(
                    f"   PLP page={api_page} "
                    f"+{added}개 "
                    f"(검색어 {len(keyword_ids)}/"
                    f"{per_keyword_limit}, "
                    f"전체 {len(all_candidates)}/"
                    f"{candidate_target})",
                    flush=True
                )

        page.on(
            "response",
            handle_response
        )

        try:

            if not goto_page(
                page,
                search_url,
                scroll_steps=0
            ):
                continue

            # ---------------------------------------------
            # 첫 화면 SSR/DOM 상품 확보
            # ---------------------------------------------

            try:

                hrefs = page.eval_on_selector_all(
                    "a[href]",
                    """
                    els =>
                    els
                    .map(a => a.href)
                    .filter(Boolean)
                    """
                )

            except Exception:
                hrefs = []

            before_dom = len(all_candidates)

            for href in hrefs:

                match = (
                    re.search(
                        r"https?://(?:www\.)?"
                        r"musinsa\.com/products/(\d+)",
                        href or "",
                        re.I
                    )
                    or
                    re.search(
                        r"https?://(?:www\.)?"
                        r"musinsa\.com/app/goods/(\d+)",
                        href or "",
                        re.I
                    )
                )

                if not match:
                    continue

                add_product_id(
                    match.group(1),
                    keyword,
                    keyword_ids
                )

                if (
                    len(all_candidates)
                    >= candidate_target
                    or len(keyword_ids)
                    >= per_keyword_limit
                ):
                    break

            print(
                f"   첫 화면 DOM "
                f"+{len(all_candidates) - before_dom}개",
                flush=True
            )

            stagnant_rounds = 0

            # ---------------------------------------------
            # 실제 무신사 무한스크롤
            # PLP page=2,3,4... 응답을 listener가 수집
            # ---------------------------------------------

            for round_no in range(
                1,
                MAX_SCROLL_ROUNDS + 1
            ):

                if (
                    len(all_candidates)
                    >= candidate_target
                    or len(keyword_ids)
                    >= per_keyword_limit
                ):
                    break

                before_round = len(all_candidates)

                try:

                    goods_links = page.locator(
                        'a[href*="/products/"], '
                        'a[href*="/app/goods/"]'
                    )

                    link_count = (
                        goods_links.count()
                    )

                    if link_count > 0:

                        goods_links.nth(
                            link_count - 1
                        ).evaluate(
                            """
                            el => el.scrollIntoView({
                                behavior: 'instant',
                                block: 'center'
                            })
                            """
                        )

                    page.mouse.wheel(
                        0,
                        900
                    )

                    page.wait_for_timeout(
                        700
                    )

                except Exception as e:

                    print(
                        f"   스크롤 오류: {e}",
                        flush=True
                    )

                    break

                # 가상 DOM에서도 혹시 새 ID가 보이면 보충
                try:

                    hrefs = page.eval_on_selector_all(
                        "a[href]",
                        """
                        els =>
                        els
                        .map(a => a.href)
                        .filter(Boolean)
                        """
                    )

                except Exception:
                    hrefs = []

                for href in hrefs:

                    match = (
                        re.search(
                            r"/products/(\d+)",
                            href or "",
                            re.I
                        )
                        or
                        re.search(
                            r"/app/goods/(\d+)",
                            href or "",
                            re.I
                        )
                    )

                    if not match:
                        continue

                    add_product_id(
                        match.group(1),
                        keyword,
                        keyword_ids
                    )

                    if (
                        len(all_candidates)
                        >= candidate_target
                        or len(keyword_ids)
                        >= per_keyword_limit
                    ):
                        break

                added_this_round = (
                    len(all_candidates)
                    - before_round
                )

                if added_this_round > 0:
                    stagnant_rounds = 0
                else:
                    stagnant_rounds += 1

                if (
                    round_no == 1
                    or round_no % 5 == 0
                    or added_this_round > 0
                ):

                    max_api_page = (
                        max(api_pages)
                        if api_pages
                        else 1
                    )

                    print(
                        f"   STEP {round_no:03d} "
                        f"+{added_this_round} "
                        f"PLP={max_api_page} "
                        f"(검색어 "
                        f"{len(keyword_ids)}/"
                        f"{per_keyword_limit}, "
                        f"전체 "
                        f"{len(all_candidates)}/"
                        f"{candidate_target})",
                        flush=True
                    )

                # IntersectionObserver 재발동
                if stagnant_rounds == 7:

                    print(
                        "   ↳ 로딩 트리거 재시도",
                        flush=True
                    )

                    try:

                        page.mouse.wheel(
                            0,
                            -1400
                        )

                        page.wait_for_timeout(
                            400
                        )

                        page.mouse.wheel(
                            0,
                            2500
                        )

                        page.wait_for_timeout(
                            1200
                        )

                    except Exception:
                        pass

                if (
                    stagnant_rounds
                    >= STAGNANT_LIMIT
                ):

                    print(
                        f"   ↳ {STAGNANT_LIMIT}회 "
                        f"연속 신규 없음 "
                        f"→ 다음 검색어",
                        flush=True
                    )

                    break

        finally:

            try:
                page.remove_listener(
                    "response",
                    handle_response
                )
            except Exception:
                pass

        print(
            f"   ✅ {keyword}: "
            f"신규 후보 {len(keyword_ids)}개",
            flush=True
        )

    random.shuffle(all_candidates)

    print(
        f"✅ PLP 후보 탐색 완료: "
        f"{len(all_candidates)}개 "
        f"→ 바로 실측 API 검사 시작",
        flush=True
    )

    return all_candidates


# ============================================================
# 후보 상품 초고속 사이즈 API 선검사
# ============================================================

def prefilter_candidates_by_size_api(
    page,
    candidates,
    batch_size=8
):

    """
    상품페이지를 열기 전에 actual-size API만 병렬 호출.

    실제 API sizes 기준으로:
    - 사이즈 개수
    - 실측 항목 개수

    를 먼저 확인하고 조건 통과 상품만 남긴다.
    """

    if not candidates:
        return []

    kept = []

    no_actual = 0
    size_short = 0
    field_short = 0
    api_unknown = 0
    api_unknown_debug_count = 0
    no_actual_debug_count = 0
    field_short_debug_count = 0

    total = len(candidates)

    print()
    print("=" * 68, flush=True)

    print(
        f"⚡ 실제 실측 API 선검사: "
        f"{total}개 / 동시 {batch_size}개",
        flush=True
    )

    print("=" * 68, flush=True)


    for start_index in range(
        0,
        total,
        batch_size
    ):

        batch = candidates[
            start_index:
            start_index + batch_size
        ]

        ids = [
            str(
                x.get(
                    "product_id_hint",
                    ""
                )
            )
            for x in batch
        ]


        try:

            results = page.evaluate(
                """
                async (ids) => {

                    const inspect =
                        async (productId) => {

                        const url =
                            `https://goods-detail.musinsa.com/api2/goods/${productId}/actual-size`;

                        try {

                            const response =
                                await fetch(
                                    url,
                                    {
                                        method: 'GET',
                                        credentials: 'include',
                                        headers: {
                                            accept:
                                                'application/json, text/plain, */*'
                                        }
                                    }
                                );

                            let body = null;

                            try {
                                body =
                                    await response.json();
                            }
                            catch (e) {}


                            const detail =
                                body
                                && body.data
                                && body.data.data
                                ? body.data.data
                                : (
                                    body
                                    && body.data
                                    ? body.data
                                    : null
                                );


                            const sizes =
                                detail
                                && Array.isArray(
                                    detail.sizes
                                )
                                ? detail.sizes
                                : [];


                            const fields =
                                new Set();


                            for (const size of sizes) {

                                const items =
                                    Array.isArray(
                                        size.items
                                    )
                                    ? size.items
                                    : [];

                                for (const item of items) {

                                    const value =
                                        Number(
                                            item.value
                                        );

                                    // 0은 미측정
                                    if (
                                        !Number.isFinite(
                                            value
                                        )
                                        || value === 0
                                    ) {
                                        continue;
                                    }

                                    const name =
                                        String(
                                            item.name || ''
                                        ).trim();

                                    if (name) {
                                        fields.add(name);
                                    }
                                }
                            }


                            return {
                                productId,
                                ok: response.ok,
                                status:
                                    response.status,

                                sizeCount:
                                    sizes.length,

                                fieldCount:
                                    fields.size,

                                body
                            };

                        }
                        catch (e) {

                            return {
                                productId,
                                ok: false,
                                status: 0,
                                error:
                                    String(e)
                            };
                        }
                    };


                    return await Promise.all(
                        ids.map(inspect)
                    );
                }
                """,
                ids
            )

        except Exception as e:

            print(
                f"⚠️ API batch 오류: {e}",
                flush=True
            )

            kept.extend(batch)

            api_unknown += len(batch)

            continue


        by_id = {
            str(r.get("productId")): r
            for r in results
            if isinstance(r, dict)
        }


        for candidate in batch:

            pid = str(
                candidate.get(
                    "product_id_hint",
                    ""
                )
            )

            info = by_id.get(pid)


            if (
                not info
                or info.get("error")
                or not info.get("ok")
            ):

                api_unknown += 1

                if api_unknown_debug_count < 10:
                    api_unknown_debug_count += 1

                    status = (
                        info.get("status")
                        if isinstance(info, dict)
                        else None
                    )

                    error = (
                        info.get("error")
                        if isinstance(info, dict)
                        else "info 없음"
                    )

                    body = (
                        info.get("body")
                        if isinstance(info, dict)
                        else None
                    )

                    print(
                        f"    🚨 API오류 진단 #{api_unknown_debug_count}"
                        f" | 상품={pid}"
                        f" | HTTP={status}"
                        f" | error={error}"
                        f" | body={str(body)[:600]}",
                        flush=True
                    )

                # API 오류는 오탐 방지를 위해
                # 상세검사로 넘김
                kept.append(candidate)
                continue


            size_count = int(
                info.get("sizeCount")
                or 0
            )

            field_count = int(
                info.get("fieldCount")
                or 0
            )


            if size_count == 0:

                no_actual += 1

                if no_actual_debug_count < 10:
                    no_actual_debug_count += 1

                    body = info.get("body")

                    body_keys = (
                        list(body.keys())
                        if isinstance(body, dict)
                        else []
                    )

                    first_data = (
                        body.get("data")
                        if isinstance(body, dict)
                        else None
                    )

                    data_keys = (
                        list(first_data.keys())
                        if isinstance(first_data, dict)
                        else []
                    )

                    nested_data = (
                        first_data.get("data")
                        if isinstance(first_data, dict)
                        else None
                    )

                    nested_keys = (
                        list(nested_data.keys())
                        if isinstance(nested_data, dict)
                        else []
                    )

                    print(
                        f"    🔬 실측없음 진단 #{no_actual_debug_count}"
                        f" | 상품={pid}"
                        f" | HTTP={info.get('status')}"
                        f" | body_keys={body_keys}"
                        f" | data_keys={data_keys}"
                        f" | nested_keys={nested_keys}"
                        f" | body={str(body)[:700]}",
                        flush=True
                    )

                continue


            if size_count < MIN_SIZE_COUNT:

                size_short += 1
                continue


            if field_count < MIN_MEASUREMENT_FIELDS:

                field_short += 1

                if field_short_debug_count < 10:
                    field_short_debug_count += 1

                    body = info.get("body")

                    first_data = (
                        body.get("data")
                        if isinstance(body, dict)
                        else None
                    )

                    detail = (
                        first_data.get("data")
                        if isinstance(first_data, dict)
                        and isinstance(first_data.get("data"), dict)
                        else first_data
                    )

                    sizes_debug = (
                        detail.get("sizes", [])
                        if isinstance(detail, dict)
                        else []
                    )

                    print(
                        f"    🧪 필드부족 진단 #{field_short_debug_count}"
                        f" | 상품={pid}"
                        f" | sizes={size_count}"
                        f" | fields={field_count}"
                        f" | 실제sizes={str(sizes_debug)[:1200]}",
                        flush=True
                    )

                continue


            # API 원본을 candidate에 같이 보관.
            # 상품페이지에서 API를 또 호출하지 않는다.
            candidate[
                "actual_size_api_prefetch"
            ] = {
                "ok": True,
                "status":
                    info.get(
                        "status",
                        200
                    ),
                "url":
                    (
                        "https://goods-detail.musinsa.com/"
                        f"api2/goods/{pid}/actual-size"
                    ),
                "data":
                    info.get("body")
            }

            kept.append(candidate)


        done = min(
            start_index + len(batch),
            total
        )

        print(
            f"⚡ {done}/{total}"
            f" | 통과 {len(kept)}"
            f" | 실측없음 {no_actual}"
            f" | 사이즈부족 {size_short}"
            f" | 필드부족 {field_short}"
            f" | API오류 {api_unknown}",
            flush=True
        )

        page.wait_for_timeout(100)


    print()
    print(
        f"✅ API 선검사: "
        f"{total}개 → {len(kept)}개",
        flush=True
    )

    return kept



# ============================================================
# 8. JSON-LD
# ============================================================

def find_product_objects(obj):

    found = []


    if isinstance(obj, dict):

        object_type = obj.get("@type")

        if (
            object_type == "Product"

            or

            (
                isinstance(object_type, list)
                and
                "Product" in object_type
            )
        ):

            found.append(obj)


        for value in obj.values():

            found.extend(
                find_product_objects(value)
            )


    elif isinstance(obj, list):

        for value in obj:

            found.extend(
                find_product_objects(value)
            )


    return found


def extract_json_ld_product(page):

    try:

        scripts = page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents()

    except Exception:

        return {}


    products = []


    for raw in scripts:

        try:

            parsed = json.loads(raw)

        except Exception:

            continue


        products.extend(
            find_product_objects(parsed)
        )


    if not products:
        return {}


    products.sort(

        key=lambda x: (
            bool(x.get("name")),
            bool(x.get("brand")),
            bool(x.get("offers")),
            bool(x.get("image"))
        ),

        reverse=True
    )


    return products[0]


# ============================================================
# 9. 상품 기본정보
# ============================================================

def extract_brand(product_json, page_text):

    brand = product_json.get("brand")


    if isinstance(brand, dict):

        brand = brand.get("name")


    brand = clean_text(brand)


    if brand:
        return brand


    patterns = [

        r"브랜드\s*[:：]\s*([^\n|]{2,80})",
        r"Brand\s*[:：]\s*([^\n|]{2,80})",
    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
            re.I
        )

        if match:

            return clean_text(
                match.group(1)
            )


    return None


def extract_product_name(
    product_json,
    page
):

    name = clean_text(
        product_json.get("name")
    )


    if name:
        return name


    try:

        name = clean_text(
            page.locator(
                "h1"
            ).first.inner_text(
                timeout=1500
            )
        )

        if name:
            return name

    except Exception:
        pass


    try:

        return clean_text(
            page.title()
        )

    except Exception:

        return None


def extract_price(product_json):

    offers = product_json.get("offers")


    if isinstance(offers, dict):

        return to_number(

            offers.get("price")

            or

            offers.get("lowPrice")

            or

            offers.get("highPrice")
        )


    if isinstance(offers, list):

        for offer in offers:

            if not isinstance(
                offer,
                dict
            ):
                continue


            value = to_number(

                offer.get("price")

                or

                offer.get("lowPrice")

                or

                offer.get("highPrice")
            )


            if value is not None:
                return value


    return None


# ============================================================
# 10. 라벨 근처 값 탐색
# ============================================================

REVIEW_MARKERS = [

    "도움돼요",
    "신고하기",
    "구매 사이즈",
    "구매 색상",
    "사이즈 좋아요",
    "밝기",
    "색감",
]


def is_review_text(value):

    value = clean_text(value)

    if not value:
        return False


    return any(
        marker in value
        for marker in REVIEW_MARKERS
    )


def get_clean_lines(text):

    result = []


    for line in str(
        text or ""
    ).splitlines():

        line = clean_text(line)


        if line:
            result.append(line)


    return result


def find_value_near_label(
    text,
    labels,
    max_next_lines=3
):

    lines = get_clean_lines(text)


    for index, line in enumerate(lines):

        for label in labels:

            # 같은 줄
            match = re.search(
                rf"{re.escape(label)}"
                r"\s*[:：]\s*(.+)",
                line,
                re.I
            )


            if match:

                value = clean_text(
                    match.group(1)
                )


                if (
                    value
                    and
                    not is_review_text(value)
                ):
                    return value


            # 라벨 단독 → 다음 줄
            compact = (
                line
                .replace(" ", "")
                .lower()
            )

            label_compact = (
                label
                .replace(" ", "")
                .lower()
            )


            if (
                compact == label_compact

                or

                compact.startswith(
                    label_compact + ":"
                )

                or

                compact.startswith(
                    label_compact + "："
                )
            ):

                for offset in range(
                    1,
                    max_next_lines + 1
                ):

                    next_index = index + offset


                    if next_index >= len(lines):
                        break


                    value = clean_text(
                        lines[next_index]
                    )


                    if not value:
                        continue


                    if is_review_text(value):
                        continue


                    if len(value) > 250:
                        continue


                    return value


    return None


# ============================================================
# 11. 컬러
# ============================================================

def extract_color(
    product_name,
    product_json,
    page_text
):

    color = clean_text(
        product_json.get("color")
    )


    if color:
        return color


    value = find_value_near_label(
        page_text,
        [
            "색상",
            "컬러",
            "COLOR"
        ]
    )


    if value and len(value) <= 80:
        return value


    # 상품명 대괄호 값은 [리뉴얼ver.], [단독], [예약배송]처럼
    # 색상이 아닌 판매/버전 배지가 매우 자주 섞이므로 추론하지 않습니다.
    # 명시적인 color/색상 필드가 없으면 null로 보존합니다.
    return None


# ============================================================
# 12. 소재 개선
# ============================================================

MATERIAL_WORDS = [

    "면",
    "코튼",
    "cotton",

    "폴리에스터",
    "폴리에스테르",
    "polyester",

    "나일론",
    "nylon",

    "아크릴",
    "acrylic",

    "울",
    "wool",

    "모",
    "레이온",
    "rayon",

    "비스코스",
    "viscose",

    "스판",
    "spandex",

    "엘라스테인",
    "elastane",

    "폴리우레탄",
    "polyurethane",

    "린넨",
    "linen",

    "캐시미어",
    "cashmere",

    "가죽",
    "leather",

    "실크",
    "silk",

    "텐셀",
    "tencel",
]


def looks_like_material(value):

    value = clean_text(value)

    if not value:
        return False


    if is_review_text(value):
        return False


    lower = value.lower()


    if "%" in value:

        return any(
            word in lower
            for word in MATERIAL_WORDS
        )


    return any(
        word in lower
        for word in MATERIAL_WORDS
    )


def extract_material(
    product_json,
    page_text
):

    # JSON-LD
    for key in [
        "material",
        "fabric"
    ]:

        value = clean_text(
            product_json.get(key)
        )


        if (
            value
            and
            looks_like_material(value)
        ):
            return value


    labels = [

        "제품 소재",
        "제품소재",
        "소재",
        "혼용률",
        "구성 소재",
        "구성소재",
        "FABRIC",
        "MATERIAL",
        "겉감",
    ]


    value = find_value_near_label(
        page_text,
        labels,
        max_next_lines=4
    )


    if (
        value
        and
        looks_like_material(value)
    ):
        return value


    # 본문에서 혼용률 같은 구조
    patterns = [

        (
            r"(?:면|코튼|COTTON)"
            r"\s*\d{1,3}\s*%"
            r"(?:\s*[,/+]?\s*"
            r"(?:폴리에스터|폴리에스테르|나일론|스판|"
            r"폴리우레탄|레이온|아크릴|울|모|린넨)"
            r"\s*\d{1,3}\s*%)*"
        ),

        (
            r"(?:COTTON|POLYESTER|NYLON|"
            r"SPANDEX|WOOL|RAYON|ACRYLIC)"
            r"\s*\d{1,3}\s*%"
            r"(?:\s*[,/+]?\s*"
            r"(?:COTTON|POLYESTER|NYLON|"
            r"SPANDEX|WOOL|RAYON|ACRYLIC)"
            r"\s*\d{1,3}\s*%)*"
        )
    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
            re.I
        )


        if match:

            value = clean_text(
                match.group(0)
            )


            if looks_like_material(value):
                return value


    return None


# ============================================================
# 13. 핏
# ============================================================

FIT_VALUES = [

    "오버사이즈핏",
    "세미오버핏",
    "오버핏",

    "릴렉스핏",
    "루즈핏",

    "레귤러핏",

    "슬림핏",
    "스키니핏",

    "스트레이트핏",
    "와이드핏",

    "부츠컷",
]


def extract_fit(page_text):

    label_value = find_value_near_label(
        page_text,
        [
            "핏",
            "FIT"
        ]
    )


    if label_value:

        compact = (
            label_value
            .replace(" ", "")
            .lower()
        )


        for fit in FIT_VALUES:

            if (
                fit.replace(
                    " ",
                    ""
                ).lower()
                in compact
            ):

                return fit


    compact_text = (
        page_text
        .replace(" ", "")
        .lower()
    )


    for fit in FIT_VALUES:

        if (
            fit.replace(
                " ",
                ""
            ).lower()
            in compact_text
        ):

            return fit


    return None


# ============================================================
# 14. 신축성 개선
# ============================================================

def normalize_stretch(value):

    if not value:
        return None


    compact = (
        value
        .replace(" ", "")
        .lower()
    )


    if (
        "거의없" in compact

        or

        "없음" in compact

        or

        "없다" in compact
    ):
        return "신축성 거의 없음"


    if (
        "매우좋" in compact

        or

        "매우있" in compact
    ):
        return "신축성 매우 좋음"


    if (
        "좋음" in compact

        or

        "있음" in compact

        or

        "있다" in compact
    ):
        return "신축성 있음"


    if "보통" in compact:
        return "신축성 보통"


    if "약간" in compact:
        return "신축성 약간 있음"


    return None


def extract_stretch(page_text):

    value = find_value_near_label(
        page_text,
        [
            "신축성",
            "스트레치",
            "STRETCH"
        ]
    )


    normalized = normalize_stretch(value)


    if normalized:
        return normalized


    patterns = [

        r"신축성\s*(?:은|이)?\s*거의\s*없",
        r"신축성\s*없음",
        r"신축성\s*보통",
        r"신축성\s*약간\s*있",
        r"신축성\s*있음",
        r"스트레치\s*소재",
    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
            re.I
        )


        if match:

            normalized = normalize_stretch(
                match.group(0)
            )


            if normalized:
                return normalized


    return None


# ============================================================
# 15. 두께감 개선
# ============================================================

def normalize_thickness(value):

    if not value:
        return None


    compact = (
        value
        .replace(" ", "")
        .lower()
    )


    if "매우얇" in compact:
        return "매우 얇음"


    if (
        "얇음" in compact

        or

        "얇다" in compact
    ):
        return "얇음"


    if "보통" in compact:
        return "보통"


    if (
        "매우두꺼" in compact
    ):
        return "매우 두꺼움"


    if (
        "두꺼움" in compact

        or

        "두껍" in compact
    ):
        return "두꺼움"


    return None


def extract_thickness(page_text):

    value = find_value_near_label(
        page_text,
        [
            "두께",
            "두께감",
            "THICKNESS"
        ]
    )


    normalized = normalize_thickness(
        value
    )


    if normalized:
        return normalized


    patterns = [

        r"두께감?\s*[:：]?\s*얇음",
        r"두께감?\s*[:：]?\s*보통",
        r"두께감?\s*[:：]?\s*두꺼움",
    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            page_text,
            re.I
        )


        if match:

            normalized = normalize_thickness(
                match.group(0)
            )


            if normalized:
                return normalized


    return None


# ============================================================
# 16. 모델 정보 개선
# ============================================================

def extract_model_info(page_text):

    lines = get_clean_lines(page_text)

    result = []


    # --------------------------------------------------------
    # 모델 관련 줄 주변 탐색
    # --------------------------------------------------------

    for index, line in enumerate(lines):

        if not re.search(
            r"모델|MODEL",
            line,
            re.I
        ):
            continue


        block = " ".join(
            lines[
                index:
                min(
                    index + 7,
                    len(lines)
                )
            ]
        )


        height_match = re.search(
            r"(?<!\d)(1[4-9]\d|20\d)\s*cm",
            block,
            re.I
        )


        size_match = re.search(
            (
                r"(?:착용\s*사이즈|착용사이즈|"
                r"착용\s*SIZE|WEARING\s*SIZE)"
                r"\s*[:：]?\s*"
                r"([A-Za-z0-9\-]+)"
            ),
            block,
            re.I
        )


        if height_match:

            result.append({

                "height_cm":
                    int(
                        height_match.group(1)
                    ),

                "wearing_size":

                    clean_text(
                        size_match.group(1)
                    )

                    if size_match

                    else None
            })


    # --------------------------------------------------------
    # 전체 본문 패턴
    # --------------------------------------------------------

    patterns = [

        (
            r"(?P<height>1[4-9]\d|20\d)"
            r"\s*cm"
            r".{0,150}?"
            r"(?:착용\s*사이즈|착용사이즈)"
            r"\s*[:：]?\s*"
            r"(?P<size>[A-Za-z0-9\-]+)"
        ),

        (
            r"(?:착용\s*사이즈|착용사이즈)"
            r"\s*[:：]?\s*"
            r"(?P<size>[A-Za-z0-9\-]+)"
            r".{0,150}?"
            r"(?P<height>1[4-9]\d|20\d)"
            r"\s*cm"
        )
    ]


    for pattern in patterns:

        for match in re.finditer(
            pattern,
            page_text,
            re.I | re.S
        ):

            try:

                result.append({

                    "height_cm":
                        int(
                            match.group(
                                "height"
                            )
                        ),

                    "wearing_size":
                        clean_text(
                            match.group(
                                "size"
                            )
                        )
                })

            except Exception:
                pass


    unique = []

    seen = set()


    for item in result:

        key = (
            item.get("height_cm"),
            item.get("wearing_size")
        )


        if key in seen:
            continue


        seen.add(key)

        unique.append(item)


    return unique[:10]


# ============================================================
# 16.5 무신사 공식 실측 API 원본 보존
# ============================================================

def fetch_actual_size_api(page, product_id):
    """
    requests/httpx 직접 호출 대신 이미 열린 브라우저 페이지 컨텍스트에서 fetch합니다.
    파싱 로직이 바뀌어도 재크롤링하지 않도록 응답 원본을 /data/raw에 남깁니다.
    현재 버전은 기존 DOM 파서를 유지하고, API 응답은 원본 증거로 함께 저장합니다.
    """
    try:
        result = page.evaluate(
            """
            async (productId) => {
              const url = `https://goods-detail.musinsa.com/api2/goods/${productId}/actual-size`;
              try {
                const response = await fetch(url, {
                  method: 'GET',
                  credentials: 'include',
                  headers: { 'accept': 'application/json, text/plain, */*' }
                });
                const text = await response.text();
                let data = null;
                try { data = JSON.parse(text); } catch (e) {}
                return {
                  ok: response.ok,
                  status: response.status,
                  url,
                  data,
                  text: data === null ? text.slice(0, 20000) : null
                };
              } catch (e) {
                return { ok: false, status: 0, url, error: String(e) };
              }
            }
            """,
            str(product_id),
        )
        return result if isinstance(result, dict) else {"ok": False, "status": 0}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


# ============================================================
# 17. 사이즈
# ============================================================

COMMON_SIZE_LABELS = {

    "XXXS",
    "XXS",
    "XS",
    "S",
    "M",
    "L",
    "XL",
    "XXL",
    "2XL",
    "3XL",
    "4XL",
    "5XL",
    "FREE",
    "ONE SIZE",
    "OS",
}


def extract_size_labels_from_dom(page):

    selectors = (
        "button, "
        "option, "
        "label, "
        "li, "
        "[role='option'], "
        "[role='radio'], "
        "[data-size]"
    )


    try:

        texts = page.locator(
            selectors
        ).all_inner_texts()

    except Exception:

        texts = []


    result = []


    for text in texts:

        text = clean_text(text)


        if not text:
            continue


        upper = text.upper()


        if upper in COMMON_SIZE_LABELS:

            result.append(upper)

            continue


        if re.fullmatch(
            r"\d{2}",
            text
        ):

            number = int(text)


            if 20 <= number <= 50:

                result.append(text)


    return unique_keep_order(result)


def extract_size_labels_from_text(text):

    text = clean_text(text)


    if not text:
        return []


    match = re.search(
        (
            r"내 사이즈\s*\|\s*"
            r"(.*?)"
            r"\s*\|\s*"
            r"(?:총장|어깨|가슴|허리)"
        ),
        text,
        re.I
    )


    if not match:
        return []


    values = [

        clean_text(x)

        for x in match.group(1).split("|")
    ]


    return unique_keep_order(
        [
            value
            for value in values
            if value
        ]
    )


# ============================================================
# 18. 사이즈 가이드
# ============================================================

def open_size_guide(page):

    labels = [

        "실측 사이즈",
        "사이즈 정보",
        "사이즈 가이드",
        "사이즈 실측 안내",
        "제품 사이즈",
        "SIZE GUIDE",
    ]


    for label in labels:

        try:

            locator = page.get_by_text(
                label,
                exact=False
            )


            count = locator.count()


            for index in range(
                min(count, 8)
            ):

                item = locator.nth(index)


                try:

                    if not item.is_visible():
                        continue


                    item.click(
                        timeout=1200
                    )


                    page.wait_for_timeout(250)

                    return True


                except Exception:
                    continue


        except Exception:
            continue


    return False


# ============================================================
# 19. HTML 테이블
# ============================================================

def extract_tables(page):

    tables = []


    try:

        count = page.locator(
            "table"
        ).count()

    except Exception:

        return []


    for table_index in range(
        min(count, 15)
    ):

        table = page.locator(
            "table"
        ).nth(table_index)


        try:

            row_count = table.locator(
                "tr"
            ).count()

        except Exception:

            continue


        rows = []


        for row_index in range(
            min(row_count, 100)
        ):

            row = table.locator(
                "tr"
            ).nth(row_index)


            try:

                cells = row.locator(
                    "th, td"
                ).all_inner_texts()

            except Exception:

                continue


            cells = [

                clean_text(cell)

                for cell in cells

                if clean_text(cell)
            ]


            if cells:
                rows.append(cells)


        if rows:
            tables.append(rows)


    return tables


# ============================================================
# 20. 실측 항목
# ============================================================

HEADER_MAP = {

    "총장": "total_length_cm",
    "기장": "total_length_cm",
    "총길이": "total_length_cm",

    "어깨": "shoulder_cm",
    "어깨너비": "shoulder_cm",
    "어깨단면": "shoulder_cm",

    "가슴": "chest_flat_cm",
    "가슴단면": "chest_flat_cm",
    "가슴너비": "chest_flat_cm",
    "가슴둘레": "chest_circumference_cm",

    "소매": "sleeve_length_cm",
    "소매길이": "sleeve_length_cm",
    "팔길이": "sleeve_length_cm",

    "암홀": "armhole_cm",

    "팔통": "sleeve_width_cm",
    "소매통": "sleeve_width_cm",

    "허리": "waist_flat_cm",
    "허리단면": "waist_flat_cm",
    "허리둘레": "waist_circumference_cm",

    "힙": "hip_flat_cm",
    "힙단면": "hip_flat_cm",
    "엉덩이단면": "hip_flat_cm",
    "힙둘레": "hip_circumference_cm",
    "엉덩이둘레": "hip_circumference_cm",

    "허벅지": "thigh_flat_cm",
    "허벅지단면": "thigh_flat_cm",

    "밑위": "rise_cm",
    "밑위길이": "rise_cm",
    "앞밑위": "rise_cm",

    "인심": "inseam_cm",
    "안쪽다리길이": "inseam_cm",

    "밑단": "hem_flat_cm",
    "밑단단면": "hem_flat_cm",
}


def normalize_measurement_header(value):

    value = clean_text(value)


    if not value:
        return None


    compact = (
        value
        .replace(" ", "")
        .replace("(cm)", "")
        .replace("cm", "")
        .replace("CM", "")
    )


    if compact in HEADER_MAP:

        return HEADER_MAP[compact]


    for keyword, field in HEADER_MAP.items():

        if keyword in compact:
            return field


    return None


# ============================================================
# 21. 사이즈 테이블 파싱
# ============================================================

def parse_measurements(
    tables,
    known_sizes
):

    measurements = {}


    for table in tables:

        if (
            not isinstance(table, list)
            or
            len(table) < 2
        ):
            continue


        header = table[0]


        if not isinstance(header, list):
            continue


        normalized_headers = [

            normalize_measurement_header(
                value
            )

            for value in header
        ]


        if not any(normalized_headers):
            continue


        size_column = None


        for index, value in enumerate(header):

            value = clean_text(value) or ""


            if value.lower() in {
                "사이즈",
                "size"
            }:

                size_column = index
                break


        numeric_rows = []


        for row in table[1:]:

            if (
                not isinstance(row, list)
                or
                len(row) <= 1
            ):
                continue


            if not any(
                to_number(cell) is not None
                for cell in row
            ):
                continue


            numeric_rows.append(row)


        # 사이즈 열이 있는 경우
        if size_column is not None:

            for row in numeric_rows:

                if size_column >= len(row):
                    continue


                size_label = clean_text(
                    row[size_column]
                )


                if not size_label:
                    continue


                item = measurements.setdefault(
                    size_label,
                    {}
                )


                for index, field in enumerate(
                    normalized_headers
                ):

                    if not field:
                        continue


                    if index >= len(row):
                        continue


                    value = to_number(
                        row[index]
                    )


                    if value is not None:

                        item[field] = value


        # 무신사 구조
        elif (
            known_sizes
            and
            len(numeric_rows) >= len(known_sizes)
        ):

            for (
                size_label,
                row
            ) in zip(
                known_sizes,
                numeric_rows
            ):

                item = measurements.setdefault(
                    str(size_label),
                    {}
                )


                for index, field in enumerate(
                    normalized_headers
                ):

                    if not field:
                        continue


                    if index >= len(row):
                        continue


                    value = to_number(
                        row[index]
                    )


                    if value is not None:

                        item[field] = value


    return measurements


def measurement_field_set(
    measurements
):

    return sorted({

        field

        for size_data
        in measurements.values()

        if isinstance(
            size_data,
            dict
        )

        for field
        in size_data.keys()
    })


def parse_actual_size_measurements(actual_size_api):
    """
    Musinsa actual-size API를 직접 실측 데이터로 변환.

    0 값은 '미측정'으로 보고 저장하지 않음.
    """

    if not isinstance(actual_size_api, dict):
        return [], {}

    payload = actual_size_api.get("data")

    if not isinstance(payload, dict):
        return [], {}

    # 응답 구조:
    # response["data"]["data"]["sizes"]
    detail = payload.get("data")

    if not isinstance(detail, dict):
        detail = payload

    sizes = detail.get("sizes")

    if not isinstance(sizes, list):
        return [], {}


    def map_field(name):

        name = clean_text(name)

        if not name:
            return None

        key = re.sub(
            r"[\s_/()\-]+",
            "",
            name
        ).lower()


        # 총장
        if key in {
            "총장",
            "전체길이",
            "기장",
            "옷길이",
        }:
            return "total_length_cm"


        # 어깨
        if (
            "어깨" in key
            and "너비" in key
        ) or key == "어깨":
            return "shoulder_cm"


        # 가슴
        if (
            "가슴" in key
            and "단면" in key
        ):
            return "chest_flat_cm"

        if (
            "가슴" in key
            and "둘레" in key
        ):
            return "chest_circumference_cm"


        # 소매
        if (
            "소매" in key
            and "길이" in key
        ):
            return "sleeve_length_cm"

        if (
            "소매통" in key
            or "소매너비" in key
            or "소매단면" in key
        ):
            return "sleeve_width_cm"


        # 암홀
        if "암홀" in key:
            return "armhole_cm"


        # 허리
        if (
            "허리" in key
            and "단면" in key
        ):
            return "waist_flat_cm"

        if (
            "허리" in key
            and "둘레" in key
        ):
            return "waist_circumference_cm"


        # 엉덩이 / 힙
        if (
            ("엉덩이" in key or "힙" in key)
            and "단면" in key
        ):
            return "hip_flat_cm"

        if (
            ("엉덩이" in key or "힙" in key)
            and "둘레" in key
        ):
            return "hip_circumference_cm"


        # 허벅지
        if (
            "허벅지" in key
            and (
                "단면" in key
                or key == "허벅지"
            )
        ):
            return "thigh_flat_cm"


        # 밑위
        if (
            "밑위" in key
            or "앞밑위" in key
        ):
            return "rise_cm"


        # 인심
        if (
            "인심" in key
            or "안쪽길이" in key
            or "안기장" in key
        ):
            return "inseam_cm"


        # 밑단
        if (
            "밑단" in key
            and "단면" in key
        ):
            return "hem_flat_cm"


        return None


    labels = []
    measurements = {}


    for size in sizes:

        if not isinstance(size, dict):
            continue

        label = clean_text(
            size.get("name")
        )

        if not label:
            continue

        labels.append(label)

        row = {}

        items = size.get("items")

        if not isinstance(items, list):
            items = []


        for item in items:

            if not isinstance(item, dict):
                continue

            field = map_field(
                item.get("name")
            )

            if not field:
                continue

            value = to_number(
                item.get("value")
            )

            # 무신사에서 0은 미측정 의미
            if value is None or value == 0:
                continue

            row[field] = value


        if row:
            measurements[label] = row


    labels = unique_keep_order(
        labels
    )

    return labels, measurements



def passes_size_first_rule(
    size_labels,
    measurements,
    search_keyword=None
):

    """
    SIZE-FIRST 고품질 실측 통과 규칙.

    단순히 필드 개수만 보는 것이 아니라
    카테고리별로 신체-의류 매칭에 필요한
    핵심 실측 조합이 있는지 확인한다.
    """

    if len(size_labels) < MIN_SIZE_COUNT:

        return (
            False,
            "판매 사이즈 부족"
        )


    if not measurements:

        return (
            False,
            "실측표 없음"
        )


    fields = measurement_field_set(
        measurements
    )


    # 전체적으로 최소 3종의 실제 실측값 필요
    if len(fields) < 3:

        return (
            False,
            f"실측 항목 부족({len(fields)}개)"
        )


    (
        _gender,
        main_category,
        sub_category
    ) = classify_keyword(
        search_keyword or ""
    )


    keyword_text = (
        f"{search_keyword or ''} "
        f"{sub_category or ''}"
    ).lower()


    def has(*names):

        return any(
            name in fields
            for name in names
        )


    # --------------------------------------------------------
    # 공통 필드 그룹
    # --------------------------------------------------------

    has_chest = has(
        "chest_flat_cm",
        "chest_circumference_cm"
    )

    has_waist = has(
        "waist_flat_cm",
        "waist_circumference_cm"
    )

    has_hip = has(
        "hip_flat_cm",
        "hip_circumference_cm"
    )

    has_total = has(
        "total_length_cm"
    )

    has_shoulder = has(
        "shoulder_cm"
    )

    has_sleeve = has(
        "sleeve_length_cm"
    )

    has_thigh = has(
        "thigh_flat_cm"
    )

    has_rise = has(
        "rise_cm"
    )

    has_inseam = has(
        "inseam_cm"
    )

    has_hem = has(
        "hem_flat_cm"
    )


    # ========================================================
    # 상의
    #
    # 필수:
    # 가슴 + 총장 + (어깨 또는 소매)
    # ========================================================

    if main_category == "상의":

        missing = []

        if not has_chest:
            missing.append("가슴")

        if not has_total:
            missing.append("총장")

        if not (
            has_shoulder
            or has_sleeve
        ):
            missing.append("어깨/소매")

        if missing:

            return (
                False,
                "상의 핵심 실측 부족: "
                + ", ".join(missing)
            )


    # ========================================================
    # 아우터
    #
    # 필수:
    # 가슴 + 총장 + (어깨 또는 소매)
    # ========================================================

    elif main_category == "아우터":

        missing = []

        if not has_chest:
            missing.append("가슴")

        if not has_total:
            missing.append("총장")

        if not (
            has_shoulder
            or has_sleeve
        ):
            missing.append("어깨/소매")

        if missing:

            return (
                False,
                "아우터 핵심 실측 부족: "
                + ", ".join(missing)
            )


    # ========================================================
    # 하의 - 스커트
    #
    # 필수:
    # 허리 + 총장 + (힙 또는 밑단)
    # ========================================================

    elif (
        main_category == "하의"
        and "스커트" in keyword_text
    ):

        missing = []

        if not has_waist:
            missing.append("허리")

        if not has_total:
            missing.append("총장")

        if not (
            has_hip
            or has_hem
        ):
            missing.append("힙/밑단")

        if missing:

            return (
                False,
                "스커트 핵심 실측 부족: "
                + ", ".join(missing)
            )


    # ========================================================
    # 하의 - 팬츠
    #
    # 필수:
    # 허리
    # + (힙 또는 허벅지)
    # + (총장 또는 인심 또는 밑위)
    # ========================================================

    elif main_category == "하의":

        missing = []

        if not has_waist:
            missing.append("허리")

        if not (
            has_hip
            or has_thigh
        ):
            missing.append("힙/허벅지")

        if not (
            has_total
            or has_inseam
            or has_rise
        ):
            missing.append(
                "총장/인심/밑위"
            )

        if missing:

            return (
                False,
                "팬츠 핵심 실측 부족: "
                + ", ".join(missing)
            )


    # ========================================================
    # 원피스
    #
    # 필수:
    # 가슴 + 총장 + (허리 또는 힙 또는 어깨)
    # ========================================================

    elif main_category == "원피스":

        missing = []

        if not has_chest:
            missing.append("가슴")

        if not has_total:
            missing.append("총장")

        if not (
            has_waist
            or has_hip
            or has_shoulder
        ):
            missing.append(
                "허리/힙/어깨"
            )

        if missing:

            return (
                False,
                "원피스 핵심 실측 부족: "
                + ", ".join(missing)
            )


    # 기타 예외 카테고리는 최소 3필드 유지
    elif len(fields) < MIN_MEASUREMENT_FIELDS:

        return (
            False,
            "실측 항목 부족"
        )


    return (
        True,
        "카테고리 핵심 실측 통과"
    )


# ============================================================
# 22. 이미지
# ============================================================

def is_relevant_product_image_url(url, product_id=None):

    if not url:
        return False

    lower = str(url).lower()

    if lower.startswith("data:"):
        return False

    # UI / 배너 / 사이즈 안내 등 명확한 잡이미지
    blocked = [
        "logo",
        "favicon",
        "sprite",
        "icon",
        "banner",
        "coupon",
        "size_type",
        "size-guide",
        "size_guide",
        "shipping",
        "delivery",
        "notice",
    ]

    if any(word in lower for word in blocked):
        return False

    product_id = str(product_id or "").strip()

    # 다른 상품의 대표 이미지 제외
    if product_id:

        goods_match = re.search(
            r"/goods_img/\d{8}/(\d+)/",
            lower
        )

        if (
            goods_match
            and goods_match.group(1) != product_id
        ):
            return False

        # 상세이미지는 현재 상품 것이면 허용
        detail_match = re.search(
            r"detail[_/-]?(\d+)",
            lower
        )

        if (
            detail_match
            and detail_match.group(1) != product_id
        ):
            return False

    return True


def extract_image_urls(
    page,
    product_json,
    page_url
):
    """
    상품/실측 수집 단계에서는 이미지를 수집하지 않는다.

    이미지 작업은 별도 image worker에서
    platform_product_id 기준으로 처리한다.
    """
    print(
        "    🖼 이미지: 별도 워커 처리 예정",
        flush=True
    )
    return []


def download_images(
    session,
    image_urls,
    folder,
    referer
):

    # 서버 기본 모드: 이미지 파일을 로컬에 쌓지 않고 URL 메타만 저장합니다.
    if not DOWNLOAD_IMAGES:
        return [
            {"index": index, "local_path": None, "source_url": image_url}
            for index, image_url in enumerate(image_urls[:MAX_IMAGES], start=1)
        ]


    downloaded = []


    for index, image_url in enumerate(
        image_urls[:MAX_IMAGES],
        start=1
    ):

        try:

            response = session.get(
                image_url,
                timeout=30,
                headers={
                    "User-Agent":
                        USER_AGENT,

                    "Referer":
                        referer
                }
            )


            response.raise_for_status()


            content_type = (
                response
                .headers
                .get(
                    "Content-Type",
                    ""
                )
                .lower()
            )


            extension = ".jpg"


            if "png" in content_type:
                extension = ".png"

            elif "webp" in content_type:
                extension = ".webp"


            path = (
                folder
                /
                f"image_{index:02d}{extension}"
            )


            path.write_bytes(
                response.content
            )


            downloaded.append({

                "index":
                    index,

                "local_path":
                    str(path),

                "source_url":
                    image_url
            })


        except Exception as e:

            print(
                "    이미지 다운로드 실패: "
                f"{e}"
            )


    return downloaded



# ============================================================
# 상품 실측 선검사용 빠른 페이지 진입
# ============================================================

def goto_product_fast(page, url):

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        # API fetch가 가능한 정도만 기다림
        page.wait_for_timeout(120)

        return True

    except Exception as e:

        print(
            f"    ❌ 빠른 페이지 진입 실패: {e}",
            flush=True
        )

        return False


def load_product_content_after_size_pass(page):

    """
    실측 API를 통과한 상품만
    나머지 상품정보/이미지 로딩을 진행.
    """

    try:

        for _ in range(PRODUCT_SCROLL_STEPS):

            page.mouse.wheel(
                0,
                1600
            )

            page.wait_for_timeout(80)

    except Exception:
        pass


# ============================================================
# 23. 상품 하나 수집
# ============================================================

def collect_one_product(
    page,
    session,
    candidate
):

    url = candidate["url"]

    product_id = candidate[
        "product_id_hint"
    ]

    search_keyword = candidate[
        "search_keyword"
    ]


    print()
    print(f"[검사] {url}")


    # ========================================================
    # 사이즈/실측은 actual-size API를 정답으로 사용
    # ========================================================

    actual_size_api = candidate.get(
        "actual_size_api_prefetch"
    )


    # 선검사를 거치지 않은 예외 후보만
    # 여기서 API 호출
    if not isinstance(
        actual_size_api,
        dict
    ):

        actual_size_api = fetch_actual_size_api(
            page,
            product_id
        )


    size_labels, measurements = (
        parse_actual_size_measurements(
            actual_size_api
        )
    )


    measurement_fields = (
        measurement_field_set(
            measurements
        )
    )


    print(
        f"    ⚡ API 실측 "
        f"sizes={len(size_labels)}"
        f" / measurement_sizes={len(measurements)}"
        f" / fields={len(measurement_fields)}",
        flush=True
    )


    passed, reason = passes_size_first_rule(
            size_labels,
            measurements,
            search_keyword
        )


    if not passed:

        print(
            f"    ⚡ 즉시 탈락: {reason}",
            flush=True
        )

        return (
            None,
            reason
        )


    # ========================================================
    # 실측 조건을 통과한 상품만 실제 상품페이지를 연다
    # ========================================================

    if not goto_product_fast(
        page,
        url
    ):

        return (
            None,
            "페이지 접근 실패"
        )


    load_product_content_after_size_pass(
        page
    )


    product_json = extract_json_ld_product(
        page
    )


    product_name = extract_product_name(
        product_json,
        page
    )


    # ========================================================
    # 의류 전용 안전장치
    # ========================================================

    clothing_ok, clothing_reason = clothing_only_guard(
        product_name,
        product_json,
        search_keyword
    )

    if not clothing_ok:
        print(
            f"    🚫 의류 전용 가드: "
            f"{clothing_reason}"
        )

        return (
            None,
            clothing_reason
        )


    # ========================================================
    # 사이즈 데이터
    #
    # 위에서 actual-size API로 이미 확정함.
    # DOM 사이즈표를 다시 열거나 파싱하지 않는다.
    # ========================================================

    size_guide_opened = False

    # 부가정보 추출에는 본문 텍스트를 계속 사용
    page_text = get_page_text(
        page
    )

    # raw 호환용. 실측 정답은 actual-size API.
    tables = []

    # ========================================================
    # 부가 정보
    # ========================================================

    brand = extract_brand(
        product_json,
        page_text
    )


    price = extract_price(
        product_json
    )


    color = extract_color(
        product_name,
        product_json,
        page_text
    )


    material = extract_material(
        product_json,
        page_text
    )


    fit = extract_fit(
        page_text
    )


    stretch = extract_stretch(
        page_text
    )


    thickness = extract_thickness(
        page_text
    )


    model_info = extract_model_info(
        page_text
    )


    (
        gender,
        main_category,
        sub_category

    ) = classify_keyword(
        search_keyword
    )


    # ========================================================
    # 상품 폴더
    # ========================================================

    collection_date = datetime.now().strftime("%Y-%m-%d")

    product_dir = (
        RAW_DIR
        / collection_date
        / safe_name(product_id)
    )

    image_dir = (
        IMAGE_DIR
        / safe_name(product_id)
    )

    product_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    image_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    page_text_path = (
        product_dir
        /
        "page_text.txt"
    )


    page_text_path.write_text(
        page_text,
        encoding="utf-8",
        errors="ignore"
    )


    raw_tables_path = (
        product_dir
        /
        "size_tables_raw.json"
    )


    raw_tables_path.write_text(
        json.dumps(
            tables,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    actual_size_path = product_dir / "actual_size_raw.json"
    actual_size_path.write_text(
        json.dumps(actual_size_api, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    image_urls = extract_image_urls(
        page,
        product_json,
        url
    )


    images = download_images(
        session,
        image_urls,
        image_dir,
        url
    )


    result = {

        "schema_version":
            "1.1",


        "collection": {

            "strategy":
                "size_first",

            "search_keyword":
                search_keyword,

            "collected_at":
                now_string()
        },


        "classification": {

            "gender":
                gender,

            "main_category":
                main_category,

            "sub_category":
                sub_category
        },


        "size_data": {

            "size_labels":
                size_labels,

            "measurements":
                measurements,

            "measurement_fields":
                measurement_fields,

            "measurement_type":
                "garment_actual"
        },


        "product": {

            # 원본 사이트 상품번호. DB 적재 시 platform_product_id로 사용합니다.
            "product_id":
                product_id,

            "platform_product_id":
                product_id,

            "brand":
                brand,

            "product_name":
                product_name,

            "price":
                price,

            "color":
                color,

            "product_url":
                url
        },


        "garment_attributes": {

            "material":
                material,

            "fit":
                fit,

            "stretch":
                stretch,

            "thickness":
                thickness
        },


        "model_info":
            model_info,


        "media": {

            "images":
                images
        },


        # 재크롤링 없이 다시 파싱/적재할 수 있도록 원본 계층을 보존합니다.
        # 전체 page_text는 별도 파일에 저장하고, JSON-LD와 원본 사이즈표를 JSON에도 남깁니다.
        "raw": {

            "json_ld_product":
                product_json,

            "size_tables":
                tables,

            "actual_size_api":
                actual_size_api,

            "page_text_path":
                str(page_text_path)
        },


        "quality": {

            "accepted":
                True,

            "size_label_count":
                len(size_labels),

            "measurement_size_count":
                len(measurements),

            "measurement_field_count":
                len(measurement_fields),

            "image_count":
                len(images)
        },


        "source": {

            "platform":
                "musinsa",

            "size_guide_opened":
                size_guide_opened,

            "page_text_path":
                str(page_text_path),

            "raw_tables_path":
                str(raw_tables_path),

            "actual_size_path":
                str(actual_size_path),

            "actual_size_status":
                actual_size_api.get("status")
        }
    }


    product_json_path = (
        product_dir
        /
        "product.json"
    )


    product_json_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


    print()
    print("    ✅ 저장 완료")
    print(f"       브랜드: {brand}")
    print(f"       상품: {product_name}")

    print(
        f"       카테고리: "
        f"{gender} / "
        f"{main_category} / "
        f"{sub_category}"
    )

    print(f"       사이즈: {size_labels}")
    print(f"       실측 필드: {measurement_fields}")

    print(
        f"       소재: "
        f"{material}"
    )

    print(
        f"       핏: "
        f"{fit}"
    )

    print(
        f"       신축성: "
        f"{stretch}"
    )

    print(
        f"       두께: "
        f"{thickness}"
    )

    print(
        f"       모델정보: "
        f"{len(model_info)}개"
    )

    print(
        f"       이미지: "
        f"{len(images)}장"
    )


    return (
        result,
        "통과"
    )


# ============================================================
# 24. 기존 데이터
# ============================================================

def load_existing_products():

    products = []
    urls = set()
    product_ids = set()


    for path in BASE_DIR.rglob("*.json"):

        if path.name.startswith("_") or path.name == "size_tables_raw.json":
            continue

        try:

            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(data, dict):
                continue
            if not isinstance(data.get("product"), dict):
                continue
            if not isinstance(data.get("size_data"), dict):
                continue

            if not data.get(
                "quality",
                {}
            ).get(
                "accepted"
            ):

                continue


            product = data.get(
                "product",
                {}
            )


            product_id = product.get(
                "product_id"
            )


            url = product.get(
                "product_url"
            )


            products.append(data)


            if url:
                urls.add(url)


            if product_id:

                product_ids.add(
                    str(product_id)
                )


        except Exception as e:

            print(
                f"기존 데이터 읽기 실패: "
                f"{path}"
            )

            print(e)


    return (
        products,
        urls,
        product_ids
    )


def bootstrap_existing_products_to_processed(
    existing_products,
    processed
):

    changed = False


    for item in existing_products:

        product = item.get(
            "product",
            {}
        )


        url = product.get(
            "product_url"
        )


        product_id = product.get(
            "product_id"
        )


        if not url:
            continue


        if url in processed:
            continue


        processed[url] = {

            "product_id":
                str(product_id)
                if product_id
                else None,

            "status":
                "accepted_existing",

            "reason":
                "기존 수집 데이터",

            "processed_at":
                item
                .get(
                    "collection",
                    {}
                )
                .get(
                    "collected_at"
                )
        }


        changed = True


    if changed:

        save_processed_urls(
            processed
        )


# ============================================================
# 25. 중복 제거
# ============================================================

def deduplicate_products(products):

    unique_products = []

    seen_urls = set()
    seen_ids = set()


    for item in products:

        product = item.get(
            "product",
            {}
        )


        url = product.get(
            "product_url"
        )


        product_id = str(
            product.get(
                "product_id",
                ""
            )
        )


        if url and url in seen_urls:
            continue


        if (
            product_id
            and
            product_id in seen_ids
        ):
            continue


        if url:
            seen_urls.add(url)


        if product_id:
            seen_ids.add(product_id)


        unique_products.append(item)


    return unique_products


# ============================================================
# 26. CSV 안전 저장
# ============================================================

def safe_write_csv(
    path,
    rows
):

    if not rows:
        return


    try:

        with open(
            path,
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=list(
                    rows[0].keys()
                )
            )


            writer.writeheader()

            writer.writerows(
                rows
            )


    except PermissionError:

        print()
        print(
            "    ⚠ CSV가 Excel에서 열려 있어 "
            "갱신하지 못했습니다."
        )

        print(f"    {path}")

        print(
            "    수집은 계속 진행합니다."
        )


# ============================================================
# 27. 전체 결과 저장
# ============================================================

def save_aggregate(products):

    products = deduplicate_products(
        products
    )


    # JSONL
    with open(
        ALL_JSONL_PATH,
        "w",
        encoding="utf-8"
    ) as file:

        for item in products:

            file.write(
                json.dumps(
                    item,
                    ensure_ascii=False
                )
                +
                "\n"
            )


    rows = []


    for item in products:

        collection = item.get(
            "collection",
            {}
        )


        classification = item.get(
            "classification",
            {}
        )


        size_data = item.get(
            "size_data",
            {}
        )


        product = item.get(
            "product",
            {}
        )


        attrs = item.get(
            "garment_attributes",
            {}
        )


        images = (
            item
            .get(
                "media",
                {}
            )
            .get(
                "images",
                []
            )
            or
            []
        )


        quality = item.get(
            "quality",
            {}
        )


        keyword = collection.get(
            "search_keyword"
        )


        # 예전 500개에는 classification 없음
        if not classification:

            (
                gender,
                main_category,
                sub_category
            ) = classify_keyword(keyword)

        else:

            gender = classification.get(
                "gender"
            )

            main_category = classification.get(
                "main_category"
            )

            sub_category = classification.get(
                "sub_category"
            )


        first_image_url = None
        first_local_path = None


        if images:

            first_image_url = (
                images[0].get(
                    "source_url"
                )
            )

            first_local_path = (
                images[0].get(
                    "local_path"
                )
            )


        rows.append({

            "product_id":
                product.get(
                    "product_id"
                ),

            "brand":
                product.get(
                    "brand"
                ),

            "product_name":
                product.get(
                    "product_name"
                ),

            "gender":
                gender,

            "main_category":
                main_category,

            "sub_category":
                sub_category,

            "search_keyword":
                keyword,

            "price":
                product.get(
                    "price"
                ),

            "color":
                product.get(
                    "color"
                ),

            "size_labels":

                json.dumps(
                    size_data.get(
                        "size_labels",
                        []
                    ),
                    ensure_ascii=False
                ),

            "measurements":

                json.dumps(
                    size_data.get(
                        "measurements",
                        {}
                    ),
                    ensure_ascii=False
                ),

            "measurement_fields":

                json.dumps(
                    size_data.get(
                        "measurement_fields",
                        []
                    ),
                    ensure_ascii=False
                ),

            "material":
                attrs.get(
                    "material"
                ),

            "fit":
                attrs.get(
                    "fit"
                ),

            "stretch":
                attrs.get(
                    "stretch"
                ),

            "thickness":
                attrs.get(
                    "thickness"
                ),

            "model_info":

                json.dumps(
                    item.get(
                        "model_info",
                        []
                    ),
                    ensure_ascii=False
                ),

            "image_count":
                quality.get(
                    "image_count"
                ),

            "representative_image_url":
                first_image_url,

            "representative_local_path":
                first_local_path,

            "collected_at":
                collection.get(
                    "collected_at"
                ),

            "product_url":
                product.get(
                    "product_url"
                ),
        })


    safe_write_csv(
        ALL_CSV_PATH,
        rows
    )


    # ========================================================
    # SUMMARY
    # ========================================================

    brand_distribution = {}
    category_distribution = {}
    gender_distribution = {}


    for item in products:

        product = item.get(
            "product",
            {}
        )


        collection = item.get(
            "collection",
            {}
        )


        classification = item.get(
            "classification",
            {}
        )


        brand = (
            product.get("brand")
            or
            "unknown"
        )


        keyword = collection.get(
            "search_keyword"
        )


        if classification:

            gender = classification.get(
                "gender"
            )

            category = classification.get(
                "sub_category"
            )

        else:

            (
                gender,
                _,
                category
            ) = classify_keyword(
                keyword
            )


        brand_distribution[brand] = (
            brand_distribution.get(
                brand,
                0
            )
            +
            1
        )


        category_distribution[category] = (
            category_distribution.get(
                category,
                0
            )
            +
            1
        )


        gender_distribution[gender] = (
            gender_distribution.get(
                gender,
                0
            )
            +
            1
        )


    summary = {

        "target_count":
            TARGET_COUNT,

        "accepted_count":
            len(products),

        "brand_count":
            len(brand_distribution),

        "brand_distribution":

            dict(
                sorted(
                    brand_distribution.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
            ),

        "gender_distribution":
            gender_distribution,

        "category_distribution":

            dict(
                sorted(
                    category_distribution.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
            ),

        "products_with_material":

            sum(
                1
                for item in products
                if item
                .get(
                    "garment_attributes",
                    {}
                )
                .get("material")
            ),

        "products_with_fit":

            sum(
                1
                for item in products
                if item
                .get(
                    "garment_attributes",
                    {}
                )
                .get("fit")
            ),

        "products_with_stretch":

            sum(
                1
                for item in products
                if item
                .get(
                    "garment_attributes",
                    {}
                )
                .get("stretch")
            ),

        "products_with_thickness":

            sum(
                1
                for item in products
                if item
                .get(
                    "garment_attributes",
                    {}
                )
                .get("thickness")
            ),

        "products_with_model_info":

            sum(
                1
                for item in products
                if item.get(
                    "model_info"
                )
            ),

        "total_images":

            sum(
                item
                .get(
                    "quality",
                    {}
                )
                .get(
                    "image_count",
                    0
                )
                or
                0

                for item in products
            ),

        "updated_at":
            now_string()
    }


    save_json_file(
        SUMMARY_PATH,
        summary
    )


    return products


# ============================================================
# 28. 시간 통계
# ============================================================

def make_run_stats(
    status,
    run_started_at,
    elapsed,
    existing,
    attempted,
    accepted,
    rejected,
    errors,
    skipped,
    current_total,
    processed_count
):

    hours = elapsed / 3600


    acceptance_rate = (
        accepted / attempted * 100
        if attempted
        else 0
    )


    candidates_per_hour = (
        attempted / hours
        if hours > 0
        else 0
    )


    accepted_per_hour = (
        accepted / hours
        if hours > 0
        else 0
    )


    avg_candidate = (
        elapsed / attempted
        if attempted
        else 0
    )


    avg_accepted = (
        elapsed / accepted
        if accepted
        else 0
    )
    remaining = (
        max(TARGET_COUNT - current_total, 0)
        if TARGET_COUNT > 0
        else None
    )

    eta = (
        remaining / accepted_per_hour * 3600
        if remaining is not None and accepted_per_hour > 0
        else None
    )


    return {

        "status":
            status,

        "run_started_at":
            run_started_at,

        "updated_at":
            now_string(),

        "elapsed_seconds":
            round(elapsed, 2),

        "elapsed_display":
            format_duration(elapsed),

        "target":
            TARGET_COUNT,

        "existing_at_start":
            existing,

        "attempted_this_run":
            attempted,

        "accepted_this_run":
            accepted,

        "rejected_this_run":
            rejected,

        "error_this_run":
            errors,

        "skipped_existing":
            skipped,

        "current_total":
            current_total,

        "remaining_to_target":
            remaining,

        "acceptance_rate_percent":
            round(
                acceptance_rate,
                2
            ),

        "avg_candidate_seconds":
            round(
                avg_candidate,
                2
            ),

        "avg_accepted_product_seconds":
            round(
                avg_accepted,
                2
            ),

        "candidates_per_hour":
            round(
                candidates_per_hour,
                2
            ),

        "accepted_products_per_hour":
            round(
                accepted_per_hour,
                2
            ),

        "estimated_remaining_display":

            format_duration(eta)

            if eta is not None

            else None,

        "processed_url_count":
            processed_count,
    }


# ============================================================
# 29. MAIN
# ============================================================

def main():

    run_start = time.perf_counter()
    run_started_at = now_string()


    print()
    print("=" * 72)
    print("SIZE-FIRST 패션 상품 자동 수집 · SERVER MODE")
    print("=" * 72)
    print(f"원본 JSON: {RAW_DIR}")
    print(f"임시 이미지: {IMAGE_DIR}")
    print(f"로그/상태: {LOG_DIR} / {STATE_DIR}")

    print("브랜드 지정: 없음")

    print(
        "이번 단계: "
        "가디건 / 후드 / 맨투맨 / "
        "아우터 / 원피스 / 스커트 보강"
    )

    print(
        f"통과 조건: "
        f"사이즈 {MIN_SIZE_COUNT}개 이상 / "
        f"실측 필드 "
        f"{MIN_MEASUREMENT_FIELDS}개 이상"
    )
    print(f"수집 제한: {target_display()}")
    print(f"이미지 로컬 다운로드: {'ON' if DOWNLOAD_IMAGES else 'OFF (URL만 저장)'}")


    (
        existing_products,
        existing_urls,
        existing_ids

    ) = load_existing_products()


    accepted_products = list(
        existing_products
    )


    start_count = len(
        accepted_products
    )


    print()
    print(
        f"기존 상품: "
        f"{start_count}개"
    )
    if TARGET_COUNT > 0:
        print(f"추가 필요: {max(TARGET_COUNT - start_count, 0)}개")
    elif MAX_NEW_PRODUCTS > 0:
        print(f"이번 실행 신규 목표: {MAX_NEW_PRODUCTS}개")
    else:
        print("추가 필요: 총량 제한 없음")


    processed = load_processed_urls()


    bootstrap_existing_products_to_processed(
        existing_products,
        processed
    )


    print(
        f"이미 검사 완료된 URL: "
        f"{len(processed)}개"
    )


    if TARGET_COUNT > 0 and start_count >= TARGET_COUNT:

        save_aggregate(
            accepted_products
        )

        print()
        print(
            f"이미 {TARGET_COUNT}개 "
            f"목표를 채웠습니다."
        )

        return


    attempted = 0
    accepted = 0
    rejected = 0
    errors = 0
    skipped = 0

    interrupted = False


    session = requests.Session()


    session.headers.update({

        "User-Agent":
            USER_AGENT,

        "Accept-Language":
            "ko-KR,ko;q=0.9,en;q=0.8"
    })


    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=HEADLESS
            )


            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="ko-KR",
                viewport={
                    "width": 1440,
                    "height": 1000
                }
            )


            page = context.new_page()


            candidates = collect_product_links(
                page,
                existing_ids=existing_ids,
                candidate_target=100
            )


            random.shuffle(
                candidates
            )


            print()
            print(
                f"이번 실행 최초 후보: "
                f"{len(candidates)}개"
            )


            # =================================================
            # 상품 상세페이지를 열기 전에
            # 실측 API + 사이즈 옵션 API로 초고속 선별
            # =================================================

            candidates = prefilter_candidates_by_size_api(
                page,
                candidates,
                batch_size=6
            )


            print()
            print(
                f"실제 상세페이지 검사 후보: "
                f"{len(candidates)}개"
            )


            for candidate in candidates:
                if target_reached(len(accepted_products), accepted):
                    break


                url = candidate["url"]

                product_id = str(
                    candidate.get(
                        "product_id_hint",
                        ""
                    )
                )


                # 기존 승인 상품
                if (
                    url in existing_urls

                    or

                    (
                        product_id
                        and
                        product_id in existing_ids
                    )
                ):

                    skipped += 1
                    continue


                # 예전에 검사했다는 이유만으로 영구 제외하지 않음.
                # 중복 여부는 최종 승인 상품(existing_ids / existing_urls)만 기준으로 판단.
                attempted += 1


                print()
                print("=" * 72)

                print(
                    f"[후보 {attempted}] "
                    f"현재 "
                    f"{len(accepted_products)}"
                    + (f"/{TARGET_COUNT}" if TARGET_COUNT > 0 else "")
                )

                print(
                    candidate[
                        "search_keyword"
                    ]
                )

                print(url)

                print("=" * 72)


                try:

                    (
                        result,
                        reason

                    ) = collect_one_product(
                        page,
                        session,
                        candidate
                    )


                except Exception as e:

                    errors += 1

                    print(
                        "    ❌ 처리 오류: "
                        f"{e}"
                    )

                    sleep_polite()

                    continue


                # 승인
                if result:

                    product = result.get(
                        "product",
                        {}
                    )


                    result_url = (
                        product.get(
                            "product_url"
                        )
                        or
                        url
                    )


                    result_id = str(
                        product.get(
                            "product_id",
                            product_id
                        )
                    )


                    if (
                        result_url
                        in existing_urls

                        or

                        (
                            result_id
                            and
                            result_id
                            in existing_ids
                        )
                    ):

                        mark_processed(
                            processed,
                            result_url,
                            result_id,
                            "duplicate",
                            "기존 ID/URL 중복"
                        )

                        skipped += 1


                    else:

                        accepted_products.append(
                            result
                        )

                        accepted += 1


                        existing_urls.add(
                            result_url
                        )


                        if result_id:

                            existing_ids.add(
                                result_id
                            )


                        mark_processed(
                            processed,
                            result_url,
                            result_id,
                            "accepted",
                            "SIZE-FIRST 기준 통과"
                        )


                        accepted_products = save_aggregate(
                            accepted_products
                        )


                # 탈락
                else:

                    rejected += 1


                    mark_processed(
                        processed,
                        url,
                        product_id,
                        "rejected",
                        reason
                    )


                elapsed = (
                    time.perf_counter()
                    -
                    run_start
                )


                stats = make_run_stats(
                    "running",
                    run_started_at,
                    elapsed,
                    start_count,
                    attempted,
                    accepted,
                    rejected,
                    errors,
                    skipped,
                    len(
                        accepted_products
                    ),
                    len(processed)
                )


                save_current_run_stats(
                    stats
                )


                print()
                print(
                    f"    현재 총 상품: "
                    f"{len(accepted_products)}"
                    + (f"/{TARGET_COUNT}" if TARGET_COUNT > 0 else "")
                )

                print(
                    f"    이번 실행 추가: "
                    f"{accepted}개"
                )

                print(
                    f"    실행시간: "
                    f"{stats['elapsed_display']}"
                )

                print(
                    f"    시간당 승인 상품: "
                    f"{stats['accepted_products_per_hour']:.1f}개"
                )


                if stats[
                    "estimated_remaining_display"
                ]:

                    print(
                        f"    예상 남은 시간: "
                        f"{stats['estimated_remaining_display']}"
                    )


                sleep_polite()


            browser.close()


    except KeyboardInterrupt:

        interrupted = True

        print()
        print("=" * 72)
        print("수집 중단")
        print(
            "현재까지 받은 데이터는 "
            "그대로 유지됩니다."
        )
        print(
            "다시 실행하면 이어서 진행됩니다."
        )
        print("=" * 72)


    finally:

        accepted_products = save_aggregate(
            accepted_products
        )


        elapsed = (
            time.perf_counter()
            -
            run_start
        )


        status = (
            "interrupted"
            if interrupted
            else
            "completed"
        )


        final_stats = make_run_stats(
            status,
            run_started_at,
            elapsed,
            start_count,
            attempted,
            accepted,
            rejected,
            errors,
            skipped,
            len(
                accepted_products
            ),
            len(processed)
        )


        final_stats[
            "run_finished_at"
        ] = now_string()


        save_current_run_stats(
            final_stats
        )


        append_run_history(
            final_stats
        )


        print()
        print("=" * 72)
        print("최종 수집 결과")
        print("=" * 72)

        print(
            f"기존 상품          : "
            f"{start_count}개"
        )

        print(
            f"이번 실행 추가     : "
            f"{accepted}개"
        )

        print(
            f"현재 총 상품       : "
            f"{len(accepted_products)}개"
            + (f"/{TARGET_COUNT}개" if TARGET_COUNT > 0 else "")
        )

        print()

        print(
            f"검사 후보          : "
            f"{attempted}개"
        )

        print(
            f"탈락               : "
            f"{rejected}개"
        )

        print(
            f"오류               : "
            f"{errors}개"
        )

        print(
            f"기존/검사완료 스킵 : "
            f"{skipped}개"
        )

        print(
            f"승인율             : "
            f"{final_stats['acceptance_rate_percent']:.1f}%"
        )

        print()
        print("=" * 72)
        print("시간")
        print("=" * 72)

        print(
            f"이번 실행시간      : "
            f"{final_stats['elapsed_display']}"
        )

        print(
            f"후보 1개 평균      : "
            f"{final_stats['avg_candidate_seconds']:.2f}초"
        )

        print(
            f"승인상품 1개 평균  : "
            f"{final_stats['avg_accepted_product_seconds']:.2f}초"
        )

        print(
            f"시간당 상품 확보   : "
            f"{final_stats['accepted_products_per_hour']:.1f}개"
        )

        print()

        print(
            f"CSV                : "
            f"{ALL_CSV_PATH}"
        )

        print(
            f"SUMMARY            : "
            f"{SUMMARY_PATH}"
        )

        print(
            f"실행 통계          : "
            f"{RUN_STATS_PATH}"
        )


        print()
        if TARGET_COUNT > 0 and len(accepted_products) >= TARGET_COUNT:
            print(f"✅ 총 {TARGET_COUNT}개 목표 달성")
        elif MAX_NEW_PRODUCTS > 0 and accepted >= MAX_NEW_PRODUCTS:
            print(f"✅ 이번 실행 신규 {MAX_NEW_PRODUCTS}개 목표 달성")
        elif TARGET_COUNT > 0:
            print(f"남은 목표: {TARGET_COUNT - len(accepted_products)}개")
            print("다시 실행하면 이어서 수집합니다.")
        else:
            print("이번 후보 풀 검사가 끝났습니다. 다시 실행하면 새 검색 결과를 확인합니다.")


if __name__ == "__main__":
    main()