"""
Contoso Order Status API — Azure Functions (Python v2 programming model).

This Function App is the "Action Executor" tool for the Foundry agent. The agent
calls it through an OpenAPI tool (see ``agent/openapi_spec.json``).

Routes (all under the default ``/api`` prefix):

* ``GET /api/orders/{customerId}/status``  -> order status for a customer
  (optional ``?orderId=CON-xxxxxx`` to pick a specific order; otherwise the most
  recent order is returned). Unknown or malformed IDs return HTTP 200 with
  ``{"found": false, "error": ...}`` so the agent can handle them (see ``_lookup_failure``).
* ``GET /api/health``                       -> liveness probe (always anonymous).

Auth level for the order route is controlled by the ``FUNCTION_AUTH_LEVEL`` app
setting (``function`` by default, ``anonymous`` for quick local labs). With
``function`` the caller must send the key in the ``x-functions-key`` header,
which is exactly what the Foundry "Custom keys" connection injects.

The data store is an in-memory, deterministic mock so that exam verification
queries always produce the same answers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import azure.functions as func

logger = logging.getLogger("contoso.order_api")

CUSTOMER_ID_PATTERN = re.compile(r"^CUST-\d{5}$")
ORDER_ID_PATTERN = re.compile(r"^CON-\d{6}$")
API_VERSION = "1.0.0"
CONTOSO_EXPRESS = "Contoso Express"


# --------------------------------------------------------------------------- #
# Domain model + mock data store
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    status: str
    placed_on: str
    region: str
    carrier: Optional[str]
    tracking_number: Optional[str]
    estimated_delivery: Optional[str]
    last_updated: str
    item_count: int


VALID_STATUSES = (
    "Processing",
    "Shipped",
    "Delivered",
    "Cancelled",
    "ReturnInitiated",
    "Refunded",
)

_ORDERS: tuple[Order, ...] = (
    Order(
        "CON-500101",
        "CUST-10001",
        "Shipped",
        "2026-09-28",
        "US-WA",
        CONTOSO_EXPRESS,
        "CX1Z99A0001",
        "2026-10-07",
        "2026-10-03T14:22:00Z",
        2,
    ),
    Order(
        "CON-500077",
        "CUST-10001",
        "Delivered",
        "2026-08-11",
        "US-WA",
        CONTOSO_EXPRESS,
        "CX1Z99A0777",
        None,
        "2026-08-15T09:05:00Z",
        1,
    ),
    Order(
        "CON-500102",
        "CUST-10002",
        "Processing",
        "2026-10-02",
        "US-NY",
        None,
        None,
        "2026-10-09",
        "2026-10-02T18:40:00Z",
        3,
    ),
    Order(
        "CON-500103",
        "CUST-10003",
        "Delivered",
        "2026-09-10",
        "CA-ON",
        "Maple Parcel",
        "MP7781230045",
        None,
        "2026-09-16T11:12:00Z",
        1,
    ),
    Order(
        "CON-500104",
        "CUST-10004",
        "Shipped",
        "2026-09-30",
        "US-AK",
        "Northern Freight",
        "NF0045519920",
        "2026-10-14",
        "2026-10-04T07:30:00Z",
        4,
    ),
    Order(
        "CON-500105",
        "CUST-10005",
        "Cancelled",
        "2026-09-21",
        "GB-LND",
        None,
        None,
        None,
        "2026-09-21T20:01:00Z",
        2,
    ),
    Order(
        "CON-500106",
        "CUST-10006",
        "ReturnInitiated",
        "2026-09-01",
        "DE-BE",
        "EuroLink",
        "EL55120034DE",
        None,
        "2026-09-25T13:45:00Z",
        1,
    ),
    Order(
        "CON-500107",
        "CUST-10007",
        "Refunded",
        "2026-08-02",
        "US-CA",
        CONTOSO_EXPRESS,
        "CX1Z99A0107",
        None,
        "2026-08-30T16:00:00Z",
        1,
    ),
)


class OrderStore:
    """Read-only, in-memory order repository."""

    def __init__(self, orders: tuple[Order, ...] = _ORDERS) -> None:
        self._by_customer: dict[str, list[Order]] = {}
        for order in orders:
            if order.status not in VALID_STATUSES:
                raise ValueError(f"Invalid status {order.status!r} for {order.order_id}")
            self._by_customer.setdefault(order.customer_id, []).append(order)
        for customer_orders in self._by_customer.values():
            customer_orders.sort(key=lambda o: o.placed_on, reverse=True)

    def get_orders(self, customer_id: str) -> list[Order]:
        return list(self._by_customer.get(customer_id, []))

    def get_order(self, customer_id: str, order_id: Optional[str] = None) -> Optional[Order]:
        orders = self._by_customer.get(customer_id, [])
        if not orders:
            return None
        if order_id is None:
            return orders[0]
        return next((o for o in orders if o.order_id == order_id), None)


STORE = OrderStore()


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _resolve_auth_level() -> func.AuthLevel:
    raw = os.environ.get("FUNCTION_AUTH_LEVEL", "function").strip().lower()
    levels = {
        "function": func.AuthLevel.FUNCTION,
        "anonymous": func.AuthLevel.ANONYMOUS,
        "admin": func.AuthLevel.ADMIN,
    }
    if raw not in levels:
        logger.warning("Unknown FUNCTION_AUTH_LEVEL %r; defaulting to 'function'.", raw)
        return func.AuthLevel.FUNCTION
    return levels[raw]


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "X-Api-Version": API_VERSION},
    )


def _error(status_code: int, error: str, message: str) -> func.HttpResponse:
    return _json_response({"error": error, "message": message}, status_code)


def _lookup_failure(customer_id: str, error: str, message: str) -> func.HttpResponse:
    """
    Business-level "no result" answers are HTTP 200 with ``found: false``.

    Foundry's OpenAPI tool treats any non-2xx response as a tool failure and aborts the
    whole agent run (``tool_user_error``), so the model never gets to apply its
    ORDER NOT FOUND / INVALID INPUT fallbacks. Returning 200 lets the agent read the
    error and answer the customer gracefully. Real faults (500) and auth (401) stay non-2xx.
    """
    return _json_response({"found": False, "customerId": customer_id, "error": error, "message": message})


def _serialize(order: Order) -> dict:
    data = asdict(order)
    return {
        "found": True,
        "customerId": data["customer_id"],
        "orderId": data["order_id"],
        "status": data["status"],
        "placedOn": data["placed_on"],
        "region": data["region"],
        "carrier": data["carrier"],
        "trackingNumber": data["tracking_number"],
        "estimatedDelivery": data["estimated_delivery"],
        "lastUpdated": data["last_updated"],
        "itemCount": data["item_count"],
        "otherOrderIds": [],
    }


# --------------------------------------------------------------------------- #
# Function app + routes
# --------------------------------------------------------------------------- #
app = func.FunctionApp(http_auth_level=_resolve_auth_level())


@app.function_name(name="GetOrderStatus")
@app.route(route="orders/{customerId}/status", methods=[func.HttpMethod.GET])
def get_order_status(req: func.HttpRequest) -> func.HttpResponse:
    """Return the status of a customer's order as JSON, e.g. {"status": "Shipped", ...}."""
    customer_id = (req.route_params.get("customerId") or "").strip().upper()
    order_id_raw = req.params.get("orderId")
    order_id = order_id_raw.strip().upper() if order_id_raw else None

    if not CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        logger.info("Rejected malformed customerId")
        return _lookup_failure(
            customer_id,
            "InvalidCustomerId",
            "customerId must match the format CUST-12345 (CUST- followed by 5 digits).",
        )
    if order_id is not None and not ORDER_ID_PATTERN.fullmatch(order_id):
        return _lookup_failure(
            customer_id,
            "InvalidOrderId",
            "orderId must match the format CON-123456 (CON- followed by 6 digits).",
        )

    try:
        order = STORE.get_order(customer_id, order_id)
    except Exception:  # defensive: never leak internals to the agent
        logger.exception("Order lookup failed for %s", customer_id)
        return _error(500, "InternalError", "The order service is temporarily unavailable.")

    if order is None:
        target = f"order {order_id} for customer {customer_id}" if order_id else f"customer {customer_id}"
        logger.info("No order found for %s", target)
        return _lookup_failure(customer_id, "OrderNotFound", f"No order was found for {target}.")

    payload = _serialize(order)
    payload["otherOrderIds"] = [o.order_id for o in STORE.get_orders(customer_id) if o.order_id != order.order_id]
    logger.info(
        "Order status served: customer=%s order=%s status=%s",
        customer_id,
        order.order_id,
        order.status,
    )
    return _json_response(payload)


@app.function_name(name="Health")
@app.route(route="health", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response(
        {
            "status": "Healthy",
            "service": "contoso-order-api",
            "version": API_VERSION,
            "timeUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
