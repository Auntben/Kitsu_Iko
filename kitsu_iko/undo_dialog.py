"""Upload history with undo."""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .history import CommentRecord, UploadJournal, inspect, undo_record
from .workers import run_async

GREY = QBrush(QColor(140, 140, 140))
GREEN = QBrush(QColor(46, 160, 67))
RED = QBrush(QColor(229, 83, 75))


class _UndoSignals(QObject):
    one_done = pyqtSignal(str, bool, str)  # comment_id, ok, note


class UndoDialog(QDialog):
    undone = pyqtSignal(list)  # comment ids that were removed from Kitsu

    def __init__(self, journal: UploadJournal, service, status_name, parent=None):
        super().__init__(parent)
        self.journal = journal
        self.service = service
        self.status_name = status_name
        self.busy = False
        self.setWindowTitle("Upload history")
        self.resize(980, 520)

        self.batch_list = QListWidget()
        self.batch_list.currentRowChanged.connect(self._show_batch)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Shot / Asset", "Status change", "Files", "State"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.undo_btn = QPushButton("Undo this upload...")
        self.undo_btn.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.undo_btn.clicked.connect(self._undo)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("<b>Uploads</b> (newest first)"))
        ll.addWidget(self.batch_list)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("<b>What it posted</b>"))
        rl.addWidget(self.tree)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([330, 650])

        hint = QLabel("Undo deletes the comments an upload posted (their revisions and attachments go with "
                      "them) and puts each task's status back. Tasks created by an upload are kept.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        bottom = QHBoxLayout()
        bottom.addWidget(self.info, 1)
        bottom.addWidget(self.undo_btn)
        bottom.addWidget(close_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

        self.signals = _UndoSignals()
        self.signals.one_done.connect(self._one_done)
        self._fill_batches()

    # -- display ------------------------------------------------------------

    def _batches(self):
        return self.journal.recent()

    def _fill_batches(self, keep_row: int = 0) -> None:
        self.batch_list.clear()
        for b in self._batches():
            recs = self.journal.batch_records(b)
            n_files = sum(len(r.files) for r in recs)
            pending = len(self.journal.pending_undo(b))
            when = b.created_at.replace("T", " ")[:16]
            state = "" if pending == len(recs) else ("  (undone)" if pending == 0 else "  (partly undone)")
            item = QListWidgetItem(f"{when}  ·  {b.context}\n{len(recs)} comment(s), {n_files} file(s){state}")
            if pending == 0:
                item.setForeground(GREY)
            self.batch_list.addItem(item)
        if self.batch_list.count():
            self.batch_list.setCurrentRow(min(keep_row, self.batch_list.count() - 1))
        else:
            self.info.setText("No uploads recorded yet.")
            self.undo_btn.setEnabled(False)

    def _current(self):
        row = self.batch_list.currentRow()
        batches = self._batches()
        return batches[row] if 0 <= row < len(batches) else None

    def _show_batch(self, *_):
        self.tree.clear()
        b = self._current()
        if b is None:
            return
        for r in self.journal.batch_records(b):
            change = f"{self.status_name(r.prev_status_id)} → {self.status_name(r.new_status_id)}"
            if r.undone:
                state = f"undone: {r.undo_note}"
            else:
                state = "posted" + ("  (task created)" if r.task_created else "")
            item = QTreeWidgetItem([r.entity_label, change, ", ".join(r.files), state])
            item.setData(0, Qt.ItemDataRole.UserRole, r.comment_id)
            if r.undone:
                for c in range(4):
                    item.setForeground(c, GREY)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        pending = self.journal.pending_undo(b)
        self.undo_btn.setEnabled(bool(pending) and not self.busy)
        self.info.setText("" if pending else "This upload has been undone.")

    # -- undo -----------------------------------------------------------------

    def _undo(self) -> None:
        b = self._current()
        if b is None:
            return
        # Newest first, so several comments on one task unwind in order.
        recs: list[CommentRecord] = list(reversed(self.journal.pending_undo(b)))
        self._set_busy(True, "Checking Kitsu...")
        run_async(lambda: inspect(self.service, recs),
                  lambda checks: self._confirm(b, recs, checks),
                  lambda msg: self._set_busy(False, f"Couldn't check Kitsu: {msg}"))

    def _confirm(self, batch, recs, checks) -> None:
        self._set_busy(False, "")
        by_id = {c.comment_id: c for c in checks}
        live = [r for r in recs if by_id[r.comment_id].exists]
        gone = len(recs) - len(live)
        n_files = sum(len(r.files) for r in live)
        replies = sum(by_id[r.comment_id].replies for r in live)
        newer = [r for r in live if by_id[r.comment_id].newer_foreign]
        text = (f"Delete {len(live)} comment(s) from Kitsu, with the {n_files} file(s) they carry, "
                f"and put task statuses back?\n\nThis can't be undone.")
        notes = []
        if replies:
            notes.append(f"• {replies} repl(ies) to these comments will be deleted too.")
        if newer:
            names = ", ".join(r.entity_label for r in newer[:5]) + (" ..." if len(newer) > 5 else "")
            notes.append(f"• Someone commented after this upload on {len(newer)} task(s) ({names}). "
                         "Our comments are still deleted, but those tasks keep their current status.")
        if gone:
            notes.append(f"• {gone} comment(s) were already deleted in Kitsu.")
        if any(r.task_created for r in live):
            notes.append("• Tasks this upload created will be kept (empty).")
        if notes:
            text += "\n\n" + "\n".join(notes)
        box = QMessageBox(QMessageBox.Icon.Warning, "Undo upload", text, parent=self)
        yes = box.addButton(f"Delete {len(live)} comment(s)" if live else "Mark as undone",
                            QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._run_undo(recs)

    def _run_undo(self, recs: list[CommentRecord]) -> None:
        self._set_busy(True, f"Undoing {len(recs)} comment(s)...")
        self._removed: list[str] = []
        self._failed = 0
        svc, sig, status_name = self.service, self.signals, self.status_name

        def work():
            remaining = {r.comment_id for r in recs}
            for r in recs:
                remaining.discard(r.comment_id)
                try:
                    note = undo_record(svc, r, remaining, status_name)
                    sig.one_done.emit(r.comment_id, True, note)
                except Exception as exc:
                    sig.one_done.emit(r.comment_id, False, str(exc))

        run_async(work, lambda _: self._finished(), lambda msg: self._finished(msg))

    def _one_done(self, comment_id: str, ok: bool, note: str) -> None:
        if ok:
            self.journal.mark_undone(comment_id, note)
            self._removed.append(comment_id)
        else:
            self._failed += 1
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == comment_id:
                it.setText(3, f"undone: {note}" if ok else f"FAILED: {note}")
                for c in range(4):
                    it.setForeground(c, GREY if ok else RED)

    def _finished(self, err: str | None = None) -> None:
        row = self.batch_list.currentRow()
        msg = f"Undo finished: {len(self._removed)} comment(s) removed."
        if self._failed:
            msg += f" {self._failed} failed (see the State column; your Kitsu role may not allow deleting)."
        if err:
            msg += f" Error: {err}"
        self._set_busy(False, msg)
        self._fill_batches(row)
        self.info.setText(msg)
        if self._removed:
            self.undone.emit(list(self._removed))

    def _set_busy(self, busy: bool, msg: str) -> None:
        self.busy = busy
        self.undo_btn.setEnabled(not busy and self._current() is not None
                                 and bool(self.journal.pending_undo(self._current())))
        self.batch_list.setEnabled(not busy)
        if msg:
            self.info.setText(msg)
