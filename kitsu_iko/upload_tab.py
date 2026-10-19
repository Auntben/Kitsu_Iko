"""Upload tab: drop files, auto-map them to shots/assets, fix the mapping,
set status/comment/mode in bulk with per-file exceptions, review, upload."""

from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QSplitter, QStyle, QStyledItemDelegate,
    QStyleOptionProgressBar, QTableView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import matcher
from .history import CommentRecord, UploadJournal, default_path
from .models import (
    ATTACHMENT, DONE, ERROR, PENDING, QUEUED, REVISION, RUNNING, UploadDefaults,
    UploadGroup, UploadItem, UploadPlan, mode_counts, plan_upload,
)
from .undo_dialog import UndoDialog
from .service import KitsuService, PublishError, PublishFile
from .widgets import ContextBar, StatusCombo, color_icon, human_size, natural_key, settings
from .workers import TRANSFER_POOL, Progress, run_async

IGNORED_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}
ITEM_ROLE = Qt.ItemDataRole.UserRole + 1

# Result comes first so it stays visible however wide the table gets; it
# shows an icon and one word, with the full message in the tooltip.
C_RESULT, C_FILE, C_ENTITY, C_MATCH, C_CURRENT, C_MODE, C_STATUS, C_COMMENT, C_SIZE = range(9)
HEADERS = ["Result", "File", "Shot / Asset", "Match", "Current", "Upload as", "New status", "Comment", "Size"]

GREY = QBrush(QColor(140, 140, 140))
RED_BG = QBrush(QColor(229, 83, 75, 45))
# Upload mode colours, used on the toggle, the table cells and the review.
REV_COLOR, ATT_COLOR = "#2f6fdb", "#d97706"
MODE_BG = {REVISION: QBrush(QColor(47, 111, 219, 38)), ATTACHMENT: QBrush(QColor(217, 119, 6, 70))}
MODE_TOGGLE_CSS = f"""
QPushButton {{ padding: 5px 16px; font-weight: bold; border: 1px solid #999; background: palette(button); }}
QPushButton#rev {{ border-top-left-radius: 4px; border-bottom-left-radius: 4px; }}
QPushButton#att {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px; }}
QPushButton#rev:checked {{ background: {REV_COLOR}; color: white; border-color: {REV_COLOR}; }}
QPushButton#att:checked {{ background: {ATT_COLOR}; color: white; border-color: {ATT_COLOR}; }}
"""
GREEN = QBrush(QColor(46, 160, 67))
RED = QBrush(QColor(229, 83, 75))


_RESULT_ICONS: dict = {}


def result_icon(kind: str):
    """Standard style icons, created lazily (needs a QApplication)."""
    if not _RESULT_ICONS:
        sp = QStyle.StandardPixmap
        style = QApplication.style()
        _RESULT_ICONS.update({
            DONE: style.standardIcon(sp.SP_DialogApplyButton),
            ERROR: style.standardIcon(sp.SP_MessageBoxCritical),
            "blocked": style.standardIcon(sp.SP_MessageBoxWarning),
            QUEUED: style.standardIcon(sp.SP_BrowserReload),
        })
    return _RESULT_ICONS.get(kind)


def entity_label(e: dict, show_episode: bool = False) -> str:
    parts = [e.get("episode_name") if show_episode else "",
             e.get("sequence_name") or e.get("asset_type_name"), e["name"]]
    return " / ".join(p for p in parts if p)


