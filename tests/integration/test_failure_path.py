import time
import uuid

import httpx
import pytest

BASE = "http://localhost:8080"
BOOK = "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a"
USER = "00000000-0000-0000-0000-000000000011"

pytestmark = pytest.mark.integration


def _auth():
    return {"X-User-Id": USER}


def test_failure_path_cancels_and_releases():
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        c.put(f"/api/v1/admin/stock/{BOOK}",
              headers={**_auth(), "X-Roles": "MANAGER"},
              json={"delta": 5, "reason": "RESTOCK"})

        c.post("/api/v1/cart/items", headers=_auth(),
               json={"bookId": BOOK, "quantity": 1})

        r = c.post("/api/v1/orders",
                   headers={**_auth(), "Idempotency-Key": str(uuid.uuid4())},
                   json={"currency": "RUB", "simulatePayment": "FAIL"})
        oid = r.json()["id"]

        for _ in range(20):
            time.sleep(0.5)
            r = c.get(f"/api/v1/orders/{oid}", headers=_auth())
            if r.json()["status"] == "CANCELLED":
                break
        assert r.json()["status"] == "CANCELLED"
