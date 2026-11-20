"""Шаблоны писем. Простой строковый шаблон без Jinja, чтобы не тянуть зависимость."""


def render_order_confirmed(payload: dict) -> tuple[str, str]:
    order_id = payload.get("orderId", "")
    total = payload.get("totalAmount", 0)
    currency = payload.get("currency", "RUB")
    subject = f"Ваш заказ #{order_id[:8]} подтверждён"
    body = (
        f"Здравствуйте!\n\n"
        f"Ваш заказ №{order_id} успешно оплачен.\n"
        f"Сумма: {total} {currency}\n\n"
        f"Спасибо за покупку!\nBookStore"
    )
    return subject, body


def render_order_cancelled(payload: dict) -> tuple[str, str]:
    order_id = payload.get("orderId", "")
    reason = payload.get("reason", "")
    subject = f"Заказ #{order_id[:8]} отменён"
    body = (
        f"Здравствуйте!\n\n"
        f"Ваш заказ №{order_id} был отменён.\n"
        f"Причина: {reason}\n\n"
        f"Если это ошибка — свяжитесь с поддержкой.\nBookStore"
    )
    return subject, body


def render_order_expired(payload: dict) -> tuple[str, str]:
    order_id = payload.get("orderId", "")
    subject = f"Заказ #{order_id[:8]} истёк"
    body = (
        f"Здравствуйте!\n\n"
        f"Срок оплаты заказа №{order_id} истёк, заказ закрыт.\n"
        f"Вы можете оформить новый заказ на нашем сайте.\nBookStore"
    )
    return subject, body


def render_payment_completed(payload: dict) -> tuple[str, str]:
    order_id = payload.get("orderId", "")
    amount = payload.get("amount", 0)
    subject = f"Оплата по заказу #{order_id[:8]} получена"
    body = (
        f"Здравствуйте!\n\n"
        f"Платёж по заказу №{order_id} успешно проведён.\n"
        f"Сумма: {amount}\n\n"
        f"Чек будет доступен в личном кабинете.\nBookStore"
    )
    return subject, body


def render_payment_failed(payload: dict) -> tuple[str, str]:
    order_id = payload.get("orderId", "")
    subject = f"Ошибка оплаты по заказу #{order_id[:8]}"
    body = (
        f"Здравствуйте!\n\n"
        f"К сожалению, оплата по заказу №{order_id} не прошла.\n"
        f"Попробуйте ещё раз или свяжитесь с поддержкой.\nBookStore"
    )
    return subject, body


def render_review_published(payload: dict) -> tuple[str, str]:
    book_id = payload.get("bookId", "")
    rating = payload.get("rating", 0)
    subject = "Ваш отзыв опубликован"
    body = (
        f"Здравствуйте!\n\n"
        f"Ваш отзыв на книгу {book_id} опубликован (оценка {rating}/5).\n"
        f"Спасибо, что делитесь мнением!\nBookStore"
    )
    return subject, body


RENDERERS = {
    "order.confirmed": render_order_confirmed,
    "order.cancelled": render_order_cancelled,
    "order.expired": render_order_expired,
    "payment.completed": render_payment_completed,
    "payment.failed": render_payment_failed,
    "review.published": render_review_published,
}

NOTIFICATION_TYPES = {
    "order.confirmed": "ORDER_CONFIRMED",
    "order.cancelled": "ORDER_CANCELLED",
    "order.expired": "ORDER_EXPIRED",
    "payment.completed": "ORDER_CONFIRMED",
    "payment.failed": "PAYMENT_FAILED",
    "review.published": "REVIEW_PUBLISHED",
}
