"""Dedicated, single-process persistent monitoring service (never a Vercel task)."""
from contextlib import asynccontextmanager
import os
import threading
from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv

from alert_api import router, get_store, _local_configured

load_dotenv(Path(__file__).parent / ".env")


@asynccontextmanager
async def lifespan(app):
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV"):
        raise RuntimeError("감시 서버는 상시 실행 호스트에서만 실행할 수 있습니다.")
    if not _local_configured():
        raise RuntimeError("SIGNAL_ALERT_DB와 24자 이상 ALERT_ACCESS_TOKEN이 필요합니다.")
    if os.getenv("ALERT_SERVICE_URL"):
        raise RuntimeError("감시 호스트에는 ALERT_SERVICE_URL을 설정하지 마세요.")
    from alert_worker import MonitorWorker
    from monitor_evidence import build_evidence_for_stock
    stop = threading.Event()
    worker = MonitorWorker(get_store(), evidence_provider=build_evidence_for_stock,
                           interval_seconds=float(os.getenv("ALERT_POLL_SECONDS", "15")))
    thread = threading.Thread(target=worker.run, args=(stop,), name="stock-monitor", daemon=True)
    thread.start()
    app.state.monitor_stop = stop
    app.state.monitor_thread = thread
    try:
        yield
    finally:
        stop.set()
        # Waiting is bounded; the process superviser must stop old processes before restart.
        import anyio
        await anyio.to_thread.run_sync(lambda: thread.join(timeout=5))


app = FastAPI(title="StockDesk private monitoring service", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health():
    return {"ok": True, "service": "stock-monitor"}
