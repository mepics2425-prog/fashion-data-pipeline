import io
import json
import os
import re
import time
from pathlib import Path
from core.config import STATE_DIR, LOG_DIR, QUEUE_DIR

import requests
import pytesseract

from PIL import (
    Image,
    ImageOps,
)

from loaders import loader
from core import cm29_base as base


PENDING = QUEUE_DIR / "29cm_ocr" / "pending"

DONE = QUEUE_DIR / "29cm_ocr" / "done"

FAILED = QUEUE_DIR / "29cm_ocr" / "failed"

RESULTS = LOG_DIR / "29cm_ocr_results"


for path in [
    PENDING,
    DONE,
    FAILED,
    RESULTS,
]:
    path.mkdir(
        parents=True,
        exist_ok=True,
    )


Image.MAX_IMAGE_PIXELS = None


SIZE_FIELDS = {
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
}


def save_json(path, data):

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def metric_key(line):

    t = re.sub(
        r"\s+",
        "",
        str(line or "").lower(),
    )

    # 둘레를 먼저 검사
    if (
        "가슴둘레" in t
        or "chestcirc" in t
    ):
        return "chest_circumference_cm"

    if (
        "허리둘레" in t
        or "waistcirc" in t
    ):
        return "waist_circumference_cm"

    if (
        "힙둘레" in t
        or "엉덩이둘레" in t
    ):
        return "hip_circumference_cm"

    rules = [
        (
            [
                "총장",
                "총길이",
                "기장",
                "뒤총장",
                "앞총장",
                "length",
            ],
            "total_length_cm",
        ),
        (
            [
                "어깨",
                "shoulder",
            ],
            "shoulder_cm",
        ),
        (
            [
                "가슴단면",
                "가슴너비",
                "가슴폭",
                "가슴",
                "chest",
                "bust",
            ],
            "chest_flat_cm",
        ),
        (
            [
                "소매길이",
                "소매장",
                "sleevelength",
            ],
            "sleeve_length_cm",
        ),
        (
            [
                "암홀",
                "진동",
                "armhole",
            ],
            "armhole_cm",
        ),
        (
            [
                "소매통",
                "소매너비",
                "소매폭",
                "sleevewidth",
            ],
            "sleeve_width_cm",
        ),
        (
            [
                "허리단면",
                "허리너비",
                "허리폭",
                "허리",
                "waist",
            ],
            "waist_flat_cm",
        ),
        (
            [
                "힙단면",
                "엉덩이단면",
                "힙너비",
                "엉덩이너비",
                "힙",
                "엉덩이",
                "hip",
            ],
            "hip_flat_cm",
        ),
        (
            [
                "허벅지",
                "thigh",
            ],
            "thigh_flat_cm",
        ),
        (
            [
                "밑위",
                "rise",
            ],
            "rise_cm",
        ),
        (
            [
                "인심",
                "inseam",
            ],
            "inseam_cm",
        ),
        (
            [
                "밑단",
                "hem",
            ],
            "hem_flat_cm",
        ),
    ]

    for words, key in rules:

        if any(
            word in t
            for word in words
        ):
            return key

    return None


def get_numbers(line):

    out = []

    for raw in re.findall(
        r"(?<!\d)-?\d+(?:\.\d+)?",
        str(line or ""),
    ):

        try:
            value = float(raw)

        except Exception:
            continue

        if 0 < value <= 300:
            out.append(value)

    return out


SIZE_PATTERN = re.compile(
    r"(?<![A-Z0-9])"
    r"(XXS|XS|S|M|L|XL|XXL|2XL|3XL|"
    r"FREE|ONE\s*SIZE|OS)"
    r"(?![A-Z0-9])",
    re.I,
)


def normalize_size(value):

    value = str(
        value or ""
    ).upper().strip()

    if value in {
        "OS",
        "ONE SIZE",
    }:
        return "FREE"

    return value


def parse_ocr_text(text):

    if not text:
        return []

    lines = [
        re.sub(
            r"\s+",
            " ",
            x
        ).strip()
        for x in text.splitlines()
        if x.strip()
    ]

    if not lines:
        return []


    # ========================================================
    # 1. FREE / ONE SIZE 세로형
    # ========================================================

    joined = " ".join(
        lines
    )

    if re.search(
        r"\b(?:FREE|ONE\s*SIZE|OS)\b",
        joined,
        re.I,
    ):

        row = {
            "size_label": "FREE",
            "measurement_type":
                "garment_actual",
        }

        for line in lines:

            key = metric_key(
                line
            )

            if not key:
                continue

            nums = get_numbers(
                line
            )

            if nums:
                row[key] = nums[-1]

        if len(
            set(row)
            & SIZE_FIELDS
        ) >= 2:
            return [row]


    # ========================================================
    # 2. 사이즈 행:
    # S 68 50 52 21
    # ========================================================

    for header_i, header in enumerate(
        lines
    ):

        metric_positions = []

        for key_name in [
            "총장",
            "어깨",
            "가슴",
            "소매",
            "암홀",
            "허리",
            "힙",
            "허벅지",
            "밑위",
            "인심",
            "밑단",
        ]:

            idx = header.find(
                key_name
            )

            if idx >= 0:
                metric_positions.append(
                    (
                        idx,
                        metric_key(
                            key_name
                        ),
                    )
                )

        metric_positions = [
            x
            for x in metric_positions
            if x[1]
        ]

        metric_positions.sort()

        keys = [
            x[1]
            for x in metric_positions
        ]

        if len(keys) < 2:
            continue

        rows = []

        for line in lines[
            header_i + 1:
            header_i + 12
        ]:

            size_match = (
                SIZE_PATTERN.search(
                    line
                )
            )

            if not size_match:
                continue

            size_label = normalize_size(
                size_match.group(1)
            )

            values = get_numbers(
                line
            )

            if len(values) < len(keys):
                continue

            row = {
                "size_label":
                    size_label,

                "measurement_type":
                    "garment_actual",
            }

            for key, value in zip(
                keys,
                values[:len(keys)],
            ):
                row[key] = value

            rows.append(row)

        if rows:
            return rows


    # ========================================================
    # 3. 세로형:
    # SIZE S M L
    # 총장 68 70 72
    # 가슴 52 54 56
    # ========================================================

    for i, line in enumerate(lines):

        labels = [
            normalize_size(
                x.group(1)
            )
            for x in SIZE_PATTERN.finditer(
                line
            )
        ]

        labels = list(
            dict.fromkeys(labels)
        )

        if len(labels) < 2:
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

        for detail in lines[
            i + 1:
            i + 18
        ]:

            key = metric_key(
                detail
            )

            if not key:
                continue

            nums = get_numbers(
                detail
            )

            if len(nums) < len(labels):
                continue

            for label, value in zip(
                labels,
                nums[:len(labels)],
            ):
                rows[label][key] = value

        result = [
            row
            for row in rows.values()
            if len(
                set(row)
                & SIZE_FIELDS
            ) >= 2
        ]

        if result:
            return result


    return []


