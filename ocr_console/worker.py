"""큐에서 한 건씩 집어 OCR 을 돌린다. 웹과 다른 프로세스다.

프로세스를 나눈 이유 두 가지.

  · OCR 은 CPU 를 오래 문다(장당 2~7초). 웹 프로세스 안에서 돌리면 그동안
    화면이 멈춘다. 진행률을 보려고 여는 화면이 진행 때문에 멈추면 쓸모가 없다.
  · 웹을 재시작해도 작업은 계속돼야 한다. 반대도 마찬가지다.

    systemctl start fashion-ocr-worker
    OCR_WORKERS=1 python -m ocr_console.worker      # 손으로 돌릴 때
"""
import os
import signal
import sys
import time
import traceback

from . import images, store

POLL_SEC = float(os.environ.get("OCR_POLL_SEC", "3"))
RECLAIM_EVERY = int(os.environ.get("OCR_RECLAIM_EVERY", "60"))   # 초

_stop = False


def _bye(signum, _frame):
    """지금 건은 끝내고 나간다. 중간에 끊으면 그 건이 stale 로 남는다."""
    global _stop
    _stop = True
    print(f"[worker] 신호 {signum} — 이번 건을 마치고 멈춥니다", flush=True)


def process(item):
    """항목 하나. 예외는 여기서 잡아 항목의 오류로 남긴다 — 워커는 죽지 않는다."""
    from fashion_ocr import extract_size_from_images

    pid = item["product_id"]
    began = time.time()
    try:
        rows_ = store.product_images(pid)
        if not rows_:
            store.finish_item(item["id"], error="이미지 행이 없습니다",
                              elapsed_ms=int((time.time() - began) * 1000))
            return "error"

        paths = images.fetch(pid, rows_)
        if not paths:
            # 🔴 이미지를 못 받은 것을 NO_SIZE_TABLE 로 적으면 안 된다.
            #    「사이즈표가 없는 상품」으로 분류되어 다시 볼 기회가 사라진다.
            store.finish_item(item["id"], error=f"이미지 {len(rows_)}건을 모두 내려받지 못했습니다",
                              image_count=0,
                              elapsed_ms=int((time.time() - began) * 1000))
            return "error"

        result = extract_size_from_images(product_id=str(pid), category=None, images=paths)
        store.finish_item(item["id"], result=result, image_count=len(paths),
                          elapsed_ms=int((time.time() - began) * 1000))
        return result.get("status")
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        store.finish_item(item["id"], error=f"{type(exc).__name__}: {exc}",
                          elapsed_ms=int((time.time() - began) * 1000))
        return "error"


def main():
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    name = os.environ.get("OCR_WORKER_NAME", f"worker-{os.getpid()}")
    print(f"[worker] {name} 시작 — 큐를 {POLL_SEC}초마다 봅니다", flush=True)

    # 시작하자마자 두 번 되돌린다.
    #   내 이름 것   앞선 나 자신이 죽으며 남긴 건. 시간을 기다릴 이유가 없다
    #   오래된 것     다른 이름(옛 설정)으로 남은 건
    mine = store.reclaim_worker(name)
    back = store.reclaim_stale()
    if mine or back:
        print(f"[worker] 멈춰 있던 {mine + back}건을 큐로 되돌렸습니다", flush=True)
    last_reclaim = time.time()
    idle_since = None

    while not _stop:
        if time.time() - last_reclaim > RECLAIM_EVERY:
            store.reclaim_stale()
            last_reclaim = time.time()

        try:
            item = store.claim_next(name)
        except Exception as exc:                               # noqa: BLE001
            # DB 가 잠깐 끊긴 것뿐일 수 있다. 죽지 말고 다음 바퀴에 다시 본다.
            print(f"[worker] 큐 조회 실패: {exc}", flush=True)
            time.sleep(POLL_SEC * 2)
            continue

        if not item:
            if idle_since is None:
                idle_since = time.time()
            time.sleep(POLL_SEC)
            continue

        idle_since = None
        status = process(item)
        store.finish_if_drained(item["job_id"])
        print(f"[worker] job={item['job_id']} product={item['product_id']} → {status}",
              flush=True)

    print("[worker] 멈춤", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
