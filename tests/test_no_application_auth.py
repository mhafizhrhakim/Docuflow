import os
from pathlib import Path
import subprocess
import sys

import pytest

import backend.main as main_module
from backend.core.config import Settings, validate_production_frontend_origins


def test_production_frontend_origin_validation_remains_strict():
    validate_production_frontend_origins(
        "https://docuflow.internal.example",
        ["https://docuflow.internal.example"],
    )
    validate_production_frontend_origins("http://localhost:8080", ["http://localhost:8080"])

    for configured, origins in (
        ("", []),
        ("http://docuflow.internal.example", ["http://docuflow.internal.example"]),
        ("https://docuflow.internal.example/path", ["https://docuflow.internal.example/path"]),
        ("*", ["*"]),
    ):
        with pytest.raises(RuntimeError):
            validate_production_frontend_origins(configured, origins)


def test_settings_have_no_application_auth_configuration():
    field_names = set(Settings.__dataclass_fields__)
    assert not any("auth" in name or "password" in name or "secret" in name for name in field_names)


def test_api_has_no_application_auth_middleware_or_routes():
    middleware_names = [item.cls.__name__ for item in main_module.app.user_middleware]
    route_paths = set(main_module.app.openapi()["paths"])

    assert middleware_names == ["CORSMiddleware"]
    assert not any(path.startswith("/api/auth/") for path in route_paths)
    assert "/api/health" in route_paths
    assert "/api/documents/inspect" in route_paths
    assert "/api/documents/inspect/progress/{progress_id}" in route_paths
    assert "/api/documents/compare" in route_paths


def test_production_app_starts_without_application_auth_secrets():
    env = os.environ.copy()
    for name in ("DOCUFLOW_ACCESS_PASSWORD", "DOCUFLOW_AUTH_SECRET", "DOCUFLOW_AUTH_TTL_SECONDS"):
        env.pop(name, None)
    env["APP_ENV"] = "production"
    env["FRONTEND_ORIGIN"] = "https://docuflow.internal.example"
    project_root = Path(__file__).resolve().parents[1]
    code = (
        "import backend.main as m; "
        "assert m.app.docs_url is None; "
        "assert [x.cls.__name__ for x in m.app.user_middleware] == ['CORSMiddleware']; "
        "assert '/api/documents/inspect' in m.app.openapi()['paths']"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