def split_image(image):

    """
    매우 긴 상세이미지를 통째로 OCR하면 느리다.

    폭을 최대 1200px로 축소한 뒤,
    이미지의 모든 영역을 위에서 아래까지
    2200px chunk로 나눠 전부 OCR한다.

    즉 '일부만 본다'가 아니라
    모든 픽셀 영역을 순차적으로 분석한다.
    """

    image = image.convert(
        "RGB"
    )

    w, h = image.size

    max_width = 1200

    if w > max_width:

        scale = (
            max_width
            / float(w)
        )

        image = image.resize(
            (
                max_width,
                max(
                    1,
                    int(h * scale)
                ),
            )
        )

    elif w < 700:

        scale = (
            900
            / float(w)
        )

        image = image.resize(
            (
                900,
                max(
                    1,
                    int(h * scale)
                ),
            )
        )

    w, h = image.size

    chunk_height = 2200
    overlap = 120

    if h <= chunk_height:
        return [image]

    chunks = []

    top = 0

    while top < h:

        bottom = min(
            h,
            top + chunk_height,
        )

        chunks.append(
            image.crop(
                (
                    0,
                    top,
                    w,
                    bottom,
                )
            )
        )

        if bottom >= h:
            break

        top = max(
            top + 1,
            bottom - overlap,
        )

    return chunks




def priority_crops(image):

    """
    일반 이미지: 전체 1회 OCR

    세로로 긴 상세 이미지:
      - 상단 20%
      - 하단 35%
    만 OCR
    """

    image = image.convert("RGB")

    w, h = image.size

    # 너무 큰 이미지는 OCR용으로 축소
    max_width = 1050

    if w > max_width:

        ratio = max_width / float(w)

        image = image.resize(
            (
                max_width,
                max(
                    1,
                    int(h * ratio)
                )
            )
        )

    elif w < 650:

        ratio = 850 / float(w)

        image = image.resize(
            (
                850,
                max(
                    1,
                    int(h * ratio)
                )
            )
        )

    w, h = image.size

    # 일반 이미지
    if h <= w * 2.2:
        return [image]

    # 세로 긴 이미지
    top_end = max(
        1,
        int(h * 0.20)
    )

    bottom_start = max(
        0,
        int(h * 0.65)
    )

    return [
        image.crop(
            (
                0,
                0,
                w,
                top_end
            )
        ),

        image.crop(
            (
                0,
                bottom_start,
                w,
                h
            )
        ),
    ]



FAST_KEYWORDS = (
    "size", "사이즈", "실측", "cm",
    "총장", "기장", "어깨", "가슴",
    "소매", "암홀", "허리", "힙",
    "엉덩이", "허벅지", "밑위",
    "인심", "밑단",
    "length", "shoulder", "chest",
    "bust", "sleeve", "armhole",
    "waist", "hip", "thigh",
    "rise", "inseam", "hem",
)


def _resize_width(image, width):

    image = image.convert("RGB")

    w, h = image.size

    if not w or not h:
        return image

    ratio = width / float(w)

    return image.resize(
        (
            width,
            max(1, int(h * ratio)),
        )
    )


def _fast_score(text):

    t = re.sub(
        r"\s+",
        " ",
        str(text or "").lower(),
    )

    if not t:
        return 0

    score = 0

    hits = sum(
        1
        for word in FAST_KEYWORDS
        if word in t
    )

    score += min(hits, 6) * 2

    if re.search(
        r"\b(?:xxs|xs|s|m|l|xl|xxl|"
        r"free|one\s*size)\b",
        t,
        re.I,
    ):
        score += 3

    if re.search(
        r"\bcm\b",
        t,
        re.I,
    ):
        score += 2

    nums = re.findall(
        r"(?<!\d)\d{1,3}(?:\.\d+)?",
        t,
    )

    if len(nums) >= 3:
        score += 2

    if len(nums) >= 6:
        score += 2

    return score


def _make_fast_tiles(image):

    """
    정밀 OCR 전에 저해상도로 빠르게 훑는다.

    긴 상세 이미지:
      상단 20%
      하단 35%

    각 영역을 폭 460px로 축소하고
    작은 타일로 분리한다.
    """

    image = image.convert("RGB")

    w, h = image.size

    regions = []

    if h <= w * 2.2:

        regions.append(
            (0, 0, w, h, "full")
        )

    else:

        top_end = max(
            1,
            int(h * 0.20)
        )

        bottom_start = max(
            0,
            int(h * 0.65)
        )

        regions.append(
            (0, 0, w, top_end, "top")
        )

        regions.append(
            (
                0,
                bottom_start,
                w,
                h,
                "bottom",
            )
        )

    tiles = []

    for (
        left,
        top,
        right,
        bottom,
        region_name,
    ) in regions:

        crop = image.crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

        small = _resize_width(
            crop,
            320,
        )

        sw, sh = small.size

        tile_height = 900
        overlap = 80

        if sh <= tile_height:

            tiles.append(
                {
                    "image": small,
                    "region": region_name,
                    "src_top": top,
                    "src_bottom": bottom,
                }
            )

            continue

        pos = 0

        while pos < sh:

            end = min(
                sh,
                pos + tile_height,
            )

            tile = small.crop(
                (
                    0,
                    pos,
                    sw,
                    end,
                )
            )

            rel_top = (
                pos / float(sh)
            )

            rel_bottom = (
                end / float(sh)
            )

            src_top = int(
                top
                + (bottom - top)
                * rel_top
            )

            src_bottom = int(
                top
                + (bottom - top)
                * rel_bottom
            )

            tiles.append(
                {
                    "image": tile,
                    "region": region_name,
                    "src_top": src_top,
                    "src_bottom": src_bottom,
                }
            )

            if end >= sh:
                break

            pos = max(
                pos + 1,
                end - overlap,
            )

    return tiles


