import logging
import os
import re
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("api-gateway")

SERVICES = {
    "catalog":   os.getenv("CATALOG_URL",   "http://catalog-service:8001"),
    "inventory": os.getenv("INVENTORY_URL", "http://inventory-service:8002"),
    "order":     os.getenv("ORDER_URL",     "http://order-service:8003"),
    "payment":   os.getenv("PAYMENT_URL",   "http://payment-service:8004"),
    "review":    os.getenv("REVIEW_URL",    "http://review-service:8005"),
}

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ROLES = "CLIENT"

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Порядок важен: специфичные маршруты раньше общих
ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"^/api/v1/books/{UUID_RE}/reviews"), "review"),
    (re.compile(rf"^/api/v1/books/{UUID_RE}/rating"),  "review"),
    (re.compile(rf"^/api/v1/books/{UUID_RE}/stock"),   "inventory"),
    (re.compile(r"^/api/v1/books"),                    "catalog"),
    (re.compile(r"^/api/v1/genres"),                   "catalog"),
    (re.compile(r"^/api/v1/authors"),                  "catalog"),
    (re.compile(r"^/api/v1/admin/books"),              "catalog"),
    (re.compile(r"^/api/v1/admin/stock"),              "inventory"),
    (re.compile(r"^/api/v1/admin/reviews"),            "review"),
    (re.compile(r"^/api/v1/cart"),                     "order"),
    (re.compile(r"^/api/v1/orders"),                   "order"),
    (re.compile(r"^/api/v1/payments"),                 "payment"),
]

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length",
}

app = FastAPI(title="api-gateway", version="1.0.0")


def _match(path: str) -> Optional[str]:
    for pattern, svc in ROUTES:
        if pattern.match(path):
            return svc
    return None


def _build_headers(request: Request) -> dict:
    headers = {}
    lower = set()
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        headers[k] = v
        lower.add(lk)
    if "x-user-id" not in lower:
        headers["X-User-Id"] = DEFAULT_USER_ID
    if "x-roles" not in lower:
        headers["X-Roles"] = DEFAULT_ROLES
    if "x-correlation-id" not in lower:
        headers["X-Correlation-Id"] = str(uuid.uuid4())
    return headers


@app.get("/health")
async def health():
    return {"status": "ok", "services": list(SERVICES.keys())}


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(full_path: str, request: Request):
    path = "/" + full_path
    svc = _match(path)
    if not svc:
        return Response(
            content=(
                '{"type":"https://bookstore.local/errors/not-found",'
                '"title":"Route not found","status":404,'
                f'"detail":"No route for {path}"}}'
            ),
            status_code=404,
            media_type="application/problem+json",
        )

    target = SERVICES[svc] + path
    if request.url.query:
        target += "?" + request.url.query

    body = await request.body()
    headers = _build_headers(request)

    logger.info("PROXY %s %s -> %s", request.method, path, svc)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(
            request.method, target,
            headers=headers,
            content=body if body else None,
        )

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
    )