def expand_paths(paths: list[str]) -> list[str]:
    """Files as-is; folders recursively. Hidden/system files are skipped."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if not f.startswith(".") and f.lower() not in IGNORED_FILES:
                        out.append(os.path.join(root, f))
        elif os.path.isfile(p):
            out.append(p)
    return out


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


class UploadModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[UploadItem] = []
        self.defaults = UploadDefaults()
        self.entities: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.status_by_id: dict[str, dict] = {}
        self.blocked: dict[int, str] = {}
        self.locked = False
        self.show_episode = False  # True when browsing "All episodes"
        self._next_id = 1

    # -- helpers ----------------------------------------------------------

    def entity_label(self, entity_id: str | None) -> str:
        e = self.entities.get(entity_id or "")
        return entity_label(e, self.show_episode) if e else ""

    def status_label(self, status_id: str | None) -> str:
        s = self.status_by_id.get(status_id or "")
        return s.get("short_name", "").upper() if s else ""

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def row_of(self, row_id: int) -> int:
        for i, it in enumerate(self.items):
            if it.row_id == row_id:
                return i
        return -1

    def item_by_id(self, row_id: int) -> UploadItem | None:
        r = self.row_of(row_id)
        return self.items[r] if r >= 0 else None

    def refresh(self, row: int | None = None) -> None:
        if not self.items:
            return
        if row is None:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(HEADERS) - 1))
        else:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))

    def add(self, items: list[UploadItem]) -> None:
        if not items:
            return
        self.beginInsertRows(QModelIndex(), len(self.items), len(self.items) + len(items) - 1)
        self.items.extend(items)
        self.endInsertRows()

    def remove_rows(self, rows: list[int]) -> None:
        for r in sorted(set(rows), reverse=True):
            self.beginRemoveRows(QModelIndex(), r, r)
            del self.items[r]
            self.endRemoveRows()

    # -- Qt model API -----------------------------------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        it = self.items[index.row()]
        if self.locked or it.state in (RUNNING, QUEUED, DONE):
            return f
        if index.column() == C_FILE:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() in (C_ENTITY, C_MODE, C_STATUS, C_COMMENT):
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        d = self.defaults
        if role == ITEM_ROLE:
            return it
        if role == Qt.ItemDataRole.CheckStateRole and col == C_FILE:
            return Qt.CheckState.Checked if it.include else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == C_FILE:
                return it.name
            if col == C_ENTITY:
                return self.entity_label(it.entity_id) or "-"
            if col == C_MATCH:
                if not it.entity_id:
                    return it.match_reason
                return "manual" if it.manual else f"auto {min(int(it.match_score * 100), 100)}%"
            if col == C_CURRENT:
                task = self.tasks.get(it.entity_id or "")
                if it.entity_id and not task:
                    return "no task"
                return self.status_label(task.get("task_status_id")) if task else ""
            if col == C_MODE:
                return it.eff_mode(d).capitalize()
            if col == C_STATUS:
                return self.status_label(it.eff_status(d)) or "-"
            if col == C_COMMENT:
                return it.eff_comment(d, (self.entities.get(it.entity_id or "") or {}).get("name", ""))
            if col == C_SIZE:
                return human_size(it.size)
            if col == C_RESULT:
                if it.state == RUNNING:
                    return f"{int(it.progress * 100)}%"
                if it.state in (QUEUED, DONE, ERROR):
                    return {QUEUED: "Queued", DONE: "Done", ERROR: "Failed"}[it.state]
                if not it.include:
                    return "Skipped"
                if it.row_id in self.blocked:
                    return "Blocked"
                return "Ready" if it.entity_id else ""
        if role == Qt.ItemDataRole.DecorationRole:
            if col == C_RESULT:
                if it.state in (QUEUED, DONE, ERROR):
                    return result_icon(it.state)
                if it.include and it.row_id in self.blocked:
                    return result_icon("blocked")
                return None
            if col == C_STATUS:
                s = self.status_by_id.get(it.eff_status(d) or "")
                return color_icon(s.get("color")) if s else None
            if col == C_CURRENT:
                task = self.tasks.get(it.entity_id or "")
                s = self.status_by_id.get((task or {}).get("task_status_id", ""))
                return color_icon(s.get("color")) if s else None
        if role == Qt.ItemDataRole.FontRole:
            overridden = {C_MODE: it.mode, C_STATUS: it.status_id, C_COMMENT: it.comment}
            if col in overridden:
                font = QFont()
                if overridden[col] is None:
                    font.setItalic(True)
                else:
                    font.setBold(True)
                return font
        if role == Qt.ItemDataRole.ForegroundRole:
            if col in (C_MODE, C_STATUS, C_COMMENT):
                over = {C_MODE: it.mode, C_STATUS: it.status_id, C_COMMENT: it.comment}[col]
                return GREY if over is None else None
            if col == C_RESULT:
                if it.state in (DONE, ERROR):
                    return {DONE: GREEN, ERROR: RED}[it.state]
                if it.state == PENDING and (not it.include or it.row_id not in self.blocked):
                    return GREY
                return RED if it.row_id in self.blocked else None
            if col == C_CURRENT and it.entity_id and it.entity_id not in self.tasks:
                return RED
            if col == C_MATCH and not it.entity_id:
                return GREY
        if role == Qt.ItemDataRole.BackgroundRole:
            if it.state == ERROR or (it.row_id in self.blocked and it.include and it.state != DONE):
                return RED_BG
            if col == C_MODE:
                return MODE_BG[it.eff_mode(d)]
        if role == Qt.ItemDataRole.ToolTipRole:
            if col in (C_MODE, C_STATUS, C_COMMENT):
                over = {C_MODE: it.mode, C_STATUS: it.status_id, C_COMMENT: it.comment}[col]
                return ("Using the default from the top bar. Edit to override for this file."
                        if over is None else "Overridden for this file. Right-click > Reset to default.")
            if col == C_FILE:
                return it.path
            if col == C_MATCH:
                return it.match_reason
            if col == C_RESULT:
                if it.state == ERROR:
                    return f"Upload failed: {it.message}\nFix the cause if needed, then click Retry."
                if it.state == DONE:
                    return "Uploaded to Kitsu."
                if it.state == QUEUED:
                    return "Waiting for a free upload slot."
                if it.state == RUNNING:
                    return "Uploading..."
                if not it.include:
                    return "Excluded: tick the checkbox to include it."
                if it.row_id in self.blocked:
                    return f"Can't upload: {self.blocked[it.row_id]}"
                return "Will be uploaded." if it.entity_id else ""
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == C_FILE:
            it.include = Qt.CheckState(value) == Qt.CheckState.Checked
        elif role == Qt.ItemDataRole.EditRole:
            if col == C_ENTITY:
                it.entity_id = value or None
                it.manual = True
                it.match_reason = "Assigned manually" if value else "Moved to pool"
            elif col == C_MODE:
                it.mode = value
            elif col == C_STATUS:
                it.status_id = value
            elif col == C_COMMENT:
                it.comment = value
            else:
                return False
        else:
            return False
        if it.state == ERROR:
            it.state, it.message = PENDING, ""
        self.refresh(index.row())
        return True


class AssignedFilter(QSortFilterProxyModel):
    def __init__(self, assigned: bool, parent=None):
        super().__init__(parent)
        self.assigned = assigned

    def filterAcceptsRow(self, source_row, source_parent):
        it = self.sourceModel().items[source_row]
        return (it.entity_id is not None) == self.assigned

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not self.assigned and role == Qt.ItemDataRole.CheckStateRole:
            return None  # no include/exclude checkbox in the pool
        return super().data(index, role)


# --------------------------------------------------------------------------
# Delegates
# --------------------------------------------------------------------------


class EntityDelegate(QStyledItemDelegate):
    """Searchable combo of all shots/assets in the current context."""

    def __init__(self, model: UploadModel, parent=None):
        super().__init__(parent)
        self.model = model

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.setEditable(True)
        cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        it: UploadItem = index.data(ITEM_ROLE)
        cb.addItem("(move to unassigned pool)", "")
        suggested = sorted((c.entity_id for c in it.candidates if c.entity_id in self.model.entities),
                           key=lambda e: natural_key(self.model.entity_label(e)))
        for eid in suggested:
            cb.addItem("★ " + self.model.entity_label(eid), eid)
        for eid in sorted(self.model.entities, key=lambda e: natural_key(self.model.entity_label(e))):
            if eid not in suggested:
                cb.addItem(self.model.entity_label(eid), eid)
        comp = cb.completer()
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return cb

    def setEditorData(self, editor, index):
        it: UploadItem = index.data(ITEM_ROLE)
        i = editor.findData(it.entity_id or "")
        editor.setCurrentIndex(max(i, 0))
        editor.lineEdit().selectAll()

    def setModelData(self, editor, model, index):
        i = editor.findText(editor.currentText(), Qt.MatchFlag.MatchExactly)
        eid = editor.itemData(i) if i >= 0 else None
        if eid is None:
            return  # typed text didn't resolve to an entity: ignore
        model.setData(index, eid or None)


class ModeDelegate(QStyledItemDelegate):
    def __init__(self, model: UploadModel, parent=None):
        super().__init__(parent)
        self.model = model

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.addItem(f"Default ({self.model.defaults.mode.capitalize()})", None)
        cb.addItem("Revision", REVISION)
        cb.addItem("Attachment", ATTACHMENT)
        return cb

    def setEditorData(self, editor, index):
        editor.setCurrentIndex(max(editor.findData(index.data(ITEM_ROLE).mode), 0))
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData())


class StatusDelegate(QStyledItemDelegate):
    def __init__(self, model: UploadModel, allowed: list, parent=None):
        super().__init__(parent)
        self.model = model
        self.allowed = allowed

    def createEditor(self, parent, option, index):
        d = self.model.status_label(self.model.defaults.status_id) or "none"
        cb = StatusCombo(parent, allow_default=f"Default ({d})")
        cb.set_statuses(self.allowed)
        return cb

    def setEditorData(self, editor, index):
        sid = index.data(ITEM_ROLE).status_id
        editor.setCurrentIndex(max(editor.findData(sid), 0) if sid else 0)
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData())


class CommentDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        ed = QLineEdit(parent)
        ed.setPlaceholderText("Override comment for this file")
        return ed

    def setEditorData(self, editor, index):
        it: UploadItem = index.data(ITEM_ROLE)
        editor.setText(it.comment if it.comment is not None else "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text())


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        it: UploadItem = index.data(ITEM_ROLE)
        if it.state != RUNNING:
            return super().paint(painter, option, index)
        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(2, 3, -2, -3)
        opt.minimum, opt.maximum = 0, 100
        opt.progress = int(it.progress * 100)
        # Uploads report all bytes read before the request completes (and
        # the server still has to store/transcode): say so instead of 100%.
        opt.text = "finishing..." if it.progress >= 0.999 else f"{opt.progress}%"
        opt.textVisible = True
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, opt, painter)


# --------------------------------------------------------------------------
# Review dialog
# --------------------------------------------------------------------------


def upload_label(n_rev: int, n_att: int) -> str:
    parts = []
    if n_rev:
        parts.append(f"{n_rev} revision{'s' if n_rev != 1 else ''}")
    if n_att:
        parts.append(f"{n_att} attachment{'s' if n_att != 1 else ''}")
    return "Upload " + " + ".join(parts) if parts else "Upload"


class ReviewDialog(QDialog):
    def __init__(self, plan: UploadPlan, model: UploadModel, ctx_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review upload")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        n_comments = len(plan.groups)
        head = QLabel(
            f"<b>{plan.file_count} file(s)</b> will be posted as <b>{n_comments} comment(s)</b> "
            f"on <b>{ctx_label}</b>."
            + (f"<br>{plan.tasks_to_create} missing task(s) will be created first." if plan.tasks_to_create else "")
        )
        head.setWordWrap(True)
        lay.addWidget(head)
        n_rev, n_att, n_media = mode_counts(plan, model.defaults)
        if n_att:
            warn = (f"📎 <b>{n_att} file(s) will be uploaded as ATTACHMENTS</b> on the comment. "
                    "They won't appear as revisions in Kitsu's review player.")
            if n_media:
                warn += (f"<br><b>{n_media} of them are video/image files</b> that would normally be "
                         "revisions. Cancel and switch them to Revision if that's not intended.")
            banner = QLabel(warn)
            banner.setWordWrap(True)
            banner.setStyleSheet(f"background: rgba(217, 119, 6, 55); border: 2px solid {ATT_COLOR}; "
                                 "padding: 8px; border-radius: 4px;")
            lay.addWidget(banner)

        tree = QTreeWidget()
        tree.setHeaderLabels(["Shot / Asset", "Status", "Comment", "Files"])
        tree.setRootIsDecorated(True)
        for g in plan.groups:
            pend = g.pending_items
            g_rev = sum(1 for i in pend if i.eff_mode(model.defaults) == REVISION)
            g_att = len(pend) - g_rev
            parts = []
            if g_rev:
                parts.append(f"{g_rev} revision file{'s' if g_rev > 1 else ''}"
                             + (f" (v{g.revision:03d})" if g.revision else ""))
            if g_att:
                parts.append(f"{g_att} attachment{'s' if g_att > 1 else ''}")
            label = model.entity_label(g.entity_id)
            if g.task_id is None:
                label += "  (create task)"
            if g.comment_id:
                label += "  (resume)"
            top = QTreeWidgetItem([label, model.status_label(g.status_id), g.text, ", ".join(parts)])
            s = model.status_by_id.get(g.status_id)
            if s:
                top.setIcon(1, color_icon(s.get("color")))
            for i in pend:
                mode = i.eff_mode(model.defaults)
                child = QTreeWidgetItem(top, ["", "", "", f"{i.name}  [{mode.upper()}]"])
                child.setForeground(3, QBrush(QColor(ATT_COLOR if mode == ATTACHMENT else REV_COLOR)))
            tree.addTopLevelItem(top)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tree.resizeColumnToContents(0)
        lay.addWidget(tree, 1)

        notes = list(plan.warnings)
        if plan.blocked:
            notes.append(f"{len(plan.blocked)} file(s) can't be uploaded and will be skipped "
                         "(highlighted red in the table).")
        if notes:
            box = QLabel("<b>Heads up</b><ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")
            box.setWordWrap(True)
            box.setStyleSheet("background: rgba(255, 196, 0, 40); padding: 6px; border-radius: 4px;")
            lay.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(upload_label(n_rev, n_att))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(plan.file_count > 0)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


# --------------------------------------------------------------------------
# Tab
# --------------------------------------------------------------------------


class UploadTab(QWidget):
    status_message = pyqtSignal(str)
    running_changed = pyqtSignal(bool)

    def __init__(self, service: KitsuService, parent=None):
        super().__init__(parent)
        self.service = service
        self.setAcceptDrops(True)
        self.ctx = None
        self.ctx_loading = False
        self.statuses: list[dict] = []
        self.allowed: list[dict] = []
        self.running_groups = 0
        self.model = UploadModel(self)
        self.progress = Progress()
        self.progress.progress.connect(self._on_progress)
        self.progress.file_done.connect(self._on_file_done)
        self.progress.state.connect(self._on_state)
        self.progress.record.connect(self._on_record)

        # Context
        self.context_bar = ContextBar(service, "upload")
        self.context_bar.context_changed.connect(self._on_context)
        self.context_bar.statuses_loaded.connect(self._on_statuses)
        self.context_bar.busy.connect(self.status_message)

        # Defaults
        defaults_box = QGroupBox("Defaults for every file  (edit a cell in the table to override one file)")
        g = QGridLayout(defaults_box)
        self.status_cb = StatusCombo()
        # A two-button colour toggle instead of radio buttons: blue revision,
        # amber attachment. Always starts on Revision (not remembered), since
        # a leftover Attachment setting is the easy accident.
        self.rev_rb = QPushButton("▶  Revision")
        self.rev_rb.setObjectName("rev")
        self.rev_rb.setToolTip("Published preview: playable in Kitsu's review player, gets a revision number.")
        self.att_rb = QPushButton("📎  Attachment")
        self.att_rb.setObjectName("att")
        self.att_rb.setToolTip("Attached to the comment as a plain file: no revision, not in the review player.")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_box = QWidget()
        mode_lay = QHBoxLayout(mode_box)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(0)
        for b in (self.rev_rb, self.att_rb):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mode_group.addButton(b)
            mode_lay.addWidget(b)
        mode_box.setStyleSheet(MODE_TOGGLE_CSS)
        self.rev_rb.setChecked(True)
        self.comment_ed = QLineEdit(settings().value("upload/comment", ""))
        self.comment_ed.setPlaceholderText("Comment text. Placeholders: {filename} {version} {entity}")
        self.combine_cb = QCheckBox("One comment per shot/asset")
        self.combine_cb.setToolTip("Files that go to the same task (with the same status and comment) "
                                   "are posted together as a single comment / revision.")
        self.combine_cb.setChecked(settings().value("upload/combine", True, type=bool))
        self.revnum_cb = QCheckBox("Revision number from filename (v###)")
        self.revnum_cb.setToolTip("Otherwise Kitsu picks the next revision number automatically.")
        self.revnum_cb.setChecked(settings().value("upload/revnum", False, type=bool))
        self.thumb_cb = QCheckBox("Set as thumbnail")
        self.thumb_cb.setChecked(settings().value("upload/thumb", False, type=bool))
        self.create_cb = QCheckBox("Create missing tasks")
        self.create_cb.setToolTip("If a shot/asset has no task of the selected type, create it.")
        g.addWidget(QLabel("Status"), 0, 0)
        g.addWidget(self.status_cb, 0, 1)
        g.addWidget(QLabel("Upload as"), 0, 2)
        g.addWidget(mode_box, 0, 3, 1, 2, Qt.AlignmentFlag.AlignLeft)
        g.addWidget(QLabel("Comment"), 1, 0)
        g.addWidget(self.comment_ed, 1, 1, 1, 4)
        opts = QHBoxLayout()
        for w in (self.combine_cb, self.revnum_cb, self.thumb_cb, self.create_cb):
            opts.addWidget(w)
        opts.addStretch(1)
        g.addLayout(opts, 2, 0, 1, 5)
        g.setColumnStretch(1, 1)
        for w in (self.rev_rb, self.att_rb, self.combine_cb, self.revnum_cb, self.thumb_cb, self.create_cb):
            w.toggled.connect(self._on_defaults)
        self.status_cb.currentIndexChanged.connect(self._on_defaults)
        self.comment_ed.textChanged.connect(self._on_defaults)

        # Mapped table
        self.mapped_proxy = AssignedFilter(True, self)
        self.mapped_proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.mapped_proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_COMMENT, QHeaderView.ResizeMode.Stretch)
        for col, w in ((C_RESULT, 95), (C_FILE, 250), (C_ENTITY, 120), (C_MATCH, 75), (C_CURRENT, 75),
                       (C_MODE, 85), (C_STATUS, 80), (C_SIZE, 65)):
            self.table.setColumnWidth(col, w)
        self.table.sortByColumn(C_FILE, Qt.SortOrder.AscendingOrder)
        self.table.setItemDelegateForColumn(C_ENTITY, EntityDelegate(self.model, self.table))
        self.table.setItemDelegateForColumn(C_MODE, ModeDelegate(self.model, self.table))
        self.status_delegate = StatusDelegate(self.model, self.allowed, self.table)
        self.table.setItemDelegateForColumn(C_STATUS, self.status_delegate)
        self.table.setItemDelegateForColumn(C_COMMENT, CommentDelegate(self.table))
        self.table.setItemDelegateForColumn(C_RESULT, ProgressDelegate(self.table))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        del_act = QAction("Remove", self.table)
        del_act.setShortcut(QKeySequence.StandardKey.Delete)
        # Scoped to its own list: two window-wide Delete shortcuts would
        # be ambiguous and Qt would fire neither.
        del_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        del_act.triggered.connect(lambda: self._remove(self._selected(self.table)))
        self.table.addAction(del_act)

        self.drop_hint = QLabel("Drop files or folders here\n\nor use  Add files...  /  Add folder...")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setStyleSheet("color: gray; font-size: 15px; border: 2px dashed #888; border-radius: 8px;")

        mapped_w = QWidget()
        ml = QVBoxLayout(mapped_w)
        ml.setContentsMargins(0, 0, 0, 0)
        self.mapped_lbl = QLabel("<b>Mapped files</b>")
        ml.addWidget(self.mapped_lbl)
        ml.addWidget(self.table, 1)
        ml.addWidget(self.drop_hint, 1)

        # Unassigned pool
        self.pool_proxy = AssignedFilter(False, self)
        self.pool_proxy.setSourceModel(self.model)
        self.pool = QTableView()
        self.pool.setModel(self.pool_proxy)
        self.pool.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pool.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pool.verticalHeader().setVisible(False)
        self.pool.setWordWrap(False)
        for col in range(len(HEADERS)):
            self.pool.setColumnHidden(col, col not in (C_FILE, C_MATCH))
        self.pool.horizontalHeader().setSectionResizeMode(C_FILE, QHeaderView.ResizeMode.Stretch)
        self.pool.setColumnWidth(C_MATCH, 150)
        self.pool.selectionModel().selectionChanged.connect(self._pool_selection)
        self.pool.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pool.customContextMenuRequested.connect(self._pool_menu)
        pool_del = QAction("Remove", self.pool)
        pool_del.setShortcut(QKeySequence.StandardKey.Delete)
        # Scoped to its own list: two window-wide Delete shortcuts would
        # be ambiguous and Qt would fire neither.
        pool_del.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        pool_del.triggered.connect(lambda: self._remove(self._selected(self.pool)))
        self.pool.addAction(pool_del)

        self.entity_search = QLineEdit()
        self.entity_search.setPlaceholderText("Search shots / assets...")
        self.entity_search.textChanged.connect(self._fill_entity_list)
        self.entity_list = QListWidget()
        # Deferred: assigning rebuilds this list, which must not happen while
        # Qt is still inside the list's own double-click handling.
        self.entity_list.itemDoubleClicked.connect(
            lambda *_: QTimer.singleShot(0, self._assign_from_pool))
        self.assign_btn = QPushButton("← Assign selected files")
        self.assign_btn.clicked.connect(self._assign_from_pool)

        pool_w = QWidget()
        pl = QVBoxLayout(pool_w)
        pl.setContentsMargins(0, 0, 0, 0)
        self.pool_lbl = QLabel("<b>Unassigned pool</b>")
        pl.addWidget(self.pool_lbl)
        pl.addWidget(self.pool, 3)
        pl.addWidget(QLabel("Assign to:  (★ = suggested)"))
        pl.addWidget(self.entity_search)
        pl.addWidget(self.entity_list, 2)
        pl.addWidget(self.assign_btn)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(mapped_w)
        splitter.addWidget(pool_w)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([900, 320])

        # Bottom bar
        self.add_files_btn = QPushButton("Add files...")
        self.add_files_btn.clicked.connect(self._add_files_dialog)
        self.add_dir_btn = QPushButton("Add folder...")
        self.add_dir_btn.clicked.connect(self._add_dir_dialog)
        self.rematch_btn = QPushButton("Re-run auto-match")
        self.rematch_btn.setToolTip("Re-match every file that wasn't assigned by hand.")
        self.rematch_btn.clicked.connect(lambda: self.rematch(force=False))
        self.clear_done_btn = QPushButton("Clear uploaded")
        self.clear_done_btn.clicked.connect(self._clear_done)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.clicked.connect(self._clear_all)
        self.undo_btn = QPushButton("↶ Undo upload...")
        self.undo_btn.setToolTip("See past uploads and reverse one: deletes the comments it posted "
                                 "and restores task statuses.")
        self.undo_btn.clicked.connect(self._open_history)
        self.undo_btn.setEnabled(False)
        self.journal: UploadJournal | None = None
        self.summary = QLabel()
        self.upload_btn = QPushButton("Review && upload...")
        self.upload_btn.setDefault(True)
        self.upload_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.upload_btn.clicked.connect(self._review)
        bottom = QHBoxLayout()
        for w in (self.add_files_btn, self.add_dir_btn, self.rematch_btn, self.clear_done_btn,
                  self.clear_btn, self.undo_btn):
            bottom.addWidget(w)
        bottom.addSpacing(12)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.upload_btn)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay = QVBoxLayout(self)
        lay.addWidget(self.context_bar)
        lay.addWidget(defaults_box)
        lay.addWidget(splitter, 1)
        lay.addWidget(line)
        lay.addLayout(bottom)

        self.model.rowsInserted.connect(self._update_counts)
        self.model.rowsRemoved.connect(self._update_counts)
        self.model.dataChanged.connect(self._update_counts)
        self._on_defaults()
        self._update_counts()

    # -- context / data loading ------------------------------------------

    def start(self) -> None:
        svc = self.service
        self.journal = UploadJournal(None if svc.is_demo else default_path(),
                                     f"{svc.host}|{svc.user.get('email', '')}")
        self.create_cb.setEnabled(self.service.can_create_tasks)
        if not self.service.can_create_tasks:
            self.create_cb.setChecked(False)
            self.create_cb.setToolTip("Your Kitsu role can't create tasks.")
        self.context_bar.reload()

    def _on_statuses(self, statuses: list[dict]) -> None:
        self.statuses = statuses
        self.allowed[:] = self.service.allowed_statuses(statuses)
        self.model.status_by_id = {s["id"]: s for s in statuses}
        want = settings().value("upload/status", "") or self.service.default_status_id(self.allowed)
        self.status_cb.set_statuses(self.allowed, want)
        self._on_defaults()

    def _on_context(self, ctx) -> None:
        old = self.ctx
        self.ctx = ctx
        if ctx is None:
            self.model.entities, self.model.tasks = {}, {}
            self.rematch(force=True)
            return
        self.ctx_loading = True
        self._update_counts()
        self.status_message.emit("Loading shots/assets and tasks...")
        svc = self.service

        def load():
            ents = svc.shots(ctx.project, ctx.episode) if ctx.kind == "shot" else svc.assets(ctx.project, ctx.episode)
            tasks = svc.tasks_by_entity(ctx.project, ctx.task_type, ctx.episode)
            return ents, tasks

        def got(res):
            if self.ctx is not ctx:
                return
            ents, tasks = res
            self.model.entities = {e["id"]: e for e in ents}
            self.model.tasks = tasks
            self.model.show_episode = (ctx.kind == "shot" and ctx.episode is None
                                       and len({e.get("episode_name") for e in ents}) > 1)
            self.ctx_loading = False
            # Changing project/episode/kind invalidates manual picks that
            # no longer exist; changing only the task type keeps them.
            same_entities = old is not None and old.key[:3] == ctx.key[:3]
            self.rematch(force=not same_entities)
            self.status_message.emit(f"{len(ents)} {ctx.kind}s, {len(tasks)} {ctx.task_type['name']} tasks loaded.")

        def fail(msg):
            self.ctx_loading = False
            self.status_message.emit(f"Error: {msg}")
            self._update_counts()

        run_async(load, got, fail)

    def rematch(self, force: bool = False) -> None:
        """Auto-match every row not assigned by hand. ``force`` also drops
        manual assignments whose entity isn't in the current list."""
        ents = list(self.model.entities.values())
        kind = self.ctx.kind if self.ctx else "shot"
        for it in self.model.items:
            if it.state == DONE:
                continue
            if it.manual and it.entity_id in self.model.entities:
                continue
            if it.manual and not force and it.entity_id is None:
                continue  # user put it in the pool on purpose
            it.manual = False
            res = matcher.match(it.name, ents, kind) if ents else matcher.MatchResult(None, 0, "Select a task first")
            it.entity_id = res.entity_id
            it.match_score = res.score
            it.match_reason = res.reason
            it.candidates = res.candidates
        self._refilter()

    def _refilter(self) -> None:
        self.mapped_proxy.invalidate()
        self.pool_proxy.invalidate()
        self.model.refresh()
        self._fill_entity_list()
        self._update_counts()

    # -- defaults ---------------------------------------------------------

    def _on_defaults(self, *_) -> None:
        d = self.model.defaults
        d.status_id = self.status_cb.current_id()
        d.mode = REVISION if self.rev_rb.isChecked() else ATTACHMENT
        d.comment = self.comment_ed.text()
        d.combine = self.combine_cb.isChecked()
        d.revision_from_filename = self.revnum_cb.isChecked()
        d.set_thumbnail = self.thumb_cb.isChecked()
        d.create_missing_tasks = self.create_cb.isChecked() and self.create_cb.isEnabled()
        self.revnum_cb.setEnabled(True)
        s = settings()
        if d.status_id:
            s.setValue("upload/status", d.status_id)
        s.setValue("upload/comment", d.comment)
        s.setValue("upload/combine", d.combine)
        s.setValue("upload/revnum", d.revision_from_filename)
        s.setValue("upload/thumb", d.set_thumbnail)
        self.model.refresh()

    # -- adding files -----------------------------------------------------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.running_groups:
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def _add_files_dialog(self):
        start = settings().value("upload/last_dir", "")
        files, _ = QFileDialog.getOpenFileNames(self, "Add files", start)
        if files:
            settings().setValue("upload/last_dir", os.path.dirname(files[0]))
            self.add_paths(files)

    def _add_dir_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder", settings().value("upload/last_dir", ""))
        if d:
            settings().setValue("upload/last_dir", d)
            self.add_paths([d])

    def add_paths(self, paths: list[str]) -> None:
        existing = {os.path.normcase(os.path.abspath(i.path)) for i in self.model.items}
        new = []
        skipped = 0
        for p in expand_paths(paths):
            key = os.path.normcase(os.path.abspath(p))
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            new.append(UploadItem(row_id=self.model.new_id(), path=p, size=size))
        self.model.add(new)
        self.rematch()
        n_auto = sum(1 for i in new if i.entity_id)
        msg = f"Added {len(new)} file(s): {n_auto} auto-mapped, {len(new) - n_auto} in the pool."
        if skipped:
            msg += f" {skipped} duplicate(s) ignored."
        self.status_message.emit(msg)

    # -- selection helpers ------------------------------------------------

    def _selected(self, view: QTableView) -> list[UploadItem]:
        proxy = view.model()
        rows = {proxy.mapToSource(ix).row() for ix in view.selectionModel().selectedRows()}
        return [self.model.items[r] for r in sorted(rows)]

    def _editable(self, items: list[UploadItem]) -> list[UploadItem]:
        return [i for i in items if i.state not in (RUNNING, QUEUED, DONE)]

    def _remove(self, items: list[UploadItem]) -> None:
        items = [i for i in items if i.state not in (RUNNING, QUEUED)]
        rows = [self.model.row_of(i.row_id) for i in items]
        self.model.remove_rows([r for r in rows if r >= 0])
        self._refilter()

    # -- context menus ----------------------------------------------------

    def _table_menu(self, pos) -> None:
        items = self._editable(self._selected(self.table))
        if not items or self.running_groups:
            return
        n = len(items)
        m = QMenu(self)
        st = m.addMenu(f"Set status ({n})")
        for s in self.allowed:
            a = st.addAction(color_icon(s.get("color")), f"{s.get('short_name', '').upper()}  {s['name']}")
            a.triggered.connect(lambda _, sid=s["id"]: self._bulk(items, status_id=sid))
        mo = m.addMenu(f"Upload as ({n})")
        mo.addAction("Revision").triggered.connect(lambda: self._bulk(items, mode=REVISION))
        mo.addAction("Attachment").triggered.connect(lambda: self._bulk(items, mode=ATTACHMENT))
        m.addAction(f"Set comment ({n})...").triggered.connect(lambda: self._bulk_comment(items))
        m.addAction(f"Reset to defaults ({n})").triggered.connect(
            lambda: self._bulk(items, status_id=None, mode=None, comment=None))
        m.addSeparator()
        m.addAction(f"Assign to... ({n})").triggered.connect(lambda: self._assign_dialog(items))
        m.addAction(f"Move to unassigned pool ({n})").triggered.connect(lambda: self._to_pool(items))
        m.addSeparator()
        m.addAction("Include").triggered.connect(lambda: self._bulk(items, include=True))
        m.addAction("Exclude (keep in list, don't upload)").triggered.connect(lambda: self._bulk(items, include=False))
        m.addAction(f"Remove from list ({n})").triggered.connect(lambda: self._remove(items))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _pool_menu(self, pos) -> None:
        items = self._selected(self.pool)
        if not items:
            return
        m = QMenu(self)
        m.addAction(f"Assign to... ({len(items)})").triggered.connect(lambda: self._assign_dialog(items))
        m.addAction(f"Remove from list ({len(items)})").triggered.connect(lambda: self._remove(items))
        m.exec(self.pool.viewport().mapToGlobal(pos))

    def _bulk(self, items: list[UploadItem], **changes) -> None:
        for it in items:
            for k, v in changes.items():
                setattr(it, k, v)
            if it.state == ERROR:
                it.state, it.message = PENDING, ""
        self.model.refresh()
        self._update_counts()

    def _bulk_comment(self, items: list[UploadItem]) -> None:
        current = items[0].comment if items[0].comment is not None else self.model.defaults.comment
        text, ok = QInputDialog.getText(self, "Comment", f"Comment for {len(items)} file(s):", text=current)
        if ok:
            self._bulk(items, comment=text)

    def _to_pool(self, items: list[UploadItem]) -> None:
        for it in items:
            it.entity_id, it.manual, it.match_reason = None, True, "Moved to pool"
        self._refilter()

    def _assign(self, items: list[UploadItem], entity_id: str) -> None:
        for it in items:
            it.entity_id, it.manual = entity_id, True
            it.match_reason = "Assigned manually"
            if it.state == ERROR:
                it.state, it.message = PENDING, ""
        self._refilter()

    def _assign_dialog(self, items: list[UploadItem]) -> None:
        labels = sorted((self.model.entity_label(e), e) for e in self.model.entities)
        if not labels:
            return
        names = [l for l, _ in labels]
        choice, ok = QInputDialog.getItem(self, "Assign", f"Assign {len(items)} file(s) to:", names, 0, True)
        if ok:
            eid = dict(labels).get(choice)
            if eid:
                self._assign(items, eid)

    # -- pool side panel --------------------------------------------------

    def _pool_selection(self, *_):
        self._fill_entity_list()

    def _fill_entity_list(self, *_):
        q = self.entity_search.text().strip().lower()
        sel = self._selected(self.pool) if self.pool.selectionModel() else []
        suggested: list[str] = []
        for it in sel:
            for c in it.candidates:
                if c.entity_id not in suggested:
                    suggested.append(c.entity_id)
        self.entity_list.clear()
        def label_key(e):
            return natural_key(self.model.entity_label(e))

        suggested = sorted((e for e in suggested if e in self.model.entities), key=label_key)
        order = suggested + sorted((e for e in self.model.entities if e not in suggested), key=label_key)
        bold = QFont()
        bold.setBold(True)
        for eid in order:
            label = self.model.entity_label(eid)
            if q and q not in label.lower():
                continue
            li = QListWidgetItem(("★ " if eid in suggested else "") + label)
            li.setData(Qt.ItemDataRole.UserRole, eid)
            if eid in suggested:
                li.setFont(bold)
            self.entity_list.addItem(li)
        if self.entity_list.count():
            self.entity_list.setCurrentRow(0)

    def _assign_from_pool(self) -> None:
        items = self._selected(self.pool)
        cur = self.entity_list.currentItem()
        if not items:
            self.status_message.emit("Select one or more files in the pool first.")
            return
        if not cur:
            return
        # Read the item before assigning: _assign rebuilds this list, which
        # deletes ``cur``.
        entity_id = cur.data(Qt.ItemDataRole.UserRole)
        self._assign(items, entity_id)
        self.status_message.emit(f"Assigned {len(items)} file(s) to {self.model.entity_label(entity_id)}.")

    # -- summary ----------------------------------------------------------

    def _plan(self) -> UploadPlan:
        names = {eid: e["name"] for eid, e in self.model.entities.items()}
        return plan_upload(self.model.items, self.model.defaults, self.model.tasks, names)

    def _update_counts(self, *_) -> None:
        items = self.model.items
        has = bool(items)
        self.table.setVisible(has)
        self.drop_hint.setVisible(not has)
        n_pool = sum(1 for i in items if i.entity_id is None)
        n_mapped = len(items) - n_pool
        self.mapped_lbl.setText(f"<b>Mapped files ({n_mapped})</b>")
        self.pool_lbl.setText(f"<b>Unassigned pool ({n_pool})</b>"
                              + ("  <span style='color:gray'>select files, pick a shot/asset below</span>" if n_pool else ""))
        if self.ctx is None:
            self.summary.setText("Choose a project and task to start.")
            self.upload_btn.setEnabled(False)
            return
        if self.ctx_loading:
            self.summary.setText("Loading...")
            self.upload_btn.setEnabled(False)
            return
        plan = self._plan()
        self.model.blocked = plan.blocked
        n_done = sum(1 for i in items if i.state == DONE)
        n_err = sum(1 for i in items if i.state == ERROR)
        bits = [f"{plan.file_count} ready → {len(plan.groups)} comment(s)"]
        if plan.unassigned:
            bits.append(f"{plan.unassigned} unassigned")
        if plan.blocked:
            bits.append(f"<span style='color:#e5534b'>{len(plan.blocked)} blocked</span>")
        if n_err:
            bits.append(f"<span style='color:#e5534b'>{n_err} failed</span>")
        if n_done:
            bits.append(f"<span style='color:#2ea043'>{n_done} uploaded</span>")
        self.summary.setText(" · ".join(bits))
        self.upload_btn.setEnabled(plan.file_count > 0 and not self.running_groups)
        n_rev, n_att, _ = mode_counts(plan, self.model.defaults)
        label = upload_label(n_rev, n_att).replace("Upload", "Retry && upload" if n_err else "Upload", 1)
        self.upload_btn.setText(label + "...")
        self.undo_btn.setEnabled(not self.running_groups and self.journal is not None
                                 and any(self.journal.pending_undo(b) for b in self.journal.recent()))

    # -- upload -----------------------------------------------------------

    def _ctx_label(self) -> str:
        c = self.ctx
        ep = f" / {c.episode['name']}" if c.episode else ""
        return f"{c.project['name']}{ep} – {c.task_type['name']}"

    def _review(self) -> None:
        plan = self._plan()
        if not plan.groups:
            return
        if ReviewDialog(plan, self.model, self._ctx_label(), self).exec() != QDialog.DialogCode.Accepted:
            return
        self._run(plan)

    def _set_running(self, running: bool) -> None:
        self.model.locked = running
        for w in (self.context_bar, self.add_files_btn, self.add_dir_btn, self.rematch_btn,
                  self.clear_btn, self.clear_done_btn, self.assign_btn):
            w.setEnabled(not running)
        self.running_changed.emit(running)
        self._update_counts()

    def _run(self, plan: UploadPlan) -> None:
        d = self.model.defaults
        ctx = self.ctx
        self._batch_id = self.journal.new_batch(self._ctx_label()) if self.journal else ""
        self.running_groups = len(plan.groups)
        self._run_stats = {"ok": 0, "failed": 0}
        for g in plan.groups:
            for it in g.pending_items:
                it.state, it.progress, it.message = QUEUED, 0.0, ""
        self.model.refresh()
        self._set_running(True)
        self.status_message.emit(f"Uploading {plan.file_count} file(s)...")
        for g in plan.groups:
            self._start_group(g, d, ctx)

    def _start_group(self, g: UploadGroup, d: UploadDefaults, ctx) -> None:
        svc = self.service
        pend = g.pending_items
        files = [PublishFile(i.path, i.eff_mode(d), i.row_id) for i in pend]
        sizes = {i.row_id: max(i.size, 1) for i in pend}
        prog = self.progress
        entity = dict(self.model.entities.get(g.entity_id, {"id": g.entity_id}))
        entity.setdefault("project_id", ctx.project["id"])
        thumb = d.set_thumbnail
        task_type = ctx.task_type
        record = {
            "batch_id": self._batch_id, "entity_label": self.model.entity_label(g.entity_id),
            "new_status_id": g.status_id, "files": [i.name for i in pend], "thumb_set": thumb,
        }

        def work():
            task_id = g.task_id
            if task_id is None:
                task = svc.create_task(entity, task_type)
                task_id = task["id"]
                prev_status = task.get("task_status_id")
                record["task_created"] = True
                prog.state.emit(-1, "task", f"{g.entity_id}|{task_id}")
            elif g.comment_id:
                prev_status = None  # resuming: the original record has it
            else:
                # Read fresh, right before posting: what undo restores.
                prev_status = svc.task(task_id).get("task_status_id")
            record.update(task_id=task_id, prev_status_id=prev_status)
            if thumb and not g.comment_id:
                try:
                    record["thumb_prev_preview_id"] = svc.entity_preview_id(g.entity_id)
                except Exception:
                    pass
            if g.comment_id:
                prog.record.emit(dict(record, comment_id=g.comment_id))
            for f in files:
                prog.state.emit(f.row_id, RUNNING, "")

            def on_comment_created(comment_id):
                prog.record.emit(dict(record, comment_id=comment_id))

            def on_progress(row_id, sent, total):
                # Attachments posted with the comment share one request:
                # spread its progress over those rows.
                prog.progress.emit(row_id, min(sent / max(total, sizes.get(row_id, 1)), 1.0))

            def on_file_done(row_id, comment_id):
                prog.file_done.emit(row_id, comment_id or "", "")

            try:
                res = svc.publish(
                    task_id, g.status_id, g.text, files,
                    existing_comment_id=g.comment_id, existing_preview_id=g.preview_id,
                    revision=g.revision, set_thumbnail=thumb,
                    on_progress=on_progress, on_file_done=on_file_done,
                    on_comment_created=on_comment_created,
                )
            except PublishError as exc:
                for row_id, pid in exc.result.preview_ids.items():
                    prog.file_done.emit(row_id, exc.result.comment_id or "", pid)
                for f in files:
                    if f.row_id not in exc.result.done_row_ids:
                        # Remember the comment so a retry adds to it instead
                        # of posting a duplicate.
                        prog.file_done.emit(f.row_id, exc.result.comment_id or "", "__failed__")
                        prog.state.emit(f.row_id, ERROR, str(exc))
                raise
            for row_id, pid in res.preview_ids.items():
                prog.file_done.emit(row_id, res.comment_id or "", pid)
            return task_id

        def done(_):
            self._run_stats["ok"] += 1
            self._group_finished()

        def failed(msg):
            for i in pend:
                if i.state in (RUNNING, QUEUED):
                    i.state, i.message = ERROR, msg
            self._run_stats["failed"] += 1
            self.model.refresh()
            self._group_finished()

        run_async(work, done, failed, pool=TRANSFER_POOL)

    def _group_finished(self) -> None:
        self.running_groups -= 1
        if self.running_groups > 0:
            return
        self._set_running(False)
        ok, bad = self._run_stats["ok"], self._run_stats["failed"]
        msg = f"Upload finished: {ok} comment(s) posted"
        msg += f", {bad} failed - fix or just click Retry." if bad else "."
        if ok:
            msg += "  Made a mistake? Use “Undo upload...”."
        self.status_message.emit(msg)
        QApplication.alert(self.window())
        # Statuses on the server changed: refresh the "Current" column.
        if self.ctx:
            ctx = self.ctx
            run_async(lambda: self.service.tasks_by_entity(ctx.project, ctx.task_type, ctx.episode),
                      lambda t: (setattr(self.model, "tasks", t), self.model.refresh()) if self.ctx is ctx else None)

    def _on_progress(self, row_id: int, frac: float) -> None:
        r = self.model.row_of(row_id)
        if r >= 0:
            it = self.model.items[r]
            it.progress = frac
            self.model.dataChanged.emit(self.model.index(r, C_RESULT), self.model.index(r, C_RESULT))

    def _on_file_done(self, row_id: int, comment_id: str, preview_id: str) -> None:
        it = self.model.item_by_id(row_id)
        if not it:
            return
        it.comment_id = comment_id or it.comment_id
        if preview_id and preview_id != "__failed__":
            it.preview_id = preview_id
        if preview_id != "__failed__":
            it.state, it.progress, it.message = DONE, 1.0, ""
        self.model.refresh(self.model.row_of(row_id))

    def _on_state(self, row_id: int, state: str, msg: str) -> None:
        if row_id == -1 and state == "task":
            eid, tid = msg.split("|", 1)
            self.model.tasks[eid] = {"id": tid, "entity_id": eid}
            return
        it = self.model.item_by_id(row_id)
        if it:
            it.state, it.message = state, msg
            if state == RUNNING:
                it.progress = 0.0
            self.model.refresh(self.model.row_of(row_id))

    # -- undo -------------------------------------------------------------

    def _on_record(self, d: dict) -> None:
        if self.journal is None:
            return
        d = dict(d)
        batch_id = d.pop("batch_id")
        self.journal.record(batch_id, CommentRecord(**d))
        self._update_counts()

    def _open_history(self) -> None:
        if self.journal is None:
            return

        def status_name(sid):
            s = self.model.status_by_id.get(sid or "")
            return s.get("short_name", "").upper() if s else "?"

        dlg = UndoDialog(self.journal, self.service, status_name, self)
        dlg.undone.connect(self._after_undo)
        dlg.exec()
        self._update_counts()

    def _after_undo(self, comment_ids: list) -> None:
        """Rows whose comment was removed go back to Ready, so they can be
        fixed (mode, status, shot...) and uploaded again."""
        gone = set(comment_ids)
        for it in self.model.items:
            if it.comment_id in gone:
                it.state, it.progress, it.message = PENDING, 0.0, ""
                it.comment_id = it.preview_id = None
        self._refilter()
        self.status_message.emit(f"Undid {len(gone)} comment(s). Their files are back in the list, ready to fix.")
        if self.ctx:
            ctx = self.ctx
            run_async(lambda: self.service.tasks_by_entity(ctx.project, ctx.task_type, ctx.episode),
                      lambda t: (setattr(self.model, "tasks", t), self.model.refresh()) if self.ctx is ctx else None)

    # -- clearing ---------------------------------------------------------

    def _clear_done(self) -> None:
        self.model.remove_rows([r for r, i in enumerate(self.model.items) if i.state == DONE])
        self._refilter()

    def _clear_all(self) -> None:
        if not self.model.items:
            return
        if QMessageBox.question(self, "Clear all", f"Remove all {len(self.model.items)} file(s) from the list?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.model.remove_rows(list(range(len(self.model.items))))
        self._refilter()

    def is_busy(self) -> bool:
        return self.running_groups > 0
