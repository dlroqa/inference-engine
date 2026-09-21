"""Unit tests for the Block 7 admission scheduler."""

from __future__ import annotations

import asyncio

import pytest

from engine.inference.scheduler import Scheduler, SchedulerSaturated


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        Scheduler(max_concurrency=0)
    with pytest.raises(ValueError):
        Scheduler(max_queue_depth=-1)
    with pytest.raises(ValueError):
        Scheduler(queue_timeout_s=-1)


def test_immediate_admission_when_slot_free() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1)
        lease = await sched.admit()
        assert sched.in_use == 1
        assert lease.waited_s == 0.0
        snap = sched.snapshot()
        assert snap["admitted_total"] == 1
        assert snap["available"] == 0
        lease.release()
        assert sched.in_use == 0
        assert sched.snapshot()["available"] == 1

    asyncio.run(body())


def test_double_release_is_idempotent() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1)
        lease = await sched.admit()
        lease.release()
        lease.release()  # no underflow, no extra permit
        assert sched.in_use == 0
        # A fresh admission still works (the semaphore wasn't over-released).
        lease2 = await sched.admit()
        assert sched.in_use == 1
        lease2.release()

    asyncio.run(body())


def test_queue_full_rejects_immediately() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1, max_queue_depth=0)
        held = await sched.admit()
        with pytest.raises(SchedulerSaturated) as ei:
            await sched.admit()
        assert ei.value.reason == "queue_full"
        assert ei.value.retry_after_s >= 1
        assert sched.snapshot()["rejected_queue_full"] == 1
        held.release()

    asyncio.run(body())


def test_queue_timeout_rejects_after_wait() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1, max_queue_depth=4, queue_timeout_s=0.05)
        held = await sched.admit()
        with pytest.raises(SchedulerSaturated) as ei:
            await sched.admit()
        assert ei.value.reason == "queue_timeout"
        assert sched.snapshot()["rejected_timeout"] == 1
        # The waiter left the queue cleanly.
        assert sched.queue_depth == 0
        held.release()

    asyncio.run(body())


def test_queued_request_admitted_after_release() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1, max_queue_depth=4, queue_timeout_s=5.0)
        held = await sched.admit()

        waiter = asyncio.create_task(sched.admit())
        await asyncio.sleep(0.02)  # let it enter the queue
        assert sched.queue_depth == 1
        assert sched.snapshot()["peak_queue_depth"] == 1

        held.release()
        lease = await asyncio.wait_for(waiter, timeout=1.0)
        assert sched.in_use == 1
        assert lease.waited_s > 0.0
        lease.release()

    asyncio.run(body())


def test_cancellation_counted_and_frees_capacity() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1)
        lease = await sched.admit()
        lease.release(cancelled=True)
        snap = sched.snapshot()
        assert snap["cancelled_total"] == 1
        assert snap["available"] == 1

    asyncio.run(body())


def test_multiple_slots_run_concurrently() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=3, max_queue_depth=0)
        a = await sched.admit()
        b = await sched.admit()
        c = await sched.admit()
        assert sched.in_use == 3
        # A 4th overlapping request has no slot and no queue -> shed.
        with pytest.raises(SchedulerSaturated):
            await sched.admit()
        for lease in (a, b, c):
            lease.release()
        assert sched.snapshot()["peak_in_use"] == 3

    asyncio.run(body())


def test_slow_consumer_signal() -> None:
    async def body() -> None:
        sched = Scheduler(max_concurrency=1)
        sched.note_slow_consumer(3)
        sched.note_slow_consumer(0)  # ignored
        assert sched.snapshot()["slow_consumer_total"] == 1

    asyncio.run(body())
