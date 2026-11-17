"""Асинхронный брокер RabbitMQ поверх aio_pika.

Каждый сервис создаёт один Broker в lifespan, подписывается на нужные
события и публикует свои. Контракт конверта — EventEnvelope из events.yaml.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import aio_pika
from aio_pika import ExchangeType, Message

logger = logging.getLogger(__name__)

EXCHANGE_NAME = "bookstore"
DEFAULT_URL = "amqp://guest:guest@rabbitmq:5672/"

Handler = Callable[[dict], Awaitable[None]]


class Broker:
    def __init__(self, url: str = DEFAULT_URL):
        self.url = url
        self.connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.publish_channel: Optional[aio_pika.abc.AbstractChannel] = None
        self.exchange: Optional[aio_pika.abc.AbstractExchange] = None
        self._consumer_channels: list[aio_pika.abc.AbstractChannel] = []

    async def connect(self) -> None:
        self.connection = await aio_pika.connect_robust(self.url)
        self.publish_channel = await self.connection.channel()
        self.exchange = await self.publish_channel.declare_exchange(
            EXCHANGE_NAME, ExchangeType.TOPIC, durable=True
        )
        logger.info("Broker connected to %s", self.url)

    async def close(self) -> None:
        for ch in self._consumer_channels:
            try:
                await ch.close()
            except Exception:
                pass
        if self.connection:
            await self.connection.close()
        logger.info("Broker connection closed")

    async def publish(
        self,
        event_type: str,
        payload: dict,
        correlation_id: Optional[str] = None,
    ) -> dict:
        """Публикует событие в exchange с routing_key == event_type."""
        if not self.exchange:
            raise RuntimeError("Broker is not connected")

        envelope = {
            "eventId": str(uuid.uuid4()),
            "eventType": event_type,
            "schemaVersion": 1,
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "correlationId": correlation_id or str(uuid.uuid4()),
            "payload": payload,
        }
        message = Message(
            body=json.dumps(envelope).encode("utf-8"),
            content_type="application/json",
            message_id=envelope["eventId"],
            correlation_id=envelope["correlationId"],
        )
        await self.exchange.publish(message, routing_key=event_type)
        logger.info("Published %s (eventId=%s)", event_type, envelope["eventId"])
        return envelope

    async def subscribe(
        self,
        queue_name: str,
        routing_keys: list[str],
        handler: Handler,
    ) -> aio_pika.abc.AbstractQueue:
        """Создаёт durable-очередь, биндит к exchange по routing_keys и вешает consumer."""
        if not self.connection:
            raise RuntimeError("Broker is not connected")

        channel = await self.connection.channel()
        self._consumer_channels.append(channel)
        exchange = await channel.declare_exchange(
            EXCHANGE_NAME, ExchangeType.TOPIC, durable=True
        )
        queue = await channel.declare_queue(queue_name, durable=True)

        for rk in routing_keys:
            await queue.bind(exchange, routing_key=rk)

        async def _on_message(message: aio_pika.IncomingMessage) -> None:
            async with message.process(requeue=False):
                try:
                    envelope = json.loads(message.body)
                except json.JSONDecodeError:
                    logger.exception("Invalid JSON in message")
                    return
                try:
                    await handler(envelope)
                except Exception:
                    logger.exception("Handler failed for %s", envelope.get("eventType"))

        await queue.consume(_on_message)
        logger.info("Subscribed queue=%s routing_keys=%s", queue_name, routing_keys)
        return queue
