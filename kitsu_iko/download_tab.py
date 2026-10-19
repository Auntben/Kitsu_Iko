"""Download tab: list every shot/asset's revisions or attachments for a task,
latest preselected; untick or pick other versions; download to a templated
folder layout."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PyQt6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton,
    QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from .models import (
    ATTACHMENT, DONE, ERROR, PENDING, QUEUED, REVISION, RUNNING, DownloadItem,
    build_choices, render_path, safe_part,
)
from .service import KitsuService
from .upload_tab import ProgressDelegate, entity_label, result_icon
from .widgets import ContextBar, color_icon, settings
from .workers import TRANSFER_POOL, Progress, run_async

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
# Same layout as the upload table: Result first, icon + one word, details
# in the tooltip.
C_RESULT, C_ENTITY, C_STATUS, C_VERSION, C_FILES, C_WHO = range(6)
HEADERS = ["Result", "Shot / Asset", "Task status", "Version", "Files", "Posted"]

FOLDER_PRESETS = [
    ("Everything in one folder", ""),
    ("One folder per shot/asset", "{entity_full}"),
    ("Episode / Sequence / Shot / Task", "{episode}/{sequence}/{entity}/{task}"),
    ("Project / Episode / Sequence / Shot / Task", "{project}/{episode}/{sequence}/{entity}/{task}"),
]
DEFAULT_NAME = "{entity_full}_{task}_v{rev:03d}"
GREY = QBrush(QColor(140, 140, 140))
GREEN = QBrush(QColor(46, 160, 67))
RED = QBrush(QColor(229, 83, 75))


class DownloadModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[DownloadItem] = []
        self.status_by_id: dict[str, dict] = {}
        self.locked = False
        self.show_episode = False
        # Set by the tab: item -> destination paths for its chosen version,
        # so the Files column previews names from the templates.
        self.paths_fn = None

    def label(self, it: DownloadItem) -> str:
        return entity_label(it.entity, self.show_episode)

    def _paths(self, it: DownloadItem) -> list[str]:
        if self.paths_fn is None:
            return [f.get("name") or f"preview.{f.get('extension', '')}" for f in it.choice.files]
        try:
            return [p for _, p in self.paths_fn(it)]
        except Exception as exc:  # a half-typed template must not break painting
            return [f"(template error: {exc})"]

    def refresh_column(self, col: int) -> None:
        if self.items:
            self.dataChanged.emit(self.index(0, col), self.index(len(self.items) - 1, col))

    def set_items(self, items: list[DownloadItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def refresh(self, row: int | None = None) -> None:
        if not self.items:
            return
        a = self.index(0 if row is None else row, 0)
        b = self.index(len(self.items) - 1 if row is None else row, len(HEADERS) - 1)
        self.dataChanged.emit(a, b)

    def row_of(self, row_id: int) -> int:
        return next((r for r, i in enumerate(self.items) if i.row_id == row_id), -1)

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
        if self.locked or not it.choices:
            return f
        if index.column() == C_ENTITY:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() == C_VERSION:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        ch = it.choice
        if role == ITEM_ROLE:
            return it
        if role == Qt.ItemDataRole.CheckStateRole and col == C_ENTITY:
            return Qt.CheckState.Checked if it.include and it.choices else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == C_ENTITY:
                return self.label(it)
            if col == C_STATUS:
                s = self.status_by_id.get(it.task.get("task_status_id", ""))
                return s.get("short_name", "").upper() if s else ""
            if col == C_VERSION:
                if not it.choices:
                    return "nothing to download"
                latest = " (latest)" if it.choice_index == 0 else ""
                if ch.kind == REVISION:
                    return f"v{ch.revision:03d}{latest}"
                return f"{ch.created_at}{latest}"
            if col == C_FILES:
                if not ch:
                    return ""
                names = [os.path.basename(p) for p in self._paths(it)]
                more = f"  (+{len(names) - 2} more)" if len(names) > 2 else ""
                return ", ".join(names[:2]) + more
            if col == C_WHO:
                return f"{ch.created_at}  {ch.author}" if ch else ""
            if col == C_RESULT:
                if it.state == RUNNING:
                    return f"{int(it.progress * 100)}%"
                if it.state in (QUEUED, DONE, ERROR):
                    return {QUEUED: "Queued", DONE: "Done", ERROR: "Failed"}[it.state]
                if not it.choices:
                    return ""
                return "Ready" if it.include else "Skipped"
        if role == Qt.ItemDataRole.DecorationRole:
            if col == C_RESULT:
                return result_icon(it.state) if it.state in (QUEUED, DONE, ERROR) else None
            if col == C_STATUS:
                s = self.status_by_id.get(it.task.get("task_status_id", ""))
                return color_icon(s.get("color")) if s else None
        if role == Qt.ItemDataRole.ForegroundRole:
            if not it.choices:
                return GREY
            if col == C_VERSION and it.choice_index > 0:
                return QBrush(QColor(210, 140, 0))
            if col == C_RESULT:
                return {DONE: GREEN, ERROR: RED}.get(it.state, GREY if it.state == PENDING else None)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == C_VERSION and ch:
                return ch.label + "\nDouble-click to pick another version."
            if col == C_RESULT:
                if it.state == ERROR:
                    return f"Download failed: {it.message}\nClick Download again to retry."
                if it.state == DONE:
                    return f"Downloaded: {it.message}."
                if it.state == QUEUED:
                    return "Waiting for a free download slot."
                if it.state == RUNNING:
                    return "Downloading..."
                if not it.choices:
                    return "Nothing to download for this task."
                return "Will be downloaded." if it.include else "Skipped: tick the checkbox to include it."
            if col == C_FILES and ch:
                return "Will be saved as:\n" + "\n".join(self._paths(it))
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        it = self.items[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == C_ENTITY:
            it.include = Qt.CheckState(value) == Qt.CheckState.Checked
        elif role == Qt.ItemDataRole.EditRole and index.column() == C_VERSION:
            if value == -1:
                it.include = False
            else:
                it.choice_index, it.include = int(value), True
        else:
            return False
        if it.state in (DONE, ERROR):
            it.state, it.message = PENDING, ""
        self.refresh(index.row())
        return True


class TextFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.hide_empty = True
        self.status_id: str | None = None

    def filterAcceptsRow(self, source_row, source_parent):
        m: DownloadModel = self.sourceModel()
        it = m.items[source_row]
        if self.hide_empty and not it.choices:
            return False
        if self.status_id and it.task.get("task_status_id") != self.status_id:
            return False
        return not self.text or self.text in m.label(it).lower()


class VersionDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        it: DownloadItem = index.data(ITEM_ROLE)
        for i, ch in enumerate(it.choices):
            cb.addItem(("★ latest: " if i == 0 else "") + ch.label, i)
        cb.addItem("(skip this one)", -1)
        return cb

    def setEditorData(self, editor, index):
        it: DownloadItem = index.data(ITEM_ROLE)
        editor.setCurrentIndex(it.choice_index if it.include else editor.count() - 1)
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData())


class DownloadTab(QWidget):
    status_message = pyqtSignal(str)
    running_changed = pyqtSignal(bool)

    def __init__(self, service: KitsuService, parent=None):
        super().__init__(parent)
        self.service = service
        self.ctx = None
        self.comments: dict[str, list[dict]] = {}  # task_id -> comments
        self.rows_meta: list[tuple[dict, dict]] = []  # (entity, task)
        self.load_gen = 0
        self.running = 0
        self.model = DownloadModel(self)
        self.progress = Progress()
        self.progress.progress.connect(self._on_progress)
        self.progress.state.connect(self._on_state)

        self.context_bar = ContextBar(service, "download")
        self.context_bar.context_changed.connect(self._on_context)
        self.context_bar.statuses_loaded.connect(self._on_statuses)
        self.context_bar.busy.connect(self.status_message)

        # What to download
        self.rev_rb = QRadioButton("Revisions (published previews)")
        self.att_rb = QRadioButton("Comment attachments")
        grp = QButtonGroup(self)
        grp.addButton(self.rev_rb)
        grp.addButton(self.att_rb)
        (self.att_rb if settings().value("download/source", REVISION) == ATTACHMENT else self.rev_rb).setChecked(True)
        self.rev_rb.toggled.connect(self._rebuild_choices)
        self.filter_ed = QLineEdit()
        self.filter_ed.setPlaceholderText("Filter shots/assets...")
        self.filter_ed.textChanged.connect(self._apply_filter)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Any status", None)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        self.hide_empty_cb = QCheckBox("Hide empty")
        self.hide_empty_cb.setToolTip("Hide shots/assets with nothing to download")
        self.hide_empty_cb.setChecked(True)
        self.hide_empty_cb.toggled.connect(self._apply_filter)
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_include(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._set_include(False))
        latest_btn = QPushButton("Reset to latest")
        latest_btn.clicked.connect(lambda: self._set_choice(lambda it: 0))

        src = QHBoxLayout()
        src.addWidget(QLabel("Download"))
        src.addWidget(self.rev_rb)
        src.addWidget(self.att_rb)
        src.addSpacing(16)
        src.addWidget(self.filter_ed, 1)
        src.addWidget(self.status_filter)
        src.addWidget(self.hide_empty_cb)
        src.addWidget(all_btn)
        src.addWidget(none_btn)
        src.addWidget(latest_btn)

        # Table
        self.proxy = TextFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_FILES, QHeaderView.ResizeMode.Stretch)
        for col, w in ((C_RESULT, 95), (C_ENTITY, 220), (C_STATUS, 90), (C_VERSION, 150), (C_WHO, 220)):
            self.table.setColumnWidth(col, w)
        self.table.sortByColumn(C_ENTITY, Qt.SortOrder.AscendingOrder)
        self.table.setItemDelegateForColumn(C_VERSION, VersionDelegate(self.table))
        self.table.setItemDelegateForColumn(C_RESULT, ProgressDelegate(self.table))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)

        # Output
        out_box = QGroupBox("Save to")
        og = QGridLayout(out_box)
        self.dest_ed = QLineEdit(settings().value("download/dest", os.path.normpath(os.path.expanduser("~/Downloads/Kitsu"))))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        self.folder_cb = QComboBox()
        self.folder_cb.setEditable(True)
        for label, tpl in FOLDER_PRESETS:
            self.folder_cb.addItem(tpl or "(flat)", tpl)
            self.folder_cb.setItemData(self.folder_cb.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        self.folder_cb.setCurrentText(settings().value("download/folder_tpl", "{entity_full}") or "(flat)")
        self.name_ed = QLineEdit(settings().value("download/name_tpl", DEFAULT_NAME))
        self.name_ed.setToolTip("Revision file name (extension added automatically). Attachments keep their own names.")
        self.skip_cb = QCheckBox("Skip files that already exist")
        self.skip_cb.setChecked(settings().value("download/skip", True, type=bool))
        self.open_cb = QCheckBox("Open folder when done")
        self.open_cb.setChecked(settings().value("download/open", True, type=bool))
        tokens = QLabel("Tokens: {project} {episode} {sequence} {entity} {entity_full} {asset_type} {task} {rev} {status} {original}")
        tokens.setStyleSheet("color: gray;")
        self.example = QLabel()
        self.example.setStyleSheet("color: gray;")
        og.addWidget(QLabel("Folder"), 0, 0)
        og.addWidget(self.dest_ed, 0, 1, 1, 3)
        og.addWidget(browse, 0, 4)
        og.addWidget(QLabel("Sub-folders"), 1, 0)
        og.addWidget(self.folder_cb, 1, 1)
        self.name_lbl = QLabel("File name")
        og.addWidget(self.name_lbl, 1, 2)
        og.addWidget(self.name_ed, 1, 3, 1, 2)
        og.addWidget(tokens, 2, 1, 1, 4)
        og.addWidget(self.example, 3, 1, 1, 4)
        opts = QHBoxLayout()
        opts.addWidget(self.skip_cb)
        opts.addWidget(self.open_cb)
        opts.addStretch(1)
        og.addLayout(opts, 4, 1, 1, 4)
        og.setColumnStretch(1, 1)
        og.setColumnStretch(3, 1)
        self.model.paths_fn = self.plan_paths
        for sig in (self.dest_ed.textChanged, self.name_ed.textChanged, self.folder_cb.currentTextChanged):
            sig.connect(lambda *_: self.model.refresh_column(C_FILES))

        self.summary = QLabel()
        self.download_btn = QPushButton("Download")
        self.download_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.download_btn.clicked.connect(self._download)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.download_btn)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay = QVBoxLayout(self)
        lay.addWidget(self.context_bar)
        lay.addLayout(src)
        lay.addWidget(self.table, 1)
        lay.addWidget(out_box)
        lay.addWidget(line)
        lay.addLayout(bottom)
        self.model.dataChanged.connect(self._update_summary)
        self.model.modelReset.connect(self._update_summary)
        self._sync_name_field()
        self._update_summary()

    def start(self) -> None:
        self.context_bar.reload()

    # -- loading ----------------------------------------------------------

    def _on_statuses(self, statuses: list[dict]) -> None:
        self.model.status_by_id = {s["id"]: s for s in statuses}
        self.status_filter.blockSignals(True)
        self.status_filter.clear()
        self.status_filter.addItem("Any status", None)
        for s in statuses:
            self.status_filter.addItem(color_icon(s.get("color")), s.get("short_name", "").upper(), s["id"])
        self.status_filter.blockSignals(False)

    def _on_context(self, ctx) -> None:
        self.ctx = ctx
        self.load_gen += 1
        gen = self.load_gen
        self.comments = {}
        self.rows_meta = []
        self.model.set_items([])
        if ctx is None:
            return
        svc = self.service
        prog = self.progress

        def load():
            ents = svc.shots(ctx.project, ctx.episode) if ctx.kind == "shot" else svc.assets(ctx.project, ctx.episode)
            tasks = svc.tasks_by_entity(ctx.project, ctx.task_type, ctx.episode)
            pairs = [(e, tasks[e["id"]]) for e in ents if e["id"] in tasks]
            comments: dict[str, list[dict]] = {}
            done = 0

            def fetch(task):
                return task["id"], svc.comments(task["id"])

            with ThreadPoolExecutor(max_workers=6) as ex:
                for tid, cs in ex.map(fetch, [t for _, t in pairs]):
                    if self.load_gen != gen:
                        return None  # context changed: abandon
                    comments[tid] = cs
                    done += 1
                    if done % 10 == 0:
                        prog.state.emit(-1, "loading", f"Loading comments {done}/{len(pairs)}...")
            return pairs, comments

        def got(res):
            if res is None or self.load_gen != gen:
                return
            self.rows_meta, self.comments = res
            self.model.show_episode = (ctx.kind == "shot" and ctx.episode is None
                                       and len({e.get("episode_name") for e, _ in self.rows_meta}) > 1)
            self._rebuild_choices()
            n = sum(1 for i in self.model.items if i.choices)
            self.status_message.emit(f"{len(self.rows_meta)} {ctx.kind}s with a {ctx.task_type['name']} task, "
                                     f"{n} with something to download.")

        self.status_message.emit("Loading tasks and comments...")
        run_async(load, got, lambda m: self.status_message.emit(f"Error: {m}"))

    def _sync_name_field(self) -> None:
        revisions = self.rev_rb.isChecked()
        self.name_ed.setEnabled(revisions)
        self.name_lbl.setEnabled(revisions)
        self.name_ed.setToolTip(
            "Revision file name (extension added automatically)." if revisions
            else "Not used for attachments: they keep their original file names.")

    def _rebuild_choices(self, *_) -> None:
        self._sync_name_field()
        kind = REVISION if self.rev_rb.isChecked() else ATTACHMENT
        settings().setValue("download/source", kind)
        items = []
        for n, (entity, task) in enumerate(self.rows_meta):
            choices = build_choices(self.comments.get(task["id"], []), kind)
            items.append(DownloadItem(row_id=n, entity=entity, task=task, choices=choices, include=bool(choices)))
        self.model.set_items(items)
        self._apply_filter()

    def _apply_filter(self, *_) -> None:
        self.proxy.text = self.filter_ed.text().strip().lower()
        self.proxy.hide_empty = self.hide_empty_cb.isChecked()
        self.proxy.status_id = self.status_filter.currentData()
        self.proxy.invalidate()
        self._update_summary()

    # -- selection / bulk -------------------------------------------------

    def _visible(self) -> list[DownloadItem]:
        return [self.model.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _selected(self) -> list[DownloadItem]:
        rows = {self.proxy.mapToSource(ix).row() for ix in self.table.selectionModel().selectedRows()}
        return [self.model.items[r] for r in sorted(rows)]

    def _set_include(self, value: bool, items=None) -> None:
        for it in items if items is not None else self._visible():
            if it.choices:
                it.include = value
        self.model.refresh()

    def _set_choice(self, pick, items=None) -> None:
        for it in items if items is not None else self._visible():
            if it.choices:
                idx = pick(it)
                if idx is not None and 0 <= idx < len(it.choices):
                    it.choice_index = idx
        self.model.refresh()

    def _menu(self, pos) -> None:
        items = [i for i in self._selected() if i.choices]
        if not items or self.running:
            return
        n = len(items)
        m = QMenu(self)
        m.addAction(f"Include ({n})").triggered.connect(lambda: self._set_include(True, items))
        m.addAction(f"Skip ({n})").triggered.connect(lambda: self._set_include(False, items))
        m.addSeparator()
        m.addAction("Use latest").triggered.connect(lambda: self._set_choice(lambda it: 0, items))
        m.addAction("Use previous (one older)").triggered.connect(
            lambda: self._set_choice(lambda it: min(it.choice_index + 1, len(it.choices) - 1), items))
        if self.rev_rb.isChecked():
            m.addAction("Use revision number...").triggered.connect(lambda: self._pick_revision(items))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _pick_revision(self, items) -> None:
        rev, ok = QInputDialog.getInt(self, "Revision", "Revision number (rows without it keep their choice):", 1, 1, 9999)
        if not ok:
            return
        self._set_choice(lambda it: next((i for i, c in enumerate(it.choices) if c.revision == rev), None), items)

    # -- output -----------------------------------------------------------

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Download to", self.dest_ed.text())
        if d:
            self.dest_ed.setText(d)

    def _folder_tpl(self) -> str:
        t = self.folder_cb.currentText().strip()
        return "" if t in ("(flat)", "") else t

    def _values(self, it: DownloadItem, ch, f: dict | None) -> dict:
        ctx = self.ctx
        e = it.entity
        seq = e.get("sequence_name") or ""
        status = self.model.status_by_id.get(it.task.get("task_status_id", ""), {})
        return {
            "project": ctx.project["name"],
            "episode": e.get("episode_name") or (ctx.episode or {}).get("name", ""),
            "sequence": seq,
            "entity": e["name"],
            "entity_full": f"{seq}_{e['name']}" if seq else e["name"],
            "asset_type": e.get("asset_type_name", ""),
            "task": ctx.task_type["name"],
            "rev": ch.revision if ch and ch.revision is not None else 0,
            "status": status.get("short_name", ""),
            "original": os.path.splitext((f or {}).get("original_name") or (f or {}).get("name") or "")[0],
        }

    def plan_paths(self, it: DownloadItem) -> list[tuple[dict, str]]:
        """(file dict, absolute destination) for the item's chosen version."""
        ch = it.choice
        if ch is None:
            return []
        root = os.path.normpath(self.dest_ed.text().strip())
        out = []
        used: set[str] = set()
        for n, f in enumerate(ch.files):
            v = self._values(it, ch, f)
            folder = render_path(self._folder_tpl(), v) if self._folder_tpl() else ""
            if ch.kind == ATTACHMENT:
                name = safe_part(f.get("name") or f"attachment_{n}")
            else:
                base = render_path(self.name_ed.text().strip() or DEFAULT_NAME, v)
                base = os.path.basename(base)
                if len(ch.files) > 1:
                    base += f"_{n + 1}"
                name = f"{base}.{f.get('extension') or 'bin'}"
            path = os.path.join(root, folder, name) if folder else os.path.join(root, name)
            stem, ext = os.path.splitext(path)
            k = 2
            while path.lower() in used:
                path = f"{stem}_{k}{ext}"
                k += 1
            used.add(path.lower())
            out.append((f, path))
        return out

    def _update_summary(self, *_) -> None:
        chosen = [i for i in self.model.items if i.include and i.choice and i.state != DONE]
        n_files = sum(len(i.choice.files) for i in chosen)
        older = sum(1 for i in chosen if i.choice_index > 0)
        n_done = sum(1 for i in self.model.items if i.state == DONE)
        txt = f"{len(chosen)} shot(s)/asset(s), {n_files} file(s) to download"
        if older:
            txt += f" · {older} set to an older version"
        if n_done:
            txt += f" · {n_done} done"
        self.summary.setText(txt)
        self.download_btn.setEnabled(n_files > 0 and not self.running)
        example = next((i for i in chosen), None)
        if example and self.ctx:
            try:
                p = self.plan_paths(example)
                self.example.setText(f"e.g. {p[0][1]}" if p else "")
            except Exception as exc:
                self.example.setText(f"Template error: {exc}")
        else:
            self.example.setText("")

    # -- download ---------------------------------------------------------

    def _download(self) -> None:
        dest = self.dest_ed.text().strip()
        if not dest:
            self.status_message.emit("Choose a destination folder first.")
            return
        s = settings()
        s.setValue("download/dest", dest)
        s.setValue("download/folder_tpl", self.folder_cb.currentText())
        s.setValue("download/name_tpl", self.name_ed.text())
        s.setValue("download/skip", self.skip_cb.isChecked())
        s.setValue("download/open", self.open_cb.isChecked())
        # Already-downloaded rows are left alone, so "Download" again retries
        # only failures (and anything re-picked since).
        jobs = [(it, self.plan_paths(it)) for it in self.model.items
                if it.include and it.choice and it.state != DONE]
        if not jobs:
            return
        # Two shots rendering to the same path (e.g. SQ010/SH010 of two
        # episodes with a template lacking {episode}) would overwrite each other.
        seen: dict[str, str] = {}
        clashes = []
        for it, paths in jobs:
            for _, p in paths:
                key = os.path.normcase(os.path.abspath(p))
                if key in seen and seen[key] != self.model.label(it):
                    clashes.append(f"{seen[key]}  and  {self.model.label(it)}  \u2192  {p}")
                seen.setdefault(key, self.model.label(it))
        if clashes:
            QMessageBox.warning(
                self, "File names collide",
                f"{len(clashes)} file(s) would overwrite each other. Add {{episode}} and/or "
                "{sequence} to the sub-folder or file name template.\n\n" + "\n".join(clashes[:8]),
            )
            return
        self.running = len(jobs)
        self._stats = {"files": 0, "skipped": 0, "failed": 0}
        self.model.locked = True
        self.context_bar.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.running_changed.emit(True)
        for it, _ in jobs:
            it.state, it.progress, it.message = QUEUED, 0.0, ""
        self.model.refresh()
        self.status_message.emit(f"Downloading {sum(len(p) for _, p in jobs)} file(s)...")
        skip = self.skip_cb.isChecked()
        for it, paths in jobs:
            self._start_job(it, paths, skip)

    def _start_job(self, it: DownloadItem, paths: list[tuple[dict, str]], skip: bool) -> None:
        svc, prog, row_id, kind = self.service, self.progress, it.row_id, it.choice.kind

        def work():
            prog.state.emit(row_id, RUNNING, "")
            done = skipped = 0
            n = len(paths)
            for k, (f, path) in enumerate(paths):
                if skip and os.path.exists(path):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".part"

                def cb(read, total, k=k):
                    frac = (read / total) if total else 0.5
                    prog.progress.emit(row_id, (k + min(frac, 1.0)) / n)

                if kind == ATTACHMENT:
                    svc.download_attachment(f, tmp, cb)
                else:
                    svc.download_preview(f, tmp, cb)
                os.replace(tmp, path)
                done += 1
            return done, skipped

        def ok(res):
            done, skipped = res
            it.state, it.progress = DONE, 1.0
            it.message = f"{done} saved" + (f", {skipped} already there" if skipped else "")
            self._stats["files"] += done
            self._stats["skipped"] += skipped
            self._job_finished()

        def fail(msg):
            it.state, it.message = ERROR, msg
            self._stats["failed"] += 1
            self._job_finished()

        run_async(work, ok, fail, pool=TRANSFER_POOL)

    def _job_finished(self) -> None:
        self.model.refresh()
        self.running -= 1
        if self.running > 0:
            return
        self.model.locked = False
        self.context_bar.setEnabled(True)
        self.running_changed.emit(False)
        st = self._stats
        msg = f"Download finished: {st['files']} file(s) saved"
        if st["skipped"]:
            msg += f", {st['skipped']} skipped (already existed)"
        if st["failed"]:
            msg += f", {st['failed']} shot(s)/asset(s) failed - click Download again to retry those"
        self.status_message.emit(msg + ".")
        self._update_summary()
        QApplication.alert(self.window())
        if self.open_cb.isChecked() and st["files"]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.dest_ed.text().strip()))

    def _on_progress(self, row_id: int, frac: float) -> None:
        r = self.model.row_of(row_id)
        if r >= 0:
            self.model.items[r].progress = frac
            self.model.dataChanged.emit(self.model.index(r, C_RESULT), self.model.index(r, C_RESULT))

    def _on_state(self, row_id: int, state: str, msg: str) -> None:
        if row_id == -1:
            self.status_message.emit(msg)
            return
        r = self.model.row_of(row_id)
        if r >= 0:
            it = self.model.items[r]
            it.state, it.message = state, msg
            self.model.refresh(r)

    def is_busy(self) -> bool:
        return self.running > 0
