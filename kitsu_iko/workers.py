"""Background execution helpers. Network calls never run on the UI thread."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from .service import describe_error

# Uploads make the server transcode; keep concurrency modest.
TRANSFER_POOL = QThreadPool()
TRANSFER_POOL.setMaxThreadCount(3)


class _Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str, object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # reported to the UI, never swallowed
            traceback.print_exc()
            self.signals.failed.emit(describe_error(exc), exc)
        else:
            self.signals.done.emit(result)


_live: set[_Task] = set()


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    pool: QThreadPool | None = None,
) -> None:
    """Run ``fn`` on a worker thread; callbacks fire on the UI thread."""
    task = _Task(fn)
    task.setAutoDelete(False)
    _live.add(task)

    def finish(*_):
        _live.discard(task)

    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(lambda msg, exc: on_error(msg))
    task.signals.done.connect(finish)
    task.signals.failed.connect(finish)
    (pool or QThreadPool.globalInstance()).start(task)


class Progress(QObject):
    """Thread-safe progress relay: workers call ``emit_*`` from any thread,
    slots run on the UI thread."""

    progress = pyqtSignal(int, float)  # row_id, fraction 0..1
    file_done = pyqtSignal(int, str, str)  # row_id, comment_id, preview_id
    state = pyqtSignal(int, str, str)  # row_id, state, message
    record = pyqtSignal(object)  # dict for the undo journal (a comment was posted)
