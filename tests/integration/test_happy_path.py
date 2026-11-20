import uuid

import httpx
import pytest

BASE = "http://localhost:8080"
BOOK = "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a"
USER = "00000000-0000-0000-0000-000000000010"

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        yield c


def _auth(user=USER):
    return {"X-User-Id": user}


def test_gateway_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "catalog" in r.json()["services"]


def test_catalog_and_search(client):
    r = client.get("/api/v1/books", params={"q": "война"})
    assert r.status_code == 200
    assert r.json()["totalElements"] >= 1


def test_happy_path_saga(client):
    # RESTOCK на случай, если остаток кончился
    client.put(
        f"/api/v1/admin/stock/{BOOK}",
        headers={**_auth(), "X-Roles": "MANAGER"},
        json={"delta": 5, "reason": "RESTOCK"},
    )

    client.post("/api/v1/cart/items", headers=_auth(),
                json={"bookId": BOOK, "quantity": 1})

    r = client.post(
        "/api/v1/orders",
        headers={**_auth(), "Idempotency-Key": str(uuid.uuid4())},
        json={"currency": "RUB", "simulatePayment": "SUCCESS"},
    )
    assert r.status_code == 201
    order = r.json()
    assert order["status"] in {"PENDING_PAYMENT", "CONFIRMED"}

    import time
    for _ in range(20):
        time.sleep(0.5)
        r = client.get(f"/api/v1/orders/{order['id']}", headers=_auth())
        if r.json()["status"] == "CONFIRMED":
            break
    assert r.json()["status"] == "CONFIRMED"


def test_idempotency(client):
    client.post("/api/v1/cart/items", headers=_auth(),
                json={"bookId": BOOK, "quantity": 1})
    key = str(uuid.uuid4())
    r1 = client.post("/api/v1/orders",
                     headers={**_auth(), "Idempotency-Key": key},
                     json={"currency": "RUB", "simulatePayment": "SUCCESS"})
    r2 = client.post("/api/v1/orders",
                     headers={**_auth(), "Idempotency-Key": key},
                     json={"currency": "RUB", "simulatePayment": "SUCCESS"})
    assert r1.json()["id"] == r2.json()["id"]


def test_problem_details_on_404(client):
    r = client.get("/api/v1/books/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
