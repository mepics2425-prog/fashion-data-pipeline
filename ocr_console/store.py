"""DB 접근과 큐 조작. 웹과 워커가 같이 쓴다.

큐를 메모리가 아니라 DB 에 두는 이유 — 작업이 몇 시간짜리다. 워커가 죽거나
서버가 재부팅돼도 어디까지 했는지 남아 있어야 하고, 화면은 그걸 읽기만 하면 된다.
"""
import json
import os
import threading
import uuid
from datetime import datetime

import pymysql

from core.config import ENV_FILE

ENV_PATH = os.environ.get("FASHION_ENV", str(ENV_FILE))

# 워커가 집어 두고 죽은 항목을 되돌리는 기준. 한 건이 이보다 오래 걸릴 일은 없다
# (사진은 OCR 없이 걸러지고, 글자판도 확대까지 해야 장당 7초쯤이다).
STALE_MINUTES = int(os.environ.get("OCR_STALE_MINUTES", "20"))


def load_env(path=ENV_PATH):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip()
    return env


E = load_env()
_local = threading.local()


def db():
    """스레드당 연결 하나.

    🔴 대시보드(web/server.py)와 달리 여기는 **쓰기 계정**을 쓴다.
    대시보드는 `viewer_ro` 로 읽기만 하는 것이 설계 약속이라 그 보장을 깨지 않으려고
    콘솔을 별도 서비스로 뗐다. 두 화면이 같은 프로세스였다면 그 약속이 사라진다.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = pymysql.connect(
            host=E.get("MYSQL_HOST", "127.0.0.1"),
            port=int(E.get("MYSQL_PORT", "3306")),
            user=E.get("MYSQL_USER", "fashion_rw"),
            password=E.get("MYSQL_PASSWORD", ""),
            database=E.get("MYSQL_DB", "fashion"),
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        _local.conn = conn
    else:
        conn.ping(reconnect=True)
    return conn


def rows(sql, args=()):
    with db().cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with db().cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


# ── 대상 고르기 ─────────────────────────────────────────────────────
# 화면이 아니라 여기에 둔다 — 작업을 만들 때와 미리보기를 할 때 같은 SQL 을 써야
# 「미리보기 500건」과 「실제 500건」이 어긋나지 않는다.
TARGETS = {
    "no_sizes": (
        "사이즈가 없는 상품",
        """SELECT p.id FROM products p
            WHERE NOT EXISTS (SELECT 1 FROM product_sizes s WHERE s.product_id = p.id)
              AND EXISTS (SELECT 1 FROM product_images i WHERE i.product_id = p.id)
            ORDER BY p.id""",
    ),
    "all": (
        "이미지가 있는 상품 전체",
        """SELECT p.id FROM products p
            WHERE EXISTS (SELECT 1 FROM product_images i WHERE i.product_id = p.id)
            ORDER BY p.id""",
    ),
    # 며칠짜리 작업을 한 번에 다 돌릴 수는 없다. 끊어 돌릴 때 앞서 끝낸 것을
    # 다시 집으면 진도가 안 나가므로, 이미 결과가 있는 상품은 빼고 고른다.
    # 오류로 끝난 것은 제외하지 않는다 — 그건 다시 봐야 하는 건이다.
    "not_done": (
        "아직 OCR 하지 않은 상품",
        """SELECT p.id FROM products p
            WHERE EXISTS (SELECT 1 FROM product_images i WHERE i.product_id = p.id)
              AND NOT EXISTS (SELECT 1 FROM ocr_job_item d
                               WHERE d.product_id = p.id AND d.state = 'done')
            ORDER BY p.id""",
    ),
}


def target_ids(source, params):
    """작업 대상 product_id 목록. 순서는 항상 id 오름차순이라 재현된다."""
    if source == "ids":
        raw = params.get("ids") or []
        ids = []
        for v in raw:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if not ids:
            return []
        marks = ",".join(["%s"] * len(ids))
        found = rows(f"SELECT id FROM products WHERE id IN ({marks}) ORDER BY id", ids)
        return [r["id"] for r in found]

    if source not in TARGETS:
        raise ValueError(f"모르는 대상: {source}")
    sql = TARGETS[source][1]
    limit = params.get("limit")
    if limit:
        sql += " LIMIT %s"
        return [r["id"] for r in rows(sql, (int(limit),))]
    return [r["id"] for r in rows(sql)]


# ── 작업 ────────────────────────────────────────────────────────────
def create_job(name, source, params):
    ids = target_ids(source, params)
    if not ids:
        return None, 0

    with db().cursor() as cur:
        cur.execute(
            "INSERT INTO ocr_job (name, source, params, total) VALUES (%s,%s,%s,%s)",
            (name, source, json.dumps(params, ensure_ascii=False), len(ids)),
        )
        job_id = cur.lastrowid
        # 한 번에 다 넣지 않고 끊어 넣는다 — 수천 건이면 한 문장이 너무 길어진다.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur.executemany(
                "INSERT INTO ocr_job_item (job_id, product_id) VALUES (%s,%s)",
                [(job_id, pid) for pid in chunk],
            )
    return job_id, len(ids)


def job(job_id):
    return one("SELECT * FROM ocr_job WHERE id=%s", (job_id,))


def job_counts(job_id):
    """진행 집계. 화면이 2초마다 부르므로 한 방에 끝낸다."""
    r = one(
        """SELECT
             COUNT(*)                                             AS total,
             SUM(state='queued')                                  AS queued,
             SUM(state='running')                                 AS running,
             SUM(state='done')                                    AS done,
             SUM(state='error')                                   AS error,
             SUM(status='SUCCESS')                                AS success,
             SUM(status='REVIEW')                                 AS review,
             SUM(status='NO_SIZE_TABLE')                          AS no_table,
             AVG(elapsed_ms)                                      AS avg_ms
           FROM ocr_job_item WHERE job_id=%s""",
        (job_id,),
    ) or {}
    out = {k: int(v or 0) for k, v in r.items() if k != "avg_ms"}
    out["avg_ms"] = int(r.get("avg_ms") or 0)
    left = out.get("queued", 0) + out.get("running", 0)
    # 남은 시간은 실측 평균으로만 낸다. 처음 몇 건이 끝나기 전에는 내지 않는다.
    out["eta_sec"] = int(left * out["avg_ms"] / 1000) if out["avg_ms"] and left else None
    return out


def set_job_status(job_id, status):
    if status == "done":
        return run("UPDATE ocr_job SET status=%s, finished_at=NOW() WHERE id=%s",
                   (status, job_id))
    return run("UPDATE ocr_job SET status=%s, finished_at=NULL WHERE id=%s",
               (status, job_id))


def finish_if_drained(job_id):
    """남은 항목이 없으면 작업을 끝난 것으로 표시한다."""
    left = one("SELECT COUNT(*) AS n FROM ocr_job_item "
               "WHERE job_id=%s AND state IN ('queued','running')", (job_id,))
    if left and left["n"] == 0:
        run("UPDATE ocr_job SET status='done', finished_at=NOW() "
            "WHERE id=%s AND status='running'", (job_id,))
        return True
    return False


def retry_failed(job_id):
    """실패·오류만 다시 큐에 넣는다. 성공한 것은 건드리지 않는다."""
    return run(
        """UPDATE ocr_job_item
              SET state='queued', status=NULL, reason=NULL, result_json=NULL,
                  error=NULL, claim_token=NULL, claimed_at=NULL, finished_at=NULL
            WHERE job_id=%s AND state='error'""",
        (job_id,),
    )


# ── 큐 ──────────────────────────────────────────────────────────────
def reclaim_stale():
    """집어 두고 끝내지 못한 항목을 큐로 되돌린다.

    워커가 죽거나 서버가 재부팅되면 `running` 인 채로 남는다. 그대로 두면
    그 건은 영원히 처리되지 않고 작업도 끝나지 않는다.
    """
    return run(
        """UPDATE ocr_job_item
              SET state='queued', claim_token=NULL, claimed_at=NULL
            WHERE state='running'
              AND claimed_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)""",
        (STALE_MINUTES,),
    )


def reclaim_worker(worker):
    """이 이름으로 집혀 있는 건을 전부 큐로 되돌린다.

    워커가 시작할 때 부른다. 방금 뜬 참이라 이 이름으로 집고 있는 건이 있을
    수 없다 — 있다면 앞선 나 자신이 죽으면서 남긴 것이다. 시간(stale) 을
    기다릴 이유가 없다. 안 그러면 죽을 때마다 그 한 건이 20분씩 묶인다.
    """
    return run(
        """UPDATE ocr_job_item
              SET state='queued', claim_token=NULL, claimed_at=NULL, worker=NULL
            WHERE state='running' AND worker=%s""",
        (worker,),
    )


def claim_next(worker, attempts=5):
    """돌고 있는 작업에서 항목 하나를 집는다. 없으면 None.

    고를 때와 집을 때를 나눈다 — MySQL 은 JOIN 이 붙은 UPDATE 에
    `ORDER BY … LIMIT` 를 허용하지 않는다(1221). 그래서 먼저 id 를 고르고,
    `state='queued'` 를 조건에 달아 그 id 만 집는다.

    워커를 여러 개 띄워도 겹치지 않는다. 같은 건을 동시에 노리면 한쪽의
    UPDATE 가 0행이 되고, 그쪽은 다음 후보로 넘어간다.
    """
    for _ in range(attempts):
        row = one(
            """SELECT i.id FROM ocr_job_item i
                 JOIN ocr_job j ON j.id = i.job_id
                WHERE i.state='queued' AND j.status='running'
                ORDER BY i.job_id, i.id
                LIMIT 1"""
        )
        if not row:
            return None
        token = uuid.uuid4().hex
        got = run(
            """UPDATE ocr_job_item
                  SET state='running', claim_token=%s, claimed_at=NOW(), worker=%s
                WHERE id=%s AND state='queued'""",
            (token, worker, row["id"]),
        )
        if got:
            return one("SELECT * FROM ocr_job_item WHERE id=%s", (row["id"],))
    return None                      # 경쟁이 심하면 다음 바퀴에 다시 본다


def finish_item(item_id, result=None, error=None, image_count=0, elapsed_ms=0):
    if error:
        return run(
            """UPDATE ocr_job_item
                  SET state='error', error=%s, image_count=%s, elapsed_ms=%s,
                      finished_at=NOW(), claim_token=NULL
                WHERE id=%s""",
            (error[:500], image_count, elapsed_ms, item_id),
        )
    return run(
        """UPDATE ocr_job_item
              SET state='done', status=%s, reason=%s, result_json=%s,
                  image_count=%s, elapsed_ms=%s, finished_at=NOW(), claim_token=NULL
            WHERE id=%s""",
        (
            result.get("status"),
            (result.get("reason") or None) and result["reason"][:500],
            json.dumps(result, ensure_ascii=False),
            image_count,
            elapsed_ms,
            item_id,
        ),
    )


# ── 조회 ────────────────────────────────────────────────────────────
def job_list(limit=50):
    return rows(
        """SELECT j.*,
                  (SELECT COUNT(*) FROM ocr_job_item i
                    WHERE i.job_id=j.id AND i.state IN ('done','error')) AS processed
             FROM ocr_job j ORDER BY j.id DESC LIMIT %s""",
        (limit,),
    )


def job_items(job_id, status=None, state=None, limit=100, offset=0):
    where = ["i.job_id=%s"]
    args = [job_id]
    if status:
        where.append("i.status=%s")
        args.append(status)
    if state:
        where.append("i.state=%s")
        args.append(state)
    args += [int(limit), int(offset)]
    return rows(
        f"""SELECT i.id, i.product_id, i.state, i.status, i.reason, i.error,
                   i.image_count, i.elapsed_ms, i.finished_at,
                   p.brand, p.product_name, p.main_category, p.product_url
              FROM ocr_job_item i
              LEFT JOIN products p ON p.id = i.product_id
             WHERE {' AND '.join(where)}
             ORDER BY i.id LIMIT %s OFFSET %s""",
        args,
    )


def item_detail(item_id):
    r = one(
        """SELECT i.*, p.brand, p.product_name, p.product_url, p.main_category
             FROM ocr_job_item i
             LEFT JOIN products p ON p.id = i.product_id
            WHERE i.id=%s""",
        (item_id,),
    )
    if r and isinstance(r.get("result_json"), str):
        try:
            r["result_json"] = json.loads(r["result_json"])
        except ValueError:
            pass
    return r


def product_images(product_id, limit=20):
    return rows(
        """SELECT id, image_order, object_key, source_url
             FROM product_images WHERE product_id=%s
            ORDER BY image_order LIMIT %s""",
        (product_id, limit),
    )


def jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat(sep=" ", timespec="seconds")
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    raise TypeError(f"직렬화할 수 없는 타입: {type(o)}")
