#!/usr/bin/env python3
"""
Fashion size-first JSON -> MySQL loader.

Examples:
  /opt/fashion/venv/bin/python loader.py --input /data/raw --limit 5 --dry-run
  /opt/fashion/venv/bin/python loader.py --input /data/raw --limit 5
  /opt/fashion/venv/bin/python loader.py --input /data/raw
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pymysql

from core.config import ENV_FILE, RAW_DIR

ENV_PATH = str(ENV_FILE)
DEFAULT_INPUT = str(RAW_DIR)

SIZE_COLUMNS = [
    "total_length_cm", "shoulder_cm", "chest_flat_cm",
    "chest_circumference_cm", "sleeve_length_cm", "armhole_cm",
    "sleeve_width_cm", "waist_flat_cm", "waist_circumference_cm",
    "hip_flat_cm", "hip_circumference_cm", "thigh_flat_cm",
    "rise_cm", "inseam_cm", "hem_flat_cm",
]

# Known non-colour tokens from the current collector. Better NULL than false data.
BAD_COLOR_PATTERNS = [r"리뉴얼", r"\bver\.?\b", r"신상", r"단독", r"예약", r"할인", r"특가"]


# Legacy records created before classification fields were added can still be loaded.
# We only infer a coarse category from the collector search keyword using deterministic rules.
# The original keyword is preserved in products.search_keyword and the raw JSON is not modified.
LEGACY_CATEGORY_RULES = [
    ("원피스", "원피스", "원피스"),
    ("스커트", "하의", "스커트"),
    ("가디건", "상의", "가디건"),
    ("맨투맨", "상의", "맨투맨"),
    ("후드", "상의", "후드"),
    ("긴팔티셔츠", "상의", "긴팔티셔츠"),
    ("긴팔 티셔츠", "상의", "긴팔티셔츠"),
    ("블레이저", "아우터", "블레이저"),
    ("바람막이", "아우터", "바람막이"),
    ("패딩", "아우터", "패딩"),
    ("코트", "아우터", "코트"),
    ("재킷", "아우터", "재킷"),
    ("자켓", "아우터", "재킷"),
    ("점퍼", "아우터", "점퍼"),
    ("집업", "아우터", "집업"),
    ("베스트", "아우터", "베스트"),
    ("반바지", "하의", "반바지"),
    ("데님", "하의", "데님"),
    ("와이드팬츠", "하의", "와이드팬츠"),
    ("와이드 팬츠", "하의", "와이드팬츠"),
    ("팬츠", "하의", "팬츠"),
    ("바지", "하의", "팬츠"),
    ("블라우스", "상의", "블라우스"),
    ("반팔티셔츠", "상의", "반팔티셔츠"),
    ("반팔 티셔츠", "상의", "반팔티셔츠"),
    ("티셔츠", "상의", "티셔츠"),
    ("셔츠", "상의", "셔츠"),
    ("니트", "상의", "니트"),
]


def infer_legacy_classification(raw):
    collection = raw.get("collection") or {}
    product = raw.get("product") or {}
    keyword = clean_text(collection.get("search_keyword")) or ""
    product_name = clean_text(product.get("product_name")) or ""

    # Search keyword is the primary fallback because it is the collector's discovery context.
    # Product name is only a secondary aid for older records whose keyword is incomplete.
    text = f"{keyword} {product_name}".lower()
    compact = re.sub(r"\s+", "", text)

    gender = None
    if "여성" in text or "여자" in text or "우먼" in text:
        gender = "여성"
    elif "남성" in text or "남자" in text or "맨즈" in text:
        gender = "남성"

    for token, main, sub in LEGACY_CATEGORY_RULES:
        token_norm = token.lower()
        if token_norm in text or re.sub(r"\s+", "", token_norm) in compact:
            return gender, main, sub, keyword or None

    return gender, None, None, keyword or None


def load_env(path=ENV_PATH):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def parse_dt(value):
    if not value:
        return datetime.now()
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return datetime.now()


def cm(value):
    if value in (None, "", 0, 0.0, "0", "0.0"):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value == 0 else value


def clean_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def clean_color(value):
    value = clean_text(value)
    if not value:
        return None
    for pattern in BAD_COLOR_PATTERNS:
        if re.search(pattern, value, re.I):
            return None
    return value


def is_product_json(data):
    return (
        isinstance(data, dict)
        and isinstance(data.get("product"), dict)
        and isinstance(data.get("size_data"), dict)
        and isinstance(data.get("source"), dict)
        and bool(data["product"].get("product_id"))
    )


def iter_product_files(root):
    root = Path(root)
    candidates = [root] if root.is_file() else sorted(root.rglob("*.json"))
    for path in candidates:
        if path.name.startswith("_") or path.name.endswith(".tmp.json"):
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if is_product_json(data):
            yield path, data


def normalize_product(raw):
    collection = raw.get("collection") or {}
    classification = raw.get("classification") or {}
    product = raw.get("product") or {}
    attrs = raw.get("garment_attributes") or {}
    source = raw.get("source") or {}

    gender = clean_text(classification.get("gender"))
    main_category = clean_text(classification.get("main_category"))
    sub_category = clean_text(classification.get("sub_category"))
    category_source = "classification" if main_category else None

    # Older collected JSON files may predate the classification block.
    # Recover only coarse categories from search_keyword/product_name; never invent measurements.
    if not main_category:
        legacy_gender, legacy_main, legacy_sub, _ = infer_legacy_classification(raw)
        gender = gender or legacy_gender
        main_category = legacy_main
        sub_category = sub_category or legacy_sub
        if legacy_main:
            category_source = "legacy_keyword_fallback"

    p = {
        "platform": clean_text(source.get("platform")) or "musinsa",
        "platform_product_id": clean_text(product.get("product_id")),
        "brand": clean_text(product.get("brand")),
        "product_name": clean_text(product.get("product_name")),
        "gender": gender,
        "main_category": main_category,
        "sub_category": sub_category,
        "price": product.get("price"),
        "color": clean_color(product.get("color")),
        "material": clean_text(attrs.get("material")),
        "fit": clean_text(attrs.get("fit")),
        "stretch": clean_text(attrs.get("stretch")),
        "thickness": clean_text(attrs.get("thickness")),
        "product_url": clean_text(product.get("product_url")),
        "search_keyword": clean_text(collection.get("search_keyword")),
        "collected_at": parse_dt(collection.get("collected_at")),
        "_category_source": category_source,
    }
    try:
        p["price"] = int(p["price"]) if p["price"] is not None else None
    except (TypeError, ValueError):
        p["price"] = None

    required = ["platform_product_id", "brand", "product_name", "main_category", "product_url"]
    missing = [k for k in required if not p[k]]
    if missing:
        if "main_category" in missing:
            kw = clean_text(collection.get("search_keyword"))
            raise ValueError(f"필수값 누락: {', '.join(missing)} (search_keyword={kw!r})")
        raise ValueError("필수값 누락: " + ", ".join(missing))
    return p


def normalize_sizes(raw):
    size_data = raw.get("size_data") or {}
    measurements = size_data.get("measurements") or {}
    labels = size_data.get("size_labels") or list(measurements.keys())
    measurement_type = clean_text(size_data.get("measurement_type")) or "unknown"

    out = []
    for label in labels:
        label = clean_text(label)
        if not label:
            continue
        m = measurements.get(label) or {}
        row = {"size_label": label, "measurement_type": measurement_type}
        for col in SIZE_COLUMNS:
            row[col] = cm(m.get(col))
        extra = {}
        for k, v in m.items():
            if k not in SIZE_COLUMNS:
                if isinstance(v, (int, float)):
                    extra[k] = cm(v)
                else:
                    extra[k] = v
        row["extra"] = extra or None
        out.append(row)
    return out


def normalize_images(raw):
    images = (raw.get("media") or {}).get("images") or []
    out, used = [], set()
    for pos, img in enumerate(images, start=1):
        if not isinstance(img, dict):
            continue
        url = clean_text(img.get("source_url"))
        if not url:
            continue
        try:
            order = int(img.get("index") or img.get("order") or pos)
        except (TypeError, ValueError):
            order = pos
        while order in used:
            order += 1
        used.add(order)
        out.append({
            "image_order": order,
            "source_url": url,
            "image_type": clean_text(img.get("image_type")),
        })
    return out


def normalize_models(raw):
    models = raw.get("model_info") or []
    if isinstance(models, dict):
        models = [models]
    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        height = cm(m.get("height_cm") if "height_cm" in m else m.get("height"))
        wearing = clean_text(m.get("wearing_size") or m.get("wear_size") or m.get("size"))
        if height is None and wearing is None:
            continue
        out.append({"height_cm": height, "wearing_size": wearing})
    return out


def preview(path, raw):
    p = normalize_product(raw)
    sizes = normalize_sizes(raw)
    images = normalize_images(raw)
    models = normalize_models(raw)
    return p, sizes, images, models


def upsert_one(conn, raw):
    p = normalize_product(raw)
    sizes = normalize_sizes(raw)
    images = normalize_images(raw)
    models = normalize_models(raw)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO products
              (platform, platform_product_id, brand, product_name,
               gender, main_category, sub_category, price,
               color, material, fit, stretch, thickness,
               product_url, search_keyword, raw, collected_at, updated_at)
            VALUES
              (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
            ON DUPLICATE KEY UPDATE
              id             = LAST_INSERT_ID(id),
              brand          = VALUES(brand),
              product_name   = VALUES(product_name),
              gender         = VALUES(gender),
              main_category  = VALUES(main_category),
              sub_category   = VALUES(sub_category),
              price          = VALUES(price),
              color          = VALUES(color),
              material       = VALUES(material),
              fit            = VALUES(fit),
              stretch        = VALUES(stretch),
              thickness      = VALUES(thickness),
              product_url    = VALUES(product_url),
              search_keyword = VALUES(search_keyword),
              raw            = VALUES(raw),
              collected_at   = VALUES(collected_at),
              updated_at     = NOW()
        """, (
            p["platform"], p["platform_product_id"], p["brand"], p["product_name"],
            p["gender"], p["main_category"], p["sub_category"], p["price"],
            p["color"], p["material"], p["fit"], p["stretch"], p["thickness"],
            p["product_url"], p["search_keyword"], json.dumps(raw, ensure_ascii=False),
            p["collected_at"],
        ))
        product_id = cur.lastrowid

        if sizes:
            rows = []
            for s in sizes:
                rows.append((
                    product_id, s["size_label"], s["measurement_type"],
                    s["total_length_cm"], s["shoulder_cm"], s["chest_flat_cm"],
                    s["chest_circumference_cm"], s["sleeve_length_cm"], s["armhole_cm"],
                    s["sleeve_width_cm"], s["waist_flat_cm"], s["waist_circumference_cm"],
                    s["hip_flat_cm"], s["hip_circumference_cm"], s["thigh_flat_cm"],
                    s["rise_cm"], s["inseam_cm"], s["hem_flat_cm"],
                    json.dumps(s["extra"], ensure_ascii=False) if s["extra"] else None,
                ))
            cur.executemany("""
                INSERT INTO product_sizes
                  (product_id, size_label, measurement_type,
                   total_length_cm, shoulder_cm, chest_flat_cm, chest_circumference_cm,
                   sleeve_length_cm, armhole_cm, sleeve_width_cm,
                   waist_flat_cm, waist_circumference_cm,
                   hip_flat_cm, hip_circumference_cm,
                   thigh_flat_cm, rise_cm, inseam_cm, hem_flat_cm, extra)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  total_length_cm        = VALUES(total_length_cm),
                  shoulder_cm            = VALUES(shoulder_cm),
                  chest_flat_cm          = VALUES(chest_flat_cm),
                  chest_circumference_cm = VALUES(chest_circumference_cm),
                  sleeve_length_cm       = VALUES(sleeve_length_cm),
                  armhole_cm             = VALUES(armhole_cm),
                  sleeve_width_cm        = VALUES(sleeve_width_cm),
                  waist_flat_cm          = VALUES(waist_flat_cm),
                  waist_circumference_cm = VALUES(waist_circumference_cm),
                  hip_flat_cm            = VALUES(hip_flat_cm),
                  hip_circumference_cm   = VALUES(hip_circumference_cm),
                  thigh_flat_cm          = VALUES(thigh_flat_cm),
                  rise_cm                = VALUES(rise_cm),
                  inseam_cm              = VALUES(inseam_cm),
                  hem_flat_cm            = VALUES(hem_flat_cm),
                  extra                  = VALUES(extra)
            """, rows)

        if images:
            cur.executemany("""
                INSERT INTO product_images (product_id, image_order, source_url, image_type)
                VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  source_url = VALUES(source_url),
                  image_type = VALUES(image_type)
            """, [(product_id, x["image_order"], x["source_url"], x["image_type"]) for x in images])

        # Keep child rows synchronized with the current raw JSON.
        size_keys = {(x["size_label"], x["measurement_type"]) for x in sizes}
        cur.execute("SELECT id, size_label, measurement_type FROM product_sizes WHERE product_id=%s", (product_id,))
        for row_id, label, mtype in cur.fetchall():
            if (label, mtype) not in size_keys:
                cur.execute("DELETE FROM product_sizes WHERE id=%s", (row_id,))

        image_orders = {x["image_order"] for x in images}
        cur.execute("SELECT id, image_order FROM product_images WHERE product_id=%s", (product_id,))
        for row_id, order in cur.fetchall():
            if order not in image_orders:
                cur.execute("DELETE FROM product_images WHERE id=%s", (row_id,))

        # product_models has no UNIQUE key in the current schema.
        cur.execute("DELETE FROM product_models WHERE product_id=%s", (product_id,))
        if models:
            cur.executemany(
                "INSERT INTO product_models (product_id, height_cm, wearing_size) VALUES (%s,%s,%s)",
                [(product_id, x["height_cm"], x["wearing_size"]) for x in models],
            )

    conn.commit()
    return product_id, len(sizes), len(images), len(models)


