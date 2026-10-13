"""상품 이미지를 내려받아 로컬 파일로 준다. 모듈은 파일 경로만 받는다.

세 곳을 순서대로 본다.

  1. 우리 캐시   같은 상품을 다시 돌릴 때(재시도·재검증) 두 번 받지 않는다
  2. OCI 버킷    수집 파이프라인이 올려 둔 것. 우리 자산이라 제일 믿을 만하다
  3. source_url  버킷에 아직 없을 때만. 외부 CDN 이라 403·차단이 흔하다

캐시를 두는 이유가 속도만은 아니다. 규칙을 고치고 같은 작업을 다시 돌리는 일이
잦은데, 그때마다 남의 CDN 을 수천 번 때리는 것은 하면 안 되는 짓이다.
"""
import os
import shutil
import threading
import urllib.request

from core.config import DATA_DIR

CACHE_DIR = os.environ.get("OCR_IMAGE_CACHE", str(DATA_DIR / "ocr-cache"))
BUCKET = os.environ.get("OCR_BUCKET", "fashion-data")
HTTP_TIMEOUT = int(os.environ.get("OCR_HTTP_TIMEOUT", "20"))

# 한 상품에서 볼 이미지 수. 선별기가 사진은 OCR 없이 걸러내므로 넉넉해도 되지만,
# 상세 이미지가 수십 장인 상품이 있어 상한을 둔다.
MAX_IMAGES = int(os.environ.get("OCR_MAX_IMAGES", "12"))

_oci_lock = threading.Lock()
_oci = {"client": None, "namespace": None, "tried": False}


def _oci_client():
    """인스턴스 프린시펄로 붙는다. 서버 디스크에 자격증명 파일을 두지 않는다."""
    with _oci_lock:
        if _oci["tried"]:
            return _oci["client"], _oci["namespace"]
        _oci["tried"] = True
        try:
            import oci

            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            client = oci.object_storage.ObjectStorageClient({}, signer=signer)
            _oci["client"] = client
            _oci["namespace"] = client.get_namespace().data
        except Exception:
            # 버킷을 못 쓰면 source_url 로 떨어진다. 작업 전체를 멈출 일은 아니다.
            _oci["client"] = None
            _oci["namespace"] = None
        return _oci["client"], _oci["namespace"]


def _cache_path(product_id, order, key_or_url):
    ext = os.path.splitext((key_or_url or "").split("?")[0])[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        ext = ".jpg"
    return os.path.join(CACHE_DIR, str(product_id), f"{order:03d}{ext}")


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)          # 반쯤 받다 죽어도 깨진 파일이 남지 않는다


def _from_bucket(object_key):
    client, namespace = _oci_client()
    if not client or not object_key:
        return None
    try:
        resp = client.get_object(namespace, BUCKET, object_key)
        return resp.data.content
    except Exception:
        return None


def _from_url(url):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; wit-fashion-ocr/1.0)",
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()
    except Exception:
        return None


def fetch(product_id, rows_):
    """이미지 행 목록 → 로컬 경로 목록. 못 받은 것은 조용히 건너뛴다.

    한 장도 못 받으면 빈 목록이고, 부르는 쪽이 그걸 오류로 처리한다 —
    「이미지를 못 받은 것」과 「사이즈표가 없는 것」은 다르기 때문이다.
    """
    paths = []
    for r in rows_[:MAX_IMAGES]:
        order = r.get("image_order") or 0
        key = r.get("object_key")
        url = r.get("source_url")
        path = _cache_path(product_id, order, key or url)

        if os.path.exists(path) and os.path.getsize(path) > 0:
            paths.append(path)
            continue

        data = _from_bucket(key) or _from_url(url)
        if not data:
            continue
        try:
            _save(path, data)
        except OSError:
            continue
        paths.append(path)
    return paths


def cache_size_mb():
    total = 0
    for root, _dirs, files in os.walk(CACHE_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return round(total / 1024 / 1024, 1)


def clear_cache():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
