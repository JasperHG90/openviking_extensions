"""The lock, and the refusal to run without one.

The Postgres implementation is proven against a real database in
``test_integration.py`` — a lock tested against a fake proves only that the
fake agrees with itself. What is here is the part that needs no database: the
in-process lock, the key derivation, and the rule that there is no way to sweep
unlocked by omission.
"""

from __future__ import annotations

import asyncio

import pytest

from ov_ext.reflect.config import ENV_PREFIX, LockKind, ReflectSettings
from ov_ext.reflect.locks import (
    LOCK_NAMESPACE,
    PostgresAdvisoryLock,
    ProcessLock,
    advisory_key,
    build_lock,
)

pytestmark = pytest.mark.usefixtures("clean_env")


def settings(**overrides: object) -> ReflectSettings:
    """Settings built without consulting the environment."""
    base = ReflectSettings().model_dump()
    base.update({"enabled": True, "user_id": "jasper"})
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


async def test_one_holder_at_a_time_in_process() -> None:
    lock = ProcessLock()
    async with lock.acquire() as first:
        assert first is True
        async with lock.acquire() as second:
            assert second is False


async def test_the_lock_is_released_when_the_block_ends() -> None:
    lock = ProcessLock()
    async with lock.acquire() as held:
        assert held is True
    async with lock.acquire() as again:
        assert again is True


async def test_the_lock_is_released_when_the_block_raises() -> None:
    """A sweep that blows up must not wedge reflection for the process lifetime."""
    lock = ProcessLock()
    with pytest.raises(RuntimeError):
        async with lock.acquire() as held:
            assert held is True
            raise RuntimeError("sweep exploded")
    async with lock.acquire() as again:
        assert again is True


async def test_a_second_task_skips_rather_than_waiting() -> None:
    """A follower must not queue up behind the leader.

    Waiting would mean every skipped tick eventually ran, so a slow sweep would
    be followed by a stampede of the ticks that piled up behind it.
    """
    lock = ProcessLock()
    started = asyncio.Event()
    release = asyncio.Event()
    outcomes: list[bool] = []

    async def hold() -> None:
        async with lock.acquire() as held:
            outcomes.append(held)
            started.set()
            await release.wait()

    async def contend() -> None:
        await started.wait()
        async with lock.acquire() as held:
            outcomes.append(held)

    holder = asyncio.create_task(hold())
    contender = asyncio.create_task(contend())
    await asyncio.wait_for(contender, timeout=1)
    release.set()
    await holder

    assert outcomes == [True, False]


def test_the_advisory_key_is_stable_and_fits_a_signed_bigint() -> None:
    """It is a Postgres bigint, and it must not move between releases."""
    key = advisory_key()
    assert advisory_key(LOCK_NAMESPACE) == key
    assert -(2**63) <= key < 2**63


def test_a_different_namespace_gets_a_different_key() -> None:
    assert advisory_key("something.else") != advisory_key()


def test_build_lock_refuses_when_no_lock_was_chosen() -> None:
    """The last mile of the guarantee: no fallback, so none can be inherited."""
    with pytest.raises(ValueError, match=f"{ENV_PREFIX}LOCK is not set"):
        build_lock(settings(lock=LockKind.UNSET))


def test_build_lock_returns_what_was_asked_for() -> None:
    assert isinstance(build_lock(settings(lock=LockKind.PROCESS)), ProcessLock)
    postgres = build_lock(
        settings(lock=LockKind.POSTGRES, lock_dsn="postgresql://x/y")
    )
    assert isinstance(postgres, PostgresAdvisoryLock)


def test_a_postgres_lock_without_a_dsn_is_refused_at_construction() -> None:
    """Caught at startup, not at the first tick fifteen minutes later."""
    with pytest.raises(ValueError, match="LOCK_DSN"):
        ReflectSettings(
            enabled=True, user_id="jasper", lock=LockKind.POSTGRES, lock_dsn=""
        )


def test_enabling_reflection_without_a_user_is_refused() -> None:
    """Reflection serves no request, so it cannot infer whose memories to read."""
    with pytest.raises(ValueError, match="USER_ID"):
        ReflectSettings(enabled=True, user_id="")


def test_the_lock_setting_has_no_default() -> None:
    """A default would be a way to sweep unlocked by forgetting a setting."""
    assert ReflectSettings().lock is LockKind.UNSET
