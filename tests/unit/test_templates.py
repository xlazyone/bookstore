from services.notification import templates


def test_render_order_confirmed():
    subject, body = templates.render_order_confirmed({
        "orderId": "d3984a6c-46dd-4125-bbd4-23d2a8dc4892",
        "totalAmount": 1800.0, "currency": "RUB",
    })
    assert "подтверждён" in subject
    assert "1800.0 RUB" in body


def test_all_event_types_have_renderer():
    expected = {
        "order.confirmed", "order.cancelled", "order.expired",
        "payment.completed", "payment.failed", "review.published",
    }
    assert expected == set(templates.RENDERERS.keys())


def test_render_review_published():
    subject, body = templates.render_review_published({
        "bookId": "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a", "rating": 5,
    })
    assert "опубликован" in subject
    assert "5/5" in body