def build_conn():
    env = load_env()
    required = ["MYSQL_HOST", "MYSQL_PORT", "MYSQL_DB", "MYSQL_USER", "MYSQL_PASSWORD"]
    missing = [x for x in required if not env.get(x)]
    if missing:
        raise RuntimeError("환경변수 누락: " + ", ".join(missing))
    return pymysql.connect(
        host=env["MYSQL_HOST"], port=int(env["MYSQL_PORT"]),
        user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"], database=env["MYSQL_DB"],
        charset="utf8mb4", autocommit=False, local_infile=True,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_INPUT, help="원본 상품 JSON 파일 또는 디렉터리")
    ap.add_argument("--limit", type=int, default=None, help="최대 상품 수")
    ap.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 파싱만 확인")
    args = ap.parse_args()

    files = []
    for path, raw in iter_product_files(args.input):
        files.append((path, raw))
        if args.limit and len(files) >= args.limit:
            break

    if not files:
        print(f"상품 JSON을 찾지 못했습니다: {args.input}")
        return 2

    print(f"입력 상품: {len(files)}건")
    print(f"모드: {'DRY-RUN' if args.dry_run else 'DB WRITE'}")

    if args.dry_run:
        for idx, (path, raw) in enumerate(files, 1):
            try:
                p, sizes, images, models = preview(path, raw)
                print(
                    f"[{idx}/{len(files)}] OK {p['platform']}:{p['platform_product_id']} "
                    f"{p['brand']} | {p['product_name'][:60]} | "
                    f"sizes={len(sizes)} images={len(images)} models={len(models)} "
                    f"category={p['main_category']}/{p['sub_category']} source={p.get('_category_source')} color={p['color']!r}"
                )
            except Exception as e:
                print(f"[{idx}/{len(files)}] ERROR {path}: {e}")
        return 0

    conn = build_conn()
    ok = failed = 0
    try:
        for idx, (path, raw) in enumerate(files, 1):
            try:
                product_id, ns, ni, nm = upsert_one(conn, raw)
                ok += 1
                source_pid = raw.get("product", {}).get("product_id")
                print(f"[{idx}/{len(files)}] OK source={source_pid} db_id={product_id} sizes={ns} images={ni} models={nm}")
            except Exception as e:
                conn.rollback()
                failed += 1
                print(f"[{idx}/{len(files)}] ERROR {path}: {e}", file=sys.stderr)
    finally:
        conn.close()

    print(f"\n완료: 성공={ok}, 실패={failed}, 총={len(files)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
