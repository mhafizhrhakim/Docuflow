import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "DocuFlow")
    app_env: str = os.getenv("APP_ENV", "development")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "50"))
    # The inspection executor is deliberately small because OCR and native PDF
    # parsing are CPU/RAM intensive. Queue capacity includes active jobs.
    inspection_workers: int = max(1, int(os.getenv("INSPECTION_WORKERS", "2")))
    inspection_queue_slots: int = max(0, int(os.getenv("INSPECTION_QUEUE_SLOTS", "2")))
    # Bound ONNX's per-session CPU threads so concurrent OCR jobs do not each
    # create a machine-wide worker pool. The value can be tuned on the host.
    ocr_intra_op_threads: int = max(
        1,
        min(
            os.cpu_count() or 1,
            int(os.getenv("OCR_INTRA_OP_THREADS", "2")),
        ),
    )
    # Keep the existing progress retention default (30 minutes) unless the
    # deployment explicitly configures another duration.
    upload_ttl_minutes: int = int(os.getenv("UPLOAD_TTL_MINUTES", "30"))


settings = Settings()
