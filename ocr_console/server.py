"""사이즈표 OCR 콘솔 — 작업을 만들고, 진행을 보고, 결과를 읽는다.

대시보드(web/server.py)와 **다른 서비스**다. 그쪽은 `viewer_ro` 로 읽기만 하는 것이
설계 약속이라, 쓰기를 하는 이 화면을 같은 프로세스에 넣으면 그 보장이 사라진다.
nginx 가 같은 도메인의 `/ocr/` 로 붙여 주고 Basic Auth·HTTPS 는 그대로 공유한다.

    OCR_CONSOLE_PORT=8081 python -m ocr_console.server
"""
import io
import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import images, store

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
BIND = os.environ.get("OCR_CONSOLE_BIND", "127.0.0.1")
PORT = int(os.environ.get("OCR_CONSOLE_PORT", "8081"))

# 즉석 시험으로 올릴 수 있는 크기. 상세 이미지는 보통 1MB 안쪽이다.
MAX_UPLOAD = int(os.environ.get("OCR_MAX_UPLOAD", str(12 * 1024 * 1024)))
TRY_DIR = os.environ.get("OCR_TRY_DIR", "/tmp/ocr-try")


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=store.jsonable).encode("utf-8")


def _int(q, key, default):
    try:
        return int(q.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def _str(q, key, default=None):
    v = q.get(key, [default])[0]
    return v if v not in ("", None) else default


# ── 읽기 ────────────────────────────────────────────────────────────
def api_jobs(_q):
    return {"jobs": store.job_list(), "cache_mb": images.cache_size_mb()}


def api_job(q):
    jid = _int(q, "id", 0)
    j = store.job(jid)
    if not j:
        return {"error": "없는 작업입니다"}, 404
    return {"job": j, "counts": store.job_counts(jid)}


def api_items(q):
    jid = _int(q, "id", 0)
    return {"items": store.job_items(
        jid,
        status=_str(q, "status"),
        state=_str(q, "state"),
        limit=min(_int(q, "limit", 50), 200),
        offset=_int(q, "offset", 0),
    )}


def api_item(q):
    it = store.item_detail(_int(q, "id", 0))
    if not it:
        return {"error": "없는 항목입니다"}, 404
    it["images"] = store.product_images(it["product_id"])
    return {"item": it}


def api_targets(q):
    """작업을 만들기 전에 대상이 몇 건인지 보여 준다.

    실제 생성과 **같은 SQL** 을 쓴다. 미리보기와 결과가 어긋나면
    「500건이라더니 왜 300건이냐」가 되고, 그 신뢰는 회복이 어렵다.
    """
    src = _str(q, "source", "no_sizes")
    limit = _int(q, "limit", 0)
    params = {"limit": limit} if limit else {}
    try:
        ids = store.target_ids(src, params)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return {"source": src, "count": len(ids), "sample": ids[:10]}


# ── 쓰기 ────────────────────────────────────────────────────────────
def api_create(body):
    src = body.get("source") or "no_sizes"
    name = (body.get("name") or "").strip()
    params = {}
    if src == "ids":
        raw = body.get("ids") or ""
        params["ids"] = [t for t in re.split(r"[^0-9]+", str(raw)) if t]
    else:
        limit = body.get("limit")
        if limit:
            params["limit"] = int(limit)

    if not name:
        label = store.TARGETS.get(src, ("직접 지정",))[0]
        name = f"{label} · {time.strftime('%m/%d %H:%M')}"

    job_id, total = store.create_job(name, src, params)
    if not job_id:
        return {"error": "대상이 0건입니다"}, 400
    return {"job_id": job_id, "total": total}


def api_control(body):
    jid = int(body.get("id") or 0)
    action = body.get("action")
    j = store.job(jid)
    if not j:
        return {"error": "없는 작업입니다"}, 404

    if action == "pause":
        store.set_job_status(jid, "paused")
    elif action == "resume":
        store.set_job_status(jid, "running")
    elif action == "cancel":
        store.set_job_status(jid, "canceled")
    elif action == "retry":
        n = store.retry_failed(jid)
        store.set_job_status(jid, "running")
        return {"ok": True, "requeued": n}
    else:
        return {"error": "모르는 동작입니다"}, 400
    return {"ok": True, "status": action}


def api_try(body):
    """이미지 한 장을 그 자리에서 돌려 본다. 큐를 거치지 않는다.

    규칙을 고친 뒤 「이 이미지가 지금은 어떻게 나오나」를 보는 자리다.
    작업과 섞이면 안 되므로 DB 에 아무것도 남기지 않는다.
    """
    from fashion_ocr import extract_size_from_images

    data = body.get("_file")
    if not data:
        return {"error": "이미지가 없습니다"}, 400

    os.makedirs(TRY_DIR, exist_ok=True)
    path = os.path.join(TRY_DIR, f"{uuid.uuid4().hex}{body.get('_ext') or '.jpg'}")
    with open(path, "wb") as f:
        f.write(data)
    try:
        began = time.time()
        result = extract_size_from_images("TRY", None, [path])
        result["_elapsed_ms"] = int((time.time() - began) * 1000)
        return {"result": result}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


GET_ROUTES = {
    "/api/jobs": api_jobs,
    "/api/job": api_job,
    "/api/items": api_items,
    "/api/item": api_item,
    "/api/targets": api_targets,
}
POST_ROUTES = {
    "/api/create": api_create,
    "/api/control": api_control,
    "/api/try": api_try,
}

_page = {"mtime": None, "body": None}


def index_html():
    path = os.path.join(STATIC, "index.html")
    mtime = os.path.getmtime(path)
    if _page["mtime"] != mtime:
        with open(path, "rb") as f:
            _page["body"] = f.read()
        _page["mtime"] = mtime
    return _page["body"]


def parse_multipart(body, ctype):
    """업로드 한 건만 꺼낸다. 프레임워크를 들이지 않으려고 직접 읽는다."""
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", ctype)
    if not m:
        return None, None
    boundary = (m.group(1) or m.group(2)).strip().encode()
    for part in body.split(b"--" + boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or b"filename=" not in head:
            continue
        name = re.search(rb'filename="([^"]*)"', head)
        ext = os.path.splitext((name.group(1) if name else b"").decode("utf-8", "replace"))[1]
        return data.rstrip(b"\r\n--"), (ext.lower() or ".jpg")
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "FashionOcrConsole"
    sys_version = ""

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, result):
        code = 200
        if isinstance(result, tuple):
            result, code = result
        self._send(code, dumps(result), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = re.sub(r"^/ocr", "", u.path)          # nginx 가 /ocr/ 로 붙여 준다
        path = re.sub(r"/+$", "", path) or "/"

        if path in ("/", "/index.html"):
            return self._send(200, index_html(), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, b'{"ok":true}', "application/json")

        fn = GET_ROUTES.get(path)
        if not fn:
            return self._json(({"error": "not found"}, 404))
        try:
            self._json(fn(parse_qs(u.query)))
        except Exception as exc:                      # noqa: BLE001
            self.log_error("%s %s → %r", self.command, self.path, exc)
            self._json(({"error": "조회 중 오류가 발생했습니다"}, 500))

    def do_POST(self):
        u = urlparse(self.path)
        path = re.sub(r"^/ocr", "", u.path)
        path = re.sub(r"/+$", "", path) or "/"

        fn = POST_ROUTES.get(path)
        if not fn:
            return self._json(({"error": "not found"}, 404))

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json(({"error": f"파일이 너무 큽니다 (상한 {MAX_UPLOAD // 1024 // 1024}MB)"}, 413))
        raw = self.rfile.read(length) if length else b""

        ctype = self.headers.get("Content-Type", "")
        try:
            if ctype.startswith("multipart/form-data"):
                data, ext = parse_multipart(raw, ctype)
                body = {"_file": data, "_ext": ext}
            else:
                body = json.loads(raw or b"{}")
            self._json(fn(body))
        except Exception as exc:                      # noqa: BLE001
            self.log_error("%s %s → %r", self.command, self.path, exc)
            self._json(({"error": "처리 중 오류가 발생했습니다"}, 500))

    def log_message(self, fmt, *args):
        pass                                           # 접근 로그는 nginx 가 남긴다


def main():
    if not os.path.isdir(STATIC):
        print(f"static 폴더가 없습니다: {STATIC}", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"OCR 콘솔 → http://{BIND}:{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
