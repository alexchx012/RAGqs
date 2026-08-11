from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService, CalendarLock, get_calendar_service
from app.usage.schema import business_calendar_version_table, usage_metadata


def make_service(timezone: str = "Asia/Shanghai"):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = SqlAlchemyDatabaseClock(engine)
    return BusinessCalendarService(engine, clock, timezone), engine


class _FixedClock:
    """Controlled clock: returns the current fixed instant; tests may advance it."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now_utc(self, connection: object | None = None) -> datetime:
        return self._now.astimezone(UTC)


class _BarrierClock:
    """Rendezvous clock: blocks until both racing callers have read the clock.

    仅第一次 now_utc 等待会合（此时表仍为空、无人插入），从而构造真实的首插
    竞争窗口，而非依赖线程调度偶然性；后续调用（如 SQLite 写锁触发的重试再次
    进入 lock_or_verify）直接返回固定时间、不再等待 barrier，避免 Barrier 已
    释放后单方等待导致 BrokenBarrierError。
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self._rendezvous_done = False

    def now_utc(self, connection: object | None = None) -> datetime:
        if not self._rendezvous_done:
            self._barrier.wait(timeout=10)
            self._rendezvous_done = True
        return datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def test_lock_is_single_row_with_fixed_id() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        first = service.lock_or_verify(connection)
        second = service.lock_or_verify(connection)
    assert first == second
    assert first.timezone == "Asia/Shanghai"
    with engine.connect() as connection:
        rows = connection.execute(select(business_calendar_version_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["id"] == "instance"


def test_period_and_reset_at_use_business_timezone_dst() -> None:
    service, engine = make_service("America/New_York")
    with engine.begin() as connection:
        lock = service.lock_or_verify(connection)
        march = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
        assert service.period_for(lock, march) == "2026-03"
        assert service.next_month_start_utc(lock, march) == datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
        april = datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
        assert service.period_for(lock, april) == "2026-04"
        assert service.next_month_start_utc(lock, april) == datetime(2026, 5, 1, 4, 0, tzinfo=UTC)


def test_month_start_utc_covers_dst_shift_and_year_boundary() -> None:
    service, engine = make_service("America/New_York")
    with engine.begin() as connection:
        lock = service.lock_or_verify(connection)
    # 同月 1 日 00:00 的 UTC 瞬间随 DST 偏移变化：3 月 EST（UTC-5）、4 月 EDT（UTC-4）
    assert service.month_start_utc(lock, datetime(2026, 3, 20, 12, 0, tzinfo=UTC)) == (
        datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    )
    assert service.month_start_utc(lock, datetime(2026, 4, 20, 12, 0, tzinfo=UTC)) == (
        datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
    )
    # 恰好位于当月 1 日 00:00 EST 的瞬间 → 月首即该瞬间
    assert service.month_start_utc(lock, datetime(2026, 3, 1, 5, 0, tzinfo=UTC)) == (
        datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    )
    # 年界：12 月末的 UTC 时刻在 +08 业务时区已进入次年 1 月 →
    # 月首 2027-01-01 00:00 CST 对应的 UTC 落在 2026 年
    shanghai, shanghai_engine = make_service("Asia/Shanghai")
    with shanghai_engine.begin() as connection:
        shanghai_lock = shanghai.lock_or_verify(connection)
    assert shanghai.month_start_utc(
        shanghai_lock, datetime(2026, 12, 31, 18, 0, tzinfo=UTC)
    ) == datetime(2026, 12, 31, 16, 0, tzinfo=UTC)


def test_lock_timestamps_come_from_clock_utc_aware_and_persist_identically() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    fixed = datetime(2026, 7, 15, 8, 30, 0, tzinfo=UTC)
    service = BusinessCalendarService(engine, _FixedClock(fixed), "Asia/Shanghai")
    with engine.begin() as connection:
        lock = service.lock_or_verify(connection)
    # 返回给调用方的锁时间必须是同一受控时钟的 UTC-aware 值
    assert lock.effective_from_utc == fixed
    assert lock.effective_from_utc.tzinfo is UTC
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    business_calendar_version_table.c.version_id,
                    business_calendar_version_table.c.timezone,
                    business_calendar_version_table.c.effective_from_utc,
                    business_calendar_version_table.c.created_at_utc,
                )
            )
            .mappings()
            .one()
        )
    # SQLite 回读为 naive UTC（方言行为），值与受控时钟必须一致
    assert row["effective_from_utc"].replace(tzinfo=UTC) == fixed
    assert row["created_at_utc"].replace(tzinfo=UTC) == fixed
    assert row["version_id"] == lock.version_id
    assert row["timezone"] == "Asia/Shanghai"


