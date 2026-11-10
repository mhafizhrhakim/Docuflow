import os
from dataclasses import dataclass
from urllib.parse import urlsplit


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

    @property
    def cors_origins(self) -> list[str]:
        """Allow one or more explicit browser origins; never use a wildcard."""
        return [origin.strip().rstrip("/") for origin in self.frontend_origin.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production" or os.getenv("RENDER", "").lower() == "true"


settings = Settings()

def validate_production_frontend_origins(configured_value: str, origins: list[str]) -> None:
    """Keep production CORS explicit; allow plain HTTP only for local loopback."""
    if not configured_value.strip() or not origins:
        raise RuntimeError("FRONTEND_ORIGIN must be set to the deployed frontend origin in production.")

    for origin in origins:
        try:
            parsed_origin = urlsplit(origin)
            hostname = (parsed_origin.hostname or "").lower()
        except ValueError as exc:
            raise RuntimeError("FRONTEND_ORIGIN must contain valid origins without paths in production.") from exc

        local_http = parsed_origin.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed_origin.netloc
            or (parsed_origin.scheme != "https" and not local_http)
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username
            or parsed_origin.password
        ):
            raise RuntimeError(
                "FRONTEND_ORIGIN must contain explicit HTTPS origins without paths in production "
                "(HTTP is allowed only for localhost loopback Compose testing)."
            )


if settings.is_production:
    validate_production_frontend_origins(
        os.getenv("FRONTEND_ORIGIN", ""),
        settings.cors_origins,
    )
