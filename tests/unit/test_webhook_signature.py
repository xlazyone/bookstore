import hashlib
import hmac

from services.payment.security import verify_webhook_signature

SECRET = "dev-secret-change-me"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature():
    body = b'{"eventId":"x","paymentId":"y","status":"SUCCEEDED","amount":100}'
    assert verify_webhook_signature(SECRET, body, _sign(body)) is True


def test_invalid_signature():
    body = b'{"eventId":"x"}'
    assert verify_webhook_signature(SECRET, body, "wrong") is False


def test_tampered_body():
    body = b'{"eventId":"x"}'
    sig = _sign(body)
    assert verify_webhook_signature(SECRET, b'{"eventId":"y"}', sig) is False
