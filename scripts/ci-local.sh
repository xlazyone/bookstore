#!/usr/bin/env bash
# Локальный прогон CI без GitHub Actions.
# Делает то же, что .github/workflows/ci.yml, но без gitleaks (если он не установлен).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/3] Install deps ==="
python3 -m pip install -q -r shared/requirements.txt -r shared/requirements-dev.txt

echo "=== [2/3] Unit tests ==="
pytest tests/unit -v

echo "=== [3/3] Build Docker images ==="
SERVICES=(
  "catalog:services/catalog"
  "inventory:services/inventory"
  "order:services/order"
  "payment:services/payment"
  "review:services/review"
  "notification:services/notification"
  "gateway:gateway"
  "mock-payment-gateway:mocks/payment-gateway"
)
for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  path="${entry##*:}"
  echo "--- build $name ($path/Dockerfile)"
  docker build -q -t bookstore-$name:ci -f "$path/Dockerfile" .
done

echo
echo "CI-LOCAL OK"
