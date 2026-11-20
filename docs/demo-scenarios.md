# Демонстрационные сценарии BookStore (ЛР4)

Все запросы — **только через API Gateway** (`http://localhost:8080`).

## Что реальное, а что — мок

| Компонент | Статус | Комментарий |
|-----------|--------|-------------|
| catalog / inventory / order / payment / review / notification | **реальные** | FastAPI + SQLite (файл на сервис) |
| API Gateway | **реальный** (FastAPI-прокси) | В production — Nginx + Keycloak |
| RabbitMQ | **реальный** | topic exchange `bookstore`, durable-очереди, management UI на `:15672` |
| MailHog | **реальный SMTP** | письма видны в UI `http://localhost:8025` |
| Mock Payment Gateway | **мок** | имитирует ЮKassa, шлёт webhook через 3 с |
| Keycloak | **мок** | `shared/auth.py`: `X-User-Id`/`X-Roles` из заголовков, JWT не валидируется |
| PostgreSQL / MongoDB / Redis | **заменены** | SQLite на сервис (разрешено преподавателем), TTL-резервы — фоновый таск |

## Запуск

```bash
docker compose up -d --build
sleep 25
docker compose ps          # 10 контейнеров Up
```

## Сценарий 1. Happy path (успешная покупка)

**Участники:** client → gateway → order → catalog → inventory → payment → mock-psp → RabbitMQ → inventory / review / notification.

```bash
BOOK_ID="9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a"
USER_ID="00000000-0000-0000-0000-000000000001"

# 1. Корзина
curl -s -X POST http://localhost:8080/api/v1/cart/items \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_ID" \
  -d "{\"bookId\": \"$BOOK_ID\", \"quantity\": 1}"

# 2. Заказ (сага запускается сразу)
OID=$(curl -s -X POST http://localhost:8080/api/v1/orders \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"currency": "RUB", "simulatePayment": "SUCCESS"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

sleep 6

# 3. Статус заказа
curl -s http://localhost:8080/api/v1/orders/$OID -H "X-User-Id: $USER_ID" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])'
```

**Ожидаемо:** `PENDING_PAYMENT` сразу после POST, `CONFIRMED` через 6 с.

**Что проверить в UI:**
- **RabbitMQ** `:15672` → Queues → `inventory.order.events`, `order.payment.events`, `notification.events`, `review.order.events` — у каждой `Consumers: 1`.
- **Exchanges → bookstore → Bindings** — карта routing keys.
- **MailHog** `:8025` — 2 письма: «Оплата получена» + «Заказ подтверждён».

**Взаимодействия:**
- sync: order→catalog (batch), order→inventory (reserve), order→payment (create), payment→mock-psp (session), mock-psp→payment (webhook)
- async: payment `payment.completed` → order; order `order.confirmed` → inventory, review, notification

## Сценарий 2. Failure path (отказ оплаты + компенсация)

```bash
USER_ID="00000000-0000-0000-0000-000000000001"

curl -s -X POST http://localhost:8080/api/v1/cart/items \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_ID" \
  -d "{\"bookId\": \"$BOOK_ID\", \"quantity\": 1}" > /dev/null

OID2=$(curl -s -X POST http://localhost:8080/api/v1/orders \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"currency": "RUB", "simulatePayment": "FAIL"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

sleep 6
curl -s http://localhost:8080/api/v1/orders/$OID2 -H "X-User-Id: $USER_ID" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])'
```

**Ожидаемо:** `CANCELLED`.

**В логах:**
- order: `Order ... cancelled (payment failed)` → публикует `order.cancelled`
- inventory: `Reservation ... released by order.cancelled` → `stock.released`

**Компенсация:** резерв снимается, остаток возвращается в `available`. Проверить:
```bash
curl -s http://localhost:8080/api/v1/books/$BOOK_ID/stock | python3 -m json.tool
```

**MailHog:** письма «Ошибка оплаты» + «Заказ отменён».

## Сценарий 3. Нехватка остатка на резерве

Если `available < quantity`:
- inventory возвращает 409 Problem
- order публикует `order.cancelled` с `reason: RESERVE_FAILED`
- заказ `CANCELLED`, корзина сохраняется

## Сценарий 4. Идемпотентность создания заказа

Повторить `POST /api/v1/orders` с **тем же** `Idempotency-Key`:
```bash
IDEM=$(python3 -c 'import uuid; print(uuid.uuid4())')
# первый запрос
curl -s -X POST http://localhost:8080/api/v1/orders \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $IDEM" -d '{"currency":"RUB","simulatePayment":"SUCCESS"}'
# второй — тот же ответ, второй саги нет
```

**Ожидаемо:** тот же `id`, в логах order — **нет** второго `Saga started`.

## Сценарий 5. TTL резерва (опционально)

В `docker-compose.yml` поставить `RESERVATION_TTL_MINUTES: "1"` у `inventory-service` и `order-service`, перезапустить. Создать заказ, **не оплачивать**. Через 1.5 мин:
- inventory: `Reservation ... expired`
- публикуется `stock.reservation.expired`
- order (если подписан) публикует `order.expired`

В базовой версии order **не подписан** на `stock.reservation.expired` (по README — да, но мы упростили). Резерв всё равно освобождается фоновым таском.

## Сценарий 6. Отмена заказа пользователем

Пока заказ в `PENDING_PAYMENT` (до webhook — окно ~3 с):
```bash
curl -s -X POST http://localhost:8080/api/v1/orders/$OID/cancel \
  -H "X-User-Id: $USER_ID"
```
**Ожидаемо:** `CANCELLED`, inventory освободил резерв. Если webhook уже прошёл — `409 Cannot cancel order` (валидный исход).

## Сценарий 7. Модерация отзыва + async `review.published`

```bash
# создание
USER_NEW="00000000-0000-0000-0000-000000000007"
RID=$(curl -s -X POST http://localhost:8080/api/v1/books/$BOOK_ID/reviews \
  -H "Content-Type: application/json" -H "X-User-Id: $USER_NEW" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"rating": 5, "text": "demo"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

# модерация
curl -s -X POST http://localhost:8080/api/v1/admin/reviews/$RID/moderate \
  -H "Content-Type: application/json" -H "X-Roles: MODERATOR" \
  -d '{"action": "APPROVE"}'
```

**Ожидаемо:** `PUBLISHED`, публикуется `review.published`, notification шлёт письмо.

## Единая модель ошибок (RFC 7807)

Все 4xx/5xx от любых сервисов — `application/problem+json` с `type/title/status/detail/instance/correlationId`.

```bash
curl -s -i http://localhost:8080/api/v1/unknown | head -6
# 404, content-type: application/problem+json

curl -s -i http://localhost:8080/api/v1/books/00000000-0000-0000-0000-000000000000 | head -6
# 404 Book not found в Problem-формате
```

## Корреляция

```bash
curl -s -i http://localhost:8080/api/v1/books \
  -H "X-Correlation-Id: 33333333-3333-3333-3333-333333333333" | grep -i correlation
# ответ содержит тот же X-Correlation-Id
```

Один и тот же `X-Correlation-Id` проходит через gateway → order → inventory → payment → события RabbitMQ (в `correlationId` конверта).