def test_loser_lock_returns_winner_stored_timestamp() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = _FixedClock(datetime(2026, 7, 1, 8, 0, tzinfo=UTC))
    service = BusinessCalendarService(engine, clock, "Asia/Shanghai")
    with engine.begin() as connection:
        winner = service.lock_or_verify(connection)
    clock._now = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    with engine.begin() as connection:
        loser = service.lock_or_verify(connection)
    # 输方（冲突路径）必须返回赢方存储的版本与时间，而非自身的 now
    assert loser == winner
    assert loser.effective_from_utc == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    assert loser.effective_from_utc.tzinfo is UTC


def test_timezone_mismatch_after_lock_is_rejected() -> None:
    service, engine = make_service("Asia/Shanghai")
    with engine.begin() as connection:
        service.lock_or_verify(connection)
    other = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "UTC")
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as exc:
            other.lock_or_verify(connection)
    assert exc.value.code == "calendar_timezone_conflict"
    assert exc.value.status_code == 503


def test_concurrent_first_lock_from_two_engines_keeps_single_row(tmp_path) -> None:
    """C2 真实并发：空表起，两线程在 INSERT 前经 BarrierClock 会合。

    双方都成功且 version/timezone/effective_from 一致，最终恰一行。
    SQLite 写锁竞争由 busy timeout + 仅对可证明的 database locked 做有界重试吸收；
    其余异常直接抛出，不吞。
    """
    from sqlalchemy import create_engine as _create_engine

    url = f"sqlite:///{tmp_path / 'cal_concurrent.sqlite3'}"
    engine_a = _create_engine(url, connect_args={"timeout": 30})
    engine_b = _create_engine(url, connect_args={"timeout": 30})
    usage_metadata.create_all(engine_a)
    with engine_a.connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
    try:
        barrier = threading.Barrier(2)
        service_a = BusinessCalendarService(engine_a, _BarrierClock(barrier), "Asia/Shanghai")
        service_b = BusinessCalendarService(engine_b, _BarrierClock(barrier), "Asia/Shanghai")
        results: list[CalendarLock | None] = [None, None]
        errors: list[BaseException] = []

        def run(index: int, service: BusinessCalendarService, engine: Engine) -> None:
            deadline = time.monotonic() + 20.0
            try:
                while True:
                    try:
                        with engine.begin() as connection:
                            results[index] = service.lock_or_verify(connection)
                        return
                    except OperationalError as exc:
                        if "locked" not in str(exc).lower():
                            raise
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
            except BaseException as exc:  # pragma: no cover - 失败时暴露原因
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(0, service_a, engine_a)),
            threading.Thread(target=run, args=(1, service_b, engine_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not threads[0].is_alive() and not threads[1].is_alive()
        assert errors == []
        assert results[0] is not None and results[1] is not None
        assert results[0] == results[1]
        assert results[0].timezone == "Asia/Shanghai"
        with engine_a.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM business_calendar_version")
            ).scalar_one()
        assert count == 1
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_barrier_clock_waits_only_once_and_retry_is_viable() -> None:
    """BarrierClock 首次调用会合后，重试路径（再次 lock_or_verify）不再等待。

    第一阶段：工作线程在 lock_or_verify 的 now_utc 处阻塞等待 barrier（此时
    barrier 尚未释放，is_alive 断言证明首次调用确实等待会合）；主线程调用一次
    now_utc 放行，工作线程完成首插。第二阶段：barrier 已消耗，再次
    lock_or_verify 的 now_utc 必须立即返回，不得再次等待 barrier（否则会抛
    BrokenBarrierError）——该路径等价于 SQLite 写锁重试再次进入 lock_or_verify。
    全程只有工作线程使用 engine 连接，避免共享 StaticPool 连接上的并发 begin。
    """
    barrier = threading.Barrier(2)
    clock = _BarrierClock(barrier)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    service = BusinessCalendarService(engine, clock, "Asia/Shanghai")
    holder: dict[str, CalendarLock | None] = {"lock": None}
    errors: list[BaseException] = []

    def first_lock() -> None:
        try:
            with engine.begin() as connection:
                holder["lock"] = service.lock_or_verify(connection)
        except BaseException as exc:  # pragma: no cover - 失败时暴露原因
            errors.append(exc)

    thread = threading.Thread(target=first_lock)
    thread.start()
    # 等待工作线程进入 now_utc 的 barrier 等待；若 now_utc 不再等待会合，
    # lock_or_verify 会立即完成，此断言将失败。
    time.sleep(0.2)
    assert thread.is_alive()
    # 主线程放行：会合完成，工作线程继续插入并提交。
    clock.now_utc()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert holder["lock"] is not None

    # 重试路径：barrier 已消耗，第二次 lock_or_verify 不再等待
    with engine.begin() as connection:
        again = service.lock_or_verify(connection)
    assert again == holder["lock"]


def test_calendar_factory_does_not_reuse_a_service_with_a_different_clock() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    early = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    late = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    first = get_calendar_service(engine, _FixedClock(early), "Asia/Shanghai")
    second = get_calendar_service(engine, _FixedClock(late), "Asia/Shanghai")

    assert first is not second
    with engine.begin() as connection:
        lock = second.lock_or_verify(connection)
    assert lock.effective_from_utc == late
