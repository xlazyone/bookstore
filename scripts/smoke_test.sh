#!/usr/bin/env bash
# Smoke-тест всего стека через gateway. Требует docker compose up -d.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
BOOK="9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a"
USER="00000000-0000-0000-0000-000000000077"

echo "1. gateway health"
curl -fsS "$BASE/health" > /dev/null && echo "  OK"

echo "2. catalog search"
curl -fsS -G "$BASE/api/v1/books" --data-urlencode "q=война" \
  | python3 -c 'import sys,json; assert json.load(sys.stdin)["totalElements"]>=1' && echo "  OK"

echo "3. restock"
curl -fsS -X PUT "$BASE/api/v1/admin/stock/$BOOK" \
  -H "X-Roles: MANAGER" -H "Content-Type: application/json" \
  -d '{"delta":10,"reason":"RESTOCK"}' > /dev/null && echo "  OK"

echo "4. add to cart"
curl -fsS -X POST "$BASE/api/v1/cart/items" \
  -H "X-User-Id: $USER" -H "Content-Type: application/json" \
  -d "{\"bookId\":\"$BOOK\",\"quantity\":1}" > /dev/null && echo "  OK"

echo "5. create order (SUCCESS)"
OID=$(curl -fsS -X POST "$BASE/api/v1/orders" \
  -H "X-User-Id: $USER" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"currency":"RUB","simulatePayment":"SUCCESS"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "  order=$OID"

echo "6. wait for CONFIRMED"
for i in $(seq 1 20); do
  STATUS=$(curl -fsS "$BASE/api/v1/orders/$OID" -H "X-User-Id: $USER" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = "CONFIRMED" ] && break
  sleep 0.5
done
[ "$STATUS" = "CONFIRMED" ] && echo "  OK" || (echo "  FAIL: $STATUS"; exit 1)

echo "7. create order (FAIL) → CANCELLED"
curl -fsS -X POST "$BASE/api/v1/cart/items" \
  -H "X-User-Id: $USER" -H "Content-Type: application/json" \
  -d "{\"bookId\":\"$BOOK\",\"quantity\":1}" > /dev/null
OID2=$(curl -fsS -X POST "$BASE/api/v1/orders" \
  -H "X-User-Id: $USER" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"currency":"RUB","simulatePayment":"FAIL"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
for i in $(seq 1 20); do
  STATUS=$(curl -fsS "$BASE/api/v1/orders/$OID2" -H "X-User-Id: $USER" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = "CANCELLED" ] && break
  sleep 0.5
done
[ "$STATUS" = "CANCELLED" ] && echo "  OK" || (echo "  FAIL: $STATUS"; exit 1)

echo "8. problem details on 404"
CT=$(curl -s -o /dev/null -w '%{content_type}' \
  "$BASE/api/v1/books/00000000-0000-0000-0000-000000000000")
[[ "$CT" == application/problem+json* ]] && echo "  OK" || (echo "  FAIL: $CT"; exit 1)

echo
echo "SMOKE OK"
