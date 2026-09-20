"""Event bus: history bounding, late-joiner replay, and drop-oldest overflow."""

from __future__ import annotations

import asyncio

from engine.telemetry.events import Event, EventBus, EventChannel, EventType


def _feed(i: int) -> Event:
    return Event(EventType.REQUEST_PROGRESS, {"request_id": f"r{i}", "tokens": i})


def test_history_is_bounded() -> None:
    bus = EventBus(history=3)
    for i in range(10):
        bus.publish(EventChannel.FEED, _feed(i))
    recent = bus.recent(EventChannel.FEED)
    assert len(recent) == 3
    assert [e.data["tokens"] for e in recent] == [7, 8, 9]


def test_channels_are_independent() -> None:
    bus = EventBus(history=5)
    bus.publish(EventChannel.FEED, _feed(1))
    bus.publish(EventChannel.METRICS, Event(EventType.METRICS, {"ok": True}))
    assert len(bus.recent(EventChannel.FEED)) == 1
    assert len(bus.recent(EventChannel.METRICS)) == 1


def test_event_to_dict_includes_type_and_data() -> None:
    event = Event(EventType.REQUEST_START, {"request_id": "r1", "model": "m"})
    payload = event.to_dict()
    assert payload["type"] == "request.start"
    assert payload["request_id"] == "r1"
    assert payload["model"] == "m"
    assert "ts" in payload


def test_late_joiner_gets_history_then_live() -> None:
    async def body() -> None:
        bus = EventBus(history=10)
        for i in range(3):
            bus.publish(EventChannel.FEED, _feed(i))

        received: list[int] = []
        sub = bus.subscribe(EventChannel.FEED, replay_history=True)

        async def consume() -> None:
            async for event in sub:
                received.append(event.data["tokens"])
                if len(received) == 5:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)  # let it drain history
        assert bus.subscriber_count(EventChannel.FEED) == 1
        bus.publish(EventChannel.FEED, _feed(3))
        bus.publish(EventChannel.FEED, _feed(4))
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [0, 1, 2, 3, 4]
        await sub.aclose()
        assert bus.subscriber_count(EventChannel.FEED) == 0

    asyncio.run(body())


def test_slow_subscriber_drops_oldest_not_publisher() -> None:
    async def body() -> None:
        bus = EventBus(history=0, subscriber_queue=2)
        sub = bus.subscribe(EventChannel.FEED, replay_history=False)
        agen = sub.__aiter__()
        consume = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)
        # Publishing far past the queue capacity must never block the publisher.
        for i in range(5):
            bus.publish(EventChannel.FEED, _feed(i))
        first = await asyncio.wait_for(consume, timeout=1.0)
        # Oldest events were dropped, so the survivors are the most recent two.
        assert first.data["tokens"] == 3
        await sub.aclose()

    asyncio.run(body())
