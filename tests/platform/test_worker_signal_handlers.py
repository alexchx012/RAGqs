from __future__ import annotations

import signal

from app.platform.worker import install_stop_signal_handlers, restore_signal_handlers


def _tracked_signals() -> list[int]:
    return [
        number
        for number in (getattr(signal, name, None) for name in ("SIGINT", "SIGTERM"))
        if number is not None
    ]


def test_stop_signal_handlers_install_and_restore() -> None:
    previous = {number: signal.getsignal(number) for number in _tracked_signals()}

    stop_event, installed = install_stop_signal_handlers()
    try:
        assert not stop_event.is_set()
        assert set(installed) == set(previous)
        for number in installed:
            assert signal.getsignal(number) is not previous[number]
    finally:
        restore_signal_handlers(installed)

    for number, handler in previous.items():
        assert signal.getsignal(number) is handler


def test_installed_handlers_set_the_stop_event() -> None:
    stop_event, installed = install_stop_signal_handlers()
    try:
        number = next(iter(installed))
        handler = signal.getsignal(number)
        assert callable(handler)
        handler(number, None)
        assert stop_event.is_set()
    finally:
        restore_signal_handlers(installed)
