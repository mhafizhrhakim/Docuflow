import json
from urllib.request import urlopen


def test_health_endpoint_returns_ok(live_http_server):
    with urlopen(f"{live_http_server}/api/health", timeout=5) as response:
        body = json.loads(response.read())
    assert response.status == 200
    assert body["status"] == "ok"
    assert body["app_name"] == "DocuFlow"


def test_root_endpoint_returns_api_status(live_http_server):
    with urlopen(f"{live_http_server}/") as response:
        body = json.loads(response.read())
    assert response.status == 200
    assert body["status"] == "ok"
