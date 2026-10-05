"""Unit tests for the Azure Function order-status API (no Azure resources needed)."""

import json

import azure.functions as func
import pytest

import function_app


def _call(customer_id: str, order_id: str | None = None) -> func.HttpResponse:
    params = {"orderId": order_id} if order_id else {}
    req = func.HttpRequest(
        method="GET",
        url=f"/api/orders/{customer_id}/status",
        route_params={"customerId": customer_id},
        params=params,
        body=b"",
    )
    return function_app.get_order_status(req)


def test_shipped_order_returns_status_payload():
    resp = _call("CUST-10001")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    body = json.loads(resp.get_body())
    assert body["status"] == "Shipped"
    assert body["orderId"] == "CON-500101"
    assert body["trackingNumber"] == "CX1Z99A0001"
    assert body["otherOrderIds"] == ["CON-500077"]


def test_lowercase_customer_id_is_normalised():
    assert json.loads(_call("cust-10002").get_body())["status"] == "Processing"


def test_specific_order_lookup():
    body = json.loads(_call("CUST-10001", "CON-500077").get_body())
    assert body["status"] == "Delivered"
    assert body["otherOrderIds"] == ["CON-500101"]


@pytest.mark.parametrize("bad_id", ["12345", "CUST-1234", "CUST-123456", "CUST-ABCDE", "", "CUST-10001;DROP"])
def test_malformed_customer_id_is_400(bad_id):
    resp = _call(bad_id)
    assert resp.status_code == 400
    assert json.loads(resp.get_body())["error"] == "InvalidCustomerId"


def test_malformed_order_id_is_400():
    resp = _call("CUST-10001", "500101")
    assert resp.status_code == 400
    assert json.loads(resp.get_body())["error"] == "InvalidOrderId"


def test_unknown_customer_is_404():
    resp = _call("CUST-99999")
    assert resp.status_code == 404
    assert json.loads(resp.get_body())["error"] == "OrderNotFound"


def test_unknown_order_for_known_customer_is_404():
    assert _call("CUST-10001", "CON-999999").status_code == 404


def test_store_failure_is_500_without_leaking(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db password=hunter2")

    monkeypatch.setattr(function_app.STORE, "get_order", boom)
    resp = _call("CUST-10001")
    assert resp.status_code == 500
    assert "hunter2" not in resp.get_body().decode()


def test_health_endpoint():
    req = func.HttpRequest(method="GET", url="/api/health", body=b"")
    body = json.loads(function_app.health(req).get_body())
    assert body["status"] == "Healthy"


def test_auth_level_resolution(monkeypatch):
    monkeypatch.setenv("FUNCTION_AUTH_LEVEL", "anonymous")
    assert function_app._resolve_auth_level() == func.AuthLevel.ANONYMOUS
    monkeypatch.setenv("FUNCTION_AUTH_LEVEL", "bogus")
    assert function_app._resolve_auth_level() == func.AuthLevel.FUNCTION


def test_every_mock_status_is_in_openapi_enum():
    from orchestrator.config import OPENAPI_SPEC_PATH

    spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    enum = spec["components"]["schemas"]["OrderStatusResponse"]["properties"]["status"]["enum"]
    assert set(function_app.VALID_STATUSES) == set(enum)