def _fine_crop(image, info):

    """
    FAST OCR에서 실측표 가능성이 높은 부분만
    원본에서 다시 잘라 정밀 OCR한다.
    """

    image = image.convert("RGB")

    w, h = image.size

    top = max(
        0,
        int(info["src_top"])
    )

    bottom = min(
        h,
        int(info["src_bottom"])
    )

    height = max(
        1,
        bottom - top,
    )

    pad = max(
        80,
        int(height * 0.12),
    )

    top = max(
        0,
        top - pad,
    )

    bottom = min(
        h,
        bottom + pad,
    )

    crop = image.crop(
        (
            0,
            top,
            w,
            bottom,
        )
    )

    cw, _ = crop.size

    if cw > 1100:

        crop = _resize_width(
            crop,
            1100,
        )

    elif cw < 700:

        crop = _resize_width(
            crop,
            850,
        )

    return crop


def ocr_one_image(
    session,
    image_url,
):

    import hashlib

    started = time.perf_counter()

    old_cache_root = STATE_DIR / "29cm_ocr_image_cache_v1"

    cache_root = STATE_DIR / "29cm_ocr_image_cache_v2"

    cache_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    response = session.get(
        image_url,
        timeout=15,
    )

    response.raise_for_status()

    content = response.content

    if len(content) < 5000:

        return "", []

    image_hash = hashlib.sha1(
        content
    ).hexdigest()

    cache_file = (
        cache_root
        / f"{image_hash}.json"
    )

    # ========================================================
    # V2 캐시
    # ========================================================

    if cache_file.exists():

        try:

            cached = json.loads(
                cache_file.read_text(
                    encoding="utf-8"
                )
            )

            print(
                "   ♻ OCR CACHE V2 HIT",
                flush=True,
            )

            return (
                cached.get("text")
                or "",

                cached.get("sizes")
                or [],
            )

        except Exception:
            pass

    # ========================================================
    # 기존 V1 OCR에서 이미 성공했던 이미지는 재사용
    #
    # 실패했던 V1 결과는 새 방식으로 다시 검사
    # ========================================================

    old_cache_file = (
        old_cache_root
        / f"{image_hash}.json"
    )

    if old_cache_file.exists():

        try:

            old_cached = json.loads(
                old_cache_file.read_text(
                    encoding="utf-8"
                )
            )

            old_sizes = (
                old_cached.get("sizes")
                or []
            )

            if old_sizes:

                print(
                    "   ♻ V1 성공 OCR 재사용",
                    flush=True,
                )

                cache_file.write_text(
                    json.dumps(
                        {
                            "text":
                                old_cached.get(
                                    "text"
                                )
                                or "",

                            "sizes":
                                old_sizes,

                            "mode":
                                "v1_success_reuse",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                return (
                    old_cached.get(
                        "text"
                    )
                    or "",

                    old_sizes,
                )

        except Exception:
            pass

    # ========================================================
    # 이미지 읽기
    # ========================================================

    image = Image.open(
        io.BytesIO(
            content
        )
    )

    try:

        image = (
            ImageOps.exif_transpose(
                image
            )
        )

    except Exception:
        pass

    image = image.convert(
        "RGB"
    )

    # ========================================================
    # 1단계
    # 저해상도 FAST OCR
    # ========================================================

    tiles = _make_fast_tiles(
        image
    )

    scored = []
    fast_texts = []

    for index, info in enumerate(
        tiles,
        start=1,
    ):

        small = ImageOps.grayscale(
            info["image"]
        )

        small = ImageOps.autocontrast(
            small
        )

        try:

            text = (
                pytesseract.image_to_string(
                    small,
                    lang="eng",
                    config="--oem 1 --psm 11",
                    timeout=3,
                )
            )

        except RuntimeError:

            print(
                f"   ⚠ FAST timeout "
                f"{index}/{len(tiles)}",
                flush=True,
            )

            continue

        except Exception as e:

            print(
                f"   ⚠ FAST OCR 오류: {e}",
                flush=True,
            )

            continue

        score = _fast_score(
            text
        )

        fast_texts.append(
            text
        )

        item = dict(
            info
        )

        item["score"] = score

        scored.append(
            item
        )

        print(
            f"   🔎 FAST "
            f"{index}/{len(tiles)} "
            f"score={score}",
            flush=True,
        )

    # 점수가 높은 영역부터
    scored.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    candidates = [
        x
        for x in scored
        if x["score"] >= 4
    ]

    # FAST OCR이 조금 틀렸더라도
    # 약간의 실측표 신호가 있으면
    # 최고점 1개는 정밀 OCR
    if (
        not candidates
        and scored
        and scored[0]["score"] >= 2
    ):

        candidates = [
            scored[0]
        ]

    # ========================================================
    # 서로 거의 같은 영역 제거
    # 정밀 OCR은 최대 2곳
    # ========================================================

    selected = []

    for candidate in candidates:

        center = (
            candidate["src_top"]
            + candidate["src_bottom"]
        ) / 2.0

        duplicate = False

        for previous in selected:

            previous_center = (
                previous["src_top"]
                + previous["src_bottom"]
            ) / 2.0

            if abs(
                center
                - previous_center
            ) < 300:

                duplicate = True
                break

        if not duplicate:

            selected.append(
                candidate
            )

        if len(selected) >= 1:
            break

    # ========================================================
    # 실측표 후보 자체가 없으면 종료
    # ========================================================

    if not selected:

        final_text = "\n".join(
            fast_texts
        )

        elapsed = (
            time.perf_counter()
            - started
        )

        try:

            cache_file.write_text(
                json.dumps(
                    {
                        "text":
                            final_text,

                        "sizes":
                            [],

                        "mode":
                            "fast_no_candidate",

                        "elapsed_sec":
                            round(
                                elapsed,
                                2,
                            ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        except Exception:
            pass

        print(
            f"   ⏩ 실측표 후보 없음 "
            f"{elapsed:.1f}s",
            flush=True,
        )

        return (
            final_text,
            [],
        )

    # ========================================================
    # 2단계
    # 후보 최대 2곳만 정밀 OCR
    # ========================================================

    fine_texts = []
    parsed = []

    for index, candidate in enumerate(
        selected,
        start=1,
    ):

        crop = _fine_crop(
            image,
            candidate,
        )

        gray = ImageOps.grayscale(
            crop
        )

        gray = ImageOps.autocontrast(
            gray
        )

        # 표 형태에 따라 psm 6 / 11 둘 다 대응
        for config in (
            "--psm 6",
            "--psm 11",
        ):

            try:

                text = (
                    pytesseract.image_to_string(
                        gray,
                        lang="kor+eng",
                        config=config,
                        timeout=18,
                    )
                )

            except RuntimeError:

                print(
                    f"   ⚠ FINE timeout "
                    f"{index}/{len(selected)} "
                    f"{config}",
                    flush=True,
                )

                continue

            except Exception as e:

                print(
                    f"   ⚠ FINE OCR 오류: {e}",
                    flush=True,
                )

                continue

            fine_texts.append(
                text
            )

            sizes = parse_ocr_text(
                text
            )

            if sizes:

                parsed.extend(
                    sizes
                )

                break

        # 하나에서 실측 데이터가 정상 파싱됐으면
        # 두 번째 후보까지 읽지 않고 종료
        if parsed:
            break

    final_text = "\n".join(
        fast_texts
        + fine_texts
    )

    elapsed = (
        time.perf_counter()
        - started
    )

    try:

        cache_file.write_text(
            json.dumps(
                {
                    "text":
                        final_text,

                    "sizes":
                        parsed,

                    "mode":
                        "fast_then_fine",

                    "elapsed_sec":
                        round(
                            elapsed,
                            2,
                        ),

                    "fast_tiles":
                        len(tiles),

                    "fine_candidates":
                        len(selected),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    except Exception:
        pass

    print(
        f"   ⏱ OCR 이미지 "
        f"{elapsed:.1f}s "
        f"(FAST {len(tiles)}개 / "
        f"FINE {len(selected)}개 / "
        f"size rows {len(parsed)})",
        flush=True,
    )

    return (
        final_text,
        parsed,
    )




def ocr_images_batch(session, images):
    """
    상품의 여러 이미지를 각각 OCR하지 않는다.

    1) 최대 7장의 저해상도 미리보기를 한 장으로 합침
    2) Tesseract를 딱 1회 돌려 실측표 가능성이 높은 이미지 선택
    3) 선택된 원본 이미지 1장만 정밀 OCR
    """

    import hashlib

    started = time.perf_counter()

    cache_root = STATE_DIR / "29cm_ocr_batch_v1"
    cache_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    prepared = []

    # ========================================================
    # 이미지 다운로드 + 미리보기 생성
    # ========================================================

    for image_index, image_info in enumerate(
        images,
        start=1,
    ):

        url = image_info.get("url")

        if not url:
            continue

        try:

            response = session.get(
                url,
                timeout=15,
            )
            response.raise_for_status()

            content = response.content

            if len(content) < 5000:
                continue

            image_hash = hashlib.sha1(
                content
            ).hexdigest()

            cache_file = (
                cache_root
                / f"{image_hash}.json"
            )

            # 이미 성공했던 결과 재사용
            if cache_file.exists():

                try:

                    cached = json.loads(
                        cache_file.read_text(
                            encoding="utf-8"
                        )
                    )

                    if cached.get("sizes"):

                        print(
                            f"   ♻ BATCH CACHE HIT "
                            f"image={image_index}",
                            flush=True,
                        )

                        return (
                            [
                                {
                                    "url": url,
                                    "ok": True,
                                    "measurement_found": True,
                                    "ocr_preview":
                                        cached.get("text", "")[:2500],
                                }
                            ],
                            cached["sizes"],
                        )

                except Exception:
                    pass

            pil = Image.open(
                io.BytesIO(content)
            )

            try:
                pil = ImageOps.exif_transpose(pil)
            except Exception:
                pass

            pil = pil.convert("RGB")

            w, h = pil.size

            # -----------------------------------------------
            # 탐색용 영역
            # 일반 이미지: 전체
            # 긴 이미지: 상단 + 하단
            # -----------------------------------------------

            regions = []

            if h <= w * 2.2:

                regions.append(
                    (0, h, "full")
                )

            else:

                regions.append(
                    (
                        0,
                        max(1, int(h * 0.22)),
                        "top",
                    )
                )

                regions.append(
                    (
                        max(0, int(h * 0.62)),
                        h,
                        "bottom",
                    )
                )

            for y1, y2, region_name in regions:

                crop = pil.crop(
                    (
                        0,
                        y1,
                        w,
                        y2,
                    )
                )

                cw, ch = crop.size

                # 탐색용은 작게
                target_w = 300

                ratio = target_w / float(cw)

                crop = crop.resize(
                    (
                        target_w,
                        max(
                            1,
                            int(ch * ratio)
                        ),
                    )
                )

                # 지나치게 긴 미리보기는
                # 끝부분 650px만 사용
                pw, ph = crop.size

                if ph > 650:

                    if region_name == "bottom":

                        crop = crop.crop(
                            (
                                0,
                                ph - 650,
                                pw,
                                ph,
                            )
                        )

                    else:

                        crop = crop.crop(
                            (
                                0,
                                0,
                                pw,
                                650,
                            )
                        )

                prepared.append(
                    {
                        "index": image_index,
                        "url": url,
                        "hash": image_hash,
                        "cache_file": cache_file,
                        "content": content,
                        "source_image": pil,
                        "preview": crop,
                        "source_y1": y1,
                        "source_y2": y2,
                        "region": region_name,
                    }
                )

        except Exception as e:

            print(
                f"   ⚠ 이미지 준비 실패 "
                f"{image_index}: {e}",
                flush=True,
            )


    if not prepared:

        return [], []


    # ========================================================
    # CONTACT SHEET 생성
    # ========================================================

    cell_w = 320
    cell_h = 700
    columns = 3

    rows = (
        len(prepared)
        + columns - 1
    ) // columns

    sheet = Image.new(
        "RGB",
        (
            cell_w * columns,
            cell_h * rows,
        ),
        "white",
    )

    positions = []

    for n, item in enumerate(prepared):

        col = n % columns
        row = n // columns

        x = col * cell_w
        y = row * cell_h

        preview = item["preview"]

        pw, ph = preview.size

        sheet.paste(
            preview,
            (
                x + 10,
                y + 10,
            ),
        )

        positions.append(
            {
                "item": item,
                "x1": x,
                "y1": y,
                "x2": x + cell_w,
                "y2": y + cell_h,
            }
        )


    sheet = ImageOps.grayscale(
        sheet
    )

    sheet = ImageOps.autocontrast(
        sheet
    )


    # ========================================================
    # TESSERACT 딱 1회
    # ========================================================

    print(
        f"   🚀 BATCH FAST OCR "
        f"{len(prepared)} regions / "
        f"{len(images)} images",
        flush=True,
    )

    try:

        data = pytesseract.image_to_data(
            sheet,
            lang="kor+eng",
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
            timeout=25,
        )

    except RuntimeError:

        print(
            "   ⚠ BATCH FAST timeout",
            flush=True,
        )

        return [], []

    except Exception as e:

        print(
            f"   ⚠ BATCH OCR 오류: {e}",
            flush=True,
        )

        return [], []


    # ========================================================
    # OCR 결과를 각 이미지 영역별로 다시 배분
    # ========================================================

    texts = [
        []
        for _ in positions
    ]

    count = len(
        data.get("text", [])
    )

    for i in range(count):

        word = str(
            data["text"][i] or ""
        ).strip()

        if not word:
            continue

        try:

            x = int(data["left"][i])
            y = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])

        except Exception:
            continue

        cx = x + w / 2
        cy = y + h / 2

        for pos_index, pos in enumerate(
            positions
        ):

            if (
                pos["x1"] <= cx < pos["x2"]
                and
                pos["y1"] <= cy < pos["y2"]
            ):

                texts[pos_index].append(
                    word
                )
                break


    def candidate_score(text):

        t = str(text or "").lower()

        score = 0

        keywords = [
            "size",
            "cm",
            "사이즈",
            "실측",
            "총장",
            "기장",
            "어깨",
            "가슴",
            "소매",
            "암홀",
            "허리",
            "힙",
            "엉덩이",
            "허벅지",
            "밑위",
            "밑단",
            "length",
            "shoulder",
            "chest",
            "waist",
            "hip",
        ]

        for keyword in keywords:

            if keyword in t:
                score += 2

        if re.search(
            r"\b(?:xs|s|m|l|xl|xxl|free)\b",
            t,
            re.I,
        ):
            score += 3

        numbers = re.findall(
            r"\d+(?:\.\d+)?",
            t,
        )

        if len(numbers) >= 3:
            score += 2

        if len(numbers) >= 6:
            score += 2

        return score


    ranked = []

    for i, pos in enumerate(positions):

        text = " ".join(
            texts[i]
        )

        score = candidate_score(
            text
        )

        ranked.append(
            (
                score,
                pos,
                text,
            )
        )

        print(
            f"      image="
            f"{pos['item']['index']} "
            f"{pos['item']['region']} "
            f"score={score}",
            flush=True,
        )


    ranked.sort(
        key=lambda x: x[0],
        reverse=True,
    )


    # ========================================================
    # 최고 후보만 정밀 OCR
    # ========================================================

    if not ranked:

        return [], []


    best_score, best_pos, fast_text = (
        ranked[0]
    )

    # 최소한의 신호도 없으면
    # 정밀 OCR을 하지 않는다.
    if best_score < 2:

        elapsed = (
            time.perf_counter()
            - started
        )

        print(
            f"   ⏩ 실측표 후보 없음 "
            f"{elapsed:.1f}s",
            flush=True,
        )

        return [], []


    item = best_pos["item"]

    source = item["source_image"]

    sw, sh = source.size

    y1 = max(
        0,
        int(item["source_y1"])
    )

    y2 = min(
        sh,
        int(item["source_y2"])
    )

    padding = max(
        80,
        int((y2 - y1) * 0.08),
    )

    y1 = max(
        0,
        y1 - padding,
    )

    y2 = min(
        sh,
        y2 + padding,
    )

    fine = source.crop(
        (
            0,
            y1,
            sw,
            y2,
        )
    )

    fw, fh = fine.size

    if fw > 1000:

        ratio = 1000 / float(fw)

        fine = fine.resize(
            (
                1000,
                max(
                    1,
                    int(fh * ratio)
                ),
            )
        )

    elif fw < 700:

        ratio = 800 / float(fw)

        fine = fine.resize(
            (
                800,
                max(
                    1,
                    int(fh * ratio)
                ),
            )
        )


    fine = ImageOps.grayscale(
        fine
    )

    fine = ImageOps.autocontrast(
        fine
    )


    print(
        f"   🎯 FINE OCR "
        f"image={item['index']} "
        f"{item['region']} "
        f"score={best_score}",
        flush=True,
    )


    try:

        fine_text = (
            pytesseract.image_to_string(
                fine,
                lang="kor+eng",
                config="--psm 6",
                timeout=30,
            )
        )

    except RuntimeError:

        print(
            "   ⚠ FINE timeout",
            flush=True,
        )

        return [], []

    except Exception as e:

        print(
            f"   ⚠ FINE OCR 오류: {e}",
            flush=True,
        )

        return [], []


    sizes = parse_ocr_text(
        fine_text
    )


    # psm6에서 파싱 실패했을 때만
    # 한 번 추가 시도
    if not sizes:

        try:

            second_text = (
                pytesseract.image_to_string(
                    fine,
                    lang="kor+eng",
                    config="--psm 11",
                    timeout=25,
                )
            )

            second_sizes = (
                parse_ocr_text(
                    second_text
                )
            )

            if second_sizes:

                fine_text = second_text
                sizes = second_sizes

        except Exception:
            pass


    elapsed = (
        time.perf_counter()
        - started
    )


    try:

        item["cache_file"].write_text(
            json.dumps(
                {
                    "text": fine_text,
                    "sizes": sizes,
                    "mode": "batch_v1",
                    "elapsed_sec":
                        round(elapsed, 2),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    except Exception:
        pass


    print(
        f"   ✅ BATCH OCR END "
        f"{elapsed:.1f}s "
        f"| image={item['index']} "
        f"| sizes={len(sizes)}",
        flush=True,
    )


    image_results = []

    for original in images:

        url = original.get("url")

        image_results.append(
            {
                "url": url,
                "ok": True,
                "measurement_found":
                    (
                        url == item["url"]
                        and bool(sizes)
                    ),
                "ocr_preview":
                    (
                        fine_text[:2500]
                        if url == item["url"]
                        else ""
                    ),
            }
        )


    return (
        image_results,
        sizes,
    )




def ocr_visual_selected(
    session,
    images,
):

    """
    이미 OCR 없는 시각 분석으로 선택된
    최대 4장만 처리.

    contact sheet 없음.
    FAST OCR 없음.

    각 이미지에서 가장 표처럼 보이는
    세로 구간 하나를 선택한 뒤
    고해상도 OCR을 수행한다.
    """

    from PIL import (
        ImageFilter,
        ImageStat,
    )

    import hashlib


    started_all = (
        time.perf_counter()
    )

    cache_root = STATE_DIR / "29cm_ocr_visual_v1"

    cache_root.mkdir(
        parents=True,
        exist_ok=True,
    )


    image_results = []
    all_sizes = []


    def crop_visual_score(
        gray
    ):

        hist = gray.histogram()

        pixels = max(
            1,
            sum(hist)
        )

        white_ratio = (
            sum(hist[220:])
            / pixels
        )

        dark_ratio = (
            sum(hist[:120])
            / pixels
        )

        edges = gray.filter(
            ImageFilter.FIND_EDGES
        )

        edge_mean = (
            ImageStat.Stat(
                edges
            ).mean[0]
            / 255.0
        )

        score = 0.0

        if white_ratio >= 0.45:
            score += 2

        if white_ratio >= 0.65:
            score += 2

        if (
            0.015
            <= dark_ratio
            <= 0.35
        ):
            score += 3

        score += min(
            edge_mean * 18,
            5,
        )

        return score


    for rank, img in enumerate(
        images,
        start=1,
    ):

        url = str(
            img.get("url")
            or ""
        )

        original_index = (
            img.get(
                "_original_index"
            )
            or rank
        )


        if not url:
            continue


        print(
            f"   🎯 후보 {rank}/{len(images)}"
            f" | 원본 image={original_index}",
            flush=True,
        )


        try:

            response = session.get(
                url,
                timeout=15,
            )

            response.raise_for_status()

            content = response.content


            if len(content) < 5000:
                continue


            image_hash = (
                hashlib.sha1(
                    content
                ).hexdigest()
            )

            cache_file = (
                cache_root
                / f"{image_hash}.json"
            )


            # -----------------------------------------------
            # 새 알고리즘 캐시
            # -----------------------------------------------

            if cache_file.exists():

                try:

                    cached = json.loads(
                        cache_file.read_text(
                            encoding="utf-8"
                        )
                    )

                    cached_sizes = (
                        cached.get(
                            "sizes"
                        )
                        or []
                    )

                    image_results.append({
                        "url": url,
                        "ok": True,
                        "measurement_found":
                            bool(
                                cached_sizes
                            ),
                        "ocr_preview":
                            (
                                cached.get(
                                    "text"
                                )
                                or ""
                            )[:2500],
                    })


                    if cached_sizes:

                        print(
                            "      ♻ VISUAL CACHE HIT "
                            f"sizes={len(cached_sizes)}",
                            flush=True,
                        )

                        all_sizes.extend(
                            cached_sizes
                        )

                        break

                    continue

                except Exception:
                    pass


            pil = Image.open(
                io.BytesIO(
                    content
                )
            )


            try:

                pil = (
                    ImageOps.exif_transpose(
                        pil
                    )
                )

            except Exception:
                pass


            pil = pil.convert(
                "RGB"
            )


            # ===============================================
            # OCR용 해상도
            #
            # contact sheet처럼 300px까지
            # 뭉개지 않는다.
            # ===============================================

            w, h = pil.size

            target_w = 900

            if w != target_w:

                ratio = (
                    target_w
                    / float(w)
                )

                pil = pil.resize(
                    (
                        target_w,
                        max(
                            1,
                            int(h * ratio)
                        ),
                    )
                )


            w, h = pil.size


            # ===============================================
            # 긴 이미지라면 전체에서
            # 1400px 구간들을 만들고
            # OCR 없이 가장 표 같은 구간 선택
            # ===============================================

            chunk_h = 1400
            overlap = 150

            chunks = []


            if h <= chunk_h:

                chunks.append(
                    (
                        0,
                        h,
                        pil,
                    )
                )

            else:

                top = 0

                while top < h:

                    bottom = min(
                        h,
                        top + chunk_h,
                    )

                    crop = pil.crop(
                        (
                            0,
                            top,
                            w,
                            bottom,
                        )
                    )

                    chunks.append(
                        (
                            top,
                            bottom,
                            crop,
                        )
                    )

                    if bottom >= h:
                        break

                    top = max(
                        top + 1,
                        bottom - overlap,
                    )


            ranked_chunks = []


            for (
                top,
                bottom,
                crop,
            ) in chunks:

                gray_preview = (
                    ImageOps.grayscale(
                        crop
                    )
                )

                gray_preview = (
                    ImageOps.autocontrast(
                        gray_preview
                    )
                )

                score = (
                    crop_visual_score(
                        gray_preview
                    )
                )

                ranked_chunks.append(
                    (
                        score,
                        top,
                        bottom,
                        crop,
                    )
                )


            ranked_chunks.sort(
                key=lambda x: x[0],
                reverse=True,
            )


            (
                best_score,
                best_top,
                best_bottom,
                fine,
            ) = ranked_chunks[0]


            print(
                f"      구간 "
                f"{best_top}-{best_bottom}"
                f" visual={best_score:.2f}",
                flush=True,
            )


            gray = ImageOps.grayscale(
                fine
            )

            gray = ImageOps.autocontrast(
                gray
            )


            # ===============================================
            # 여기서 처음으로 OCR 수행
            # ===============================================

            started = (
                time.perf_counter()
            )


            try:

                text = (
                    pytesseract.image_to_string(
                        gray,
                        lang="kor+eng",
                        config="--psm 6",
                        timeout=30,
                    )
                )

            except RuntimeError:

                print(
                    "      ⚠ FINE OCR timeout",
                    flush=True,
                )

                image_results.append({
                    "url": url,
                    "ok": False,
                    "error":
                        "fine OCR timeout",
                })

                continue


            sizes = parse_ocr_text(
                text
            )


            # -----------------------------------------------
            # psm6에서 표를 놓친 경우에만
            # psm11 한 번 추가
            # -----------------------------------------------

            if not sizes:

                try:

                    text2 = (
                        pytesseract.image_to_string(
                            gray,
                            lang="kor+eng",
                            config="--psm 11",
                            timeout=25,
                        )
                    )

                    sizes2 = (
                        parse_ocr_text(
                            text2
                        )
                    )

                    if sizes2:

                        text = text2
                        sizes = sizes2

                except Exception:
                    pass


            elapsed = (
                time.perf_counter()
                - started
            )


            print(
                f"      OCR {elapsed:.1f}s"
                f" | sizes={len(sizes)}",
                flush=True,
            )


            try:

                cache_file.write_text(
                    json.dumps(
                        {
                            "text":
                                text,

                            "sizes":
                                sizes,

                            "visual_score":
                                best_score,

                            "chunk":
                                [
                                    best_top,
                                    best_bottom,
                                ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            except Exception:
                pass


            image_results.append({
                "url": url,
                "ok": True,
                "measurement_found":
                    bool(sizes),
                "ocr_preview":
                    text[:2500],
            })


            # 실측 찾았으면 나머지 이미지 OCR 안 함
            if sizes:

                all_sizes.extend(
                    sizes
                )

                print(
                    f"   ✅ 실측 발견 "
                    f"image={original_index}"
                    f" → 나머지 OCR 생략",
                    flush=True,
                )

                break


        except Exception as e:

            print(
                f"      ⚠ "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

            image_results.append({
                "url": url,
                "ok": False,
                "error":
                    f"{type(e).__name__}: {e}",
            })


    elapsed_all = (
        time.perf_counter()
        - started_all
    )


    print(
        f"   ⏱ VISUAL→OCR 전체 "
        f"{elapsed_all:.1f}s"
        f" | sizes={len(all_sizes)}",
        flush=True,
    )


    return (
        image_results,
        all_sizes,
    )



def merge_sizes(all_rows):

    merged = {}

    for row in all_rows:

        label = str(
            row.get(
                "size_label"
            )
            or ""
        ).strip()

        if not label:
            continue

        target = merged.setdefault(
            label,
            {
                "size_label":
                    label,

                "measurement_type":
                    "garment_actual",
            },
        )

        for key, value in row.items():

            if key in {
                "size_label",
                "measurement_type",
            }:
                continue

            if (
                value is not None
                and target.get(key)
                is None
            ):
                target[key] = value

    return list(
        merged.values()
    )


def make_raw(job, sizes):

    measurements = {}

    labels = []

    for row in sizes:

        label = row[
            "size_label"
        ]

        labels.append(label)

        measurements[label] = {
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
            "platform": "29cm",
        },

        "collection": {
            "search_keyword":
                "image_ocr",
        },

        "classification": {
            "main_category":
                job.get(
                    "main_category"
                )
                or "기타",
        },

        "product": {
            "product_id":
                str(
                    job[
                        "platform_product_id"
                    ]
                ),

            "brand":
                job.get("brand"),

            "product_name":
                job.get(
                    "product_name"
                ),

            "price":
                job.get("price"),

            "product_url":
                job.get(
                    "product_url"
                ),
        },

        "garment_attributes": {},

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


def existing_size_count(
    conn,
    pid,
):

    with conn.cursor() as cur:

        cur.execute(
            """
            SELECT
                p.id,
                COUNT(ps.id)
            FROM products p
            LEFT JOIN product_sizes ps
              ON ps.product_id = p.id
            WHERE
                p.platform = '29cm'
                AND p.platform_product_id = %s
            GROUP BY p.id
            """,
            (
                str(pid),
            ),
        )

        row = cur.fetchone()

    if not row:
        return None, 0

    return (
        row[0],
        int(
            row[1]
            or 0
        ),
    )


def supplement_sizes(
    conn,
    product_id,
    sizes,
):

    columns = [
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

    with conn.cursor() as cur:

        for row in sizes:

            values = [
                product_id,
                row["size_label"],
                "garment_actual",
            ]

            values += [
                row.get(col)
                for col in columns
            ]

            placeholders = ",".join(
                ["%s"] * len(values)
            )

            updates = ",".join(
                f"{col}=COALESCE("
                f"{col},VALUES({col}))"
                for col in columns
            )

            cur.execute(
                f"""
                INSERT INTO product_sizes
                (
                    product_id,
                    size_label,
                    measurement_type,
                    {",".join(columns)}
                )
                VALUES ({placeholders})
                ON DUPLICATE KEY UPDATE
                    {updates}
                """,
                values,
            )

    conn.commit()


def process_job(path):

    job = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    pid = str(
        job[
            "platform_product_id"
        ]
    )

    images = (
        job.get("images")
        or []
    )

    # ========================================================
    # FAST OCR
    # 처음 2장 + 마지막 4장 + 가운데 1장만 검사
    # ========================================================

    try:
        images = sorted(
            images,
            key=lambda x: float(
                x.get("y") or 0
            )
        )
    except Exception:
        pass

    original_images = images

    selected = []

    # ========================================================
    # OCR 없이 전체 이미지 시각 구조만 분석
    #
    # 기존:
    #   앞 2 + 뒤 4 + 가운데 1
    #
    # 변경:
    #   전체 이미지 조사
    #   → 표/텍스트 이미지 가능성이 높은 TOP 2
    #   → 안전장치로 마지막 2장 추가
    # ========================================================

    from concurrent.futures import (
        ThreadPoolExecutor,
        as_completed,
    )

    from PIL import (
        ImageFilter,
        ImageStat,
    )


    def visual_table_score(pil):

        """
        OCR을 전혀 하지 않고
        '표/텍스트 이미지처럼 보이는가'만 판별.

        사진보다:
        - 흰 배경 비율
        - 검은 글자 비율
        - 작은 edge 밀도
        - 낮거나 중간 정도 entropy

        가 높은 영역을 선호한다.
        """

        try:

            pil = ImageOps.exif_transpose(
                pil
            )

        except Exception:
            pass


        pil = pil.convert("RGB")

        w, h = pil.size

        if not w or not h:
            return 0.0


        # 탐색용이라 아주 작게
        target_w = 240

        ratio = (
            target_w
            / float(w)
        )

        small = pil.resize(
            (
                target_w,
                max(
                    1,
                    int(h * ratio)
                ),
            )
        )

        sw, sh = small.size


        # 긴 이미지는 작은 구간들로 나눠
        # 가장 '표 같은' 부분의 점수를 사용
        tile_h = 360
        overlap = 60

        tiles = []

        if sh <= tile_h:

            tiles = [
                small
            ]

        else:

            y = 0

            while y < sh:

                bottom = min(
                    sh,
                    y + tile_h,
                )

                tiles.append(
                    small.crop(
                        (
                            0,
                            y,
                            sw,
                            bottom,
                        )
                    )
                )

                if bottom >= sh:
                    break

                y = max(
                    y + 1,
                    bottom - overlap,
                )


        scores = []

        for tile in tiles:

            gray = ImageOps.grayscale(
                tile
            )

            gray = ImageOps.autocontrast(
                gray
            )

            hist = gray.histogram()

            pixels = max(
                1,
                sum(hist)
            )

            white_ratio = (
                sum(hist[220:])
                / pixels
            )

            dark_ratio = (
                sum(hist[:120])
                / pixels
            )

            edges = gray.filter(
                ImageFilter.FIND_EDGES
            )

            edge_mean = (
                ImageStat.Stat(
                    edges
                ).mean[0]
                / 255.0
            )

            try:

                entropy = gray.entropy()

            except Exception:

                entropy = 8.0


            score = 0.0

            # 표/문서류는 보통 흰 배경 비율이 높음
            if white_ratio >= 0.45:
                score += 2.0

            if white_ratio >= 0.65:
                score += 2.5


            # 검은 글자/선이 적당히 존재
            if (
                0.015
                <= dark_ratio
                <= 0.35
            ):
                score += 3.0


            # 글자와 표 선의 edge
            score += min(
                edge_mean * 18.0,
                5.0,
            )


            # 사진보다 문서/표 이미지에 약간 유리
            if entropy <= 6.5:
                score += 1.5

            if entropy <= 5.0:
                score += 1.0


            # 완전 백지 비슷하면 감점
            if dark_ratio < 0.008:
                score -= 3.0


            scores.append(
                score
            )


        if not scores:
            return 0.0


        scores.sort(
            reverse=True
        )

        # 최고 구간 중심.
        # 두 번째 구간도 좋으면 약간 가산.
        result = scores[0]

        if len(scores) >= 2:

            result += (
                scores[1] * 0.20
            )

        return round(
            result,
            3,
        )


    def inspect_image(item):

        index, img = item

        url = str(
            img.get("url")
            or ""
        )

        if not url:

            return (
                -999.0,
                index,
                img,
            )

        try:

            response = requests.get(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0",

                    "Referer":
                        "https://www.29cm.co.kr/",
                },
                timeout=12,
            )

            response.raise_for_status()

            if len(response.content) < 5000:

                return (
                    -999.0,
                    index,
                    img,
                )

            pil = Image.open(
                io.BytesIO(
                    response.content
                )
            )

            score = visual_table_score(
                pil
            )

            return (
                score,
                index,
                img,
            )

        except Exception:

            return (
                -999.0,
                index,
                img,
            )


    visual_results = []


    # 다운로드 대기시간을 줄이기 위해
    # 최대 6장 병렬 조사
    with ThreadPoolExecutor(
        max_workers=6
    ) as executor:

        futures = [
            executor.submit(
                inspect_image,
                (i, img),
            )

            for i, img
            in enumerate(
                original_images,
                start=1,
            )
        ]


        for future in as_completed(
            futures
        ):

            try:

                visual_results.append(
                    future.result()
                )

            except Exception:
                pass


    visual_results.sort(
        key=lambda x: x[0],
        reverse=True,
    )


    print(
        "🔍 OCR 없는 전체 이미지 선별",
        flush=True,
    )


    for (
        score,
        index,
        _img,
    ) in visual_results[:8]:

        print(
            f"   image={index}"
            f" visual_score={score:.2f}",
            flush=True,
        )


    # --------------------------------------------------------
    # 시각 점수 TOP 2
    # --------------------------------------------------------

    selected_indexes = []

    for (
        score,
        index,
        _img,
    ) in visual_results:

        if score < 0:
            continue

        if index not in selected_indexes:

            selected_indexes.append(
                index
            )

        if len(
            selected_indexes
        ) >= 2:

            break


    # --------------------------------------------------------
    # 실측표가 상세페이지 끝쪽에 있는 경우가 많으므로
    # 안전장치로 마지막 2장 추가
    # --------------------------------------------------------

    total_images = len(
        original_images
    )

    for index in (
        total_images - 1,
        total_images,
    ):

        if (
            index >= 1
            and
            index not in selected_indexes
        ):

            selected_indexes.append(
                index
            )


    images = []

    for index in selected_indexes:

        if (
            1
            <= index
            <= len(original_images)
        ):

            img = dict(
                original_images[
                    index - 1
                ]
            )

            img[
                "_original_index"
            ] = index

            images.append(
                img
            )


    print(
        f"⚡ 시각 선별 완료"
        f" | 전체={len(original_images)}"
        f" → 정밀 OCR={len(images)}"
        f" | indexes={selected_indexes}",
        flush=True,
    )

    print(
        f"⚡ OCR 대상 축소"
        f" | 전체={len(original_images)}"
        f" → 검사={len(images)}"
    )

    print()
    print("=" * 80)
    print(
        f"OCR START {pid}"
        f" | {job.get('brand')}"
        f" | images={len(images)}"
    )
    print("=" * 80)

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "Mozilla/5.0",

        "Referer":
            "https://www.29cm.co.kr/",
    })

    all_sizes = []
    image_results = []

    # ★ 모든 큐 이미지 전부 분석

    print(
        f"⚡ OCR 없는 시각선별 → 정밀 OCR 시작 "
        f"| images={len(images)}",
        flush=True,
    )

    image_results, all_sizes = (
        ocr_visual_selected(
            session,
            images,
        )
    )

    sizes = merge_sizes(
        all_sizes
    )

    category = (
        job.get(
            "main_category"
        )
        or "기타"
    )

    ok = False
    reason = "OCR 실측 없음"

    if sizes:

        try:
            ok, reason = (
                base.passes_quality(
                    category,
                    sizes,
                )
            )

        except Exception as e:
            reason = (
                "quality error: "
                f"{type(e).__name__}"
            )


    db_action = "none"

    if ok:

        conn = loader.build_conn()

        try:

            product_db_id, size_count = (
                existing_size_count(
                    conn,
                    pid,
                )
            )

            # DB에 상품 자체가 없음
            if product_db_id is None:

                raw = make_raw(
                    job,
                    sizes,
                )

                (
                    product_db_id,
                    _,
                    _,
                    _,
                ) = loader.upsert_one(
                    conn,
                    raw,
                )

                db_action = (
                    "insert_product"
                )

            # 상품은 이미 있음.
            # 기존 HTML 값을 덮어쓰지 않고
            # NULL / 없는 사이즈만 OCR로 보충.
            else:

                supplement_sizes(
                    conn,
                    product_db_id,
                    sizes,
                )

                db_action = (
                    "supplement_sizes"
                )

        finally:
            conn.close()


    result = {
        **job,

        "ocr_sizes":
            sizes,

        "quality_ok":
            ok,

        "quality_reason":
            reason,

        "db_action":
            db_action,

        "image_results":
            image_results,
    }


    save_json(
        RESULTS
        / f"{pid}.json",
        result,
    )


    destination = (
        DONE
        / path.name
    )

    save_json(
        destination,
        result,
    )

    path.unlink(
        missing_ok=True
    )


    print(
        f"✅ OCR END {pid}"
        f" | sizes={len(sizes)}"
        f" | quality={ok}"
        f" | db={db_action}"
    )


def main():

    print(
        "29CM OCR WORKER START"
    )

    while True:

        jobs = sorted(
            PENDING.glob(
                "*.json"
            ),
            key=lambda p:
                p.stat().st_mtime,
        )

        if not jobs:

            time.sleep(3)
            continue

        path = jobs[0]

        try:

            process_job(
                path
            )

        except Exception as e:

            print(
                f"❌ JOB FAIL "
                f"{path.name}"
                f" | {type(e).__name__}: {e}"
            )

            try:
                os.replace(
                    path,
                    FAILED / path.name,
                )

            except Exception:
                pass

            time.sleep(2)


if __name__ == "__main__":
    main()
