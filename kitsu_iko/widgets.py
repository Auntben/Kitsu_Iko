"""Shared widgets: the project/episode/type/task picker, status combo,
and small helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QLabel, QRadioButton, QToolButton,
    QWidget,
)

from .service import KitsuService
from .workers import run_async

ORG, APP = "KitsuIko", "KitsuIko"


def settings() -> QSettings:
    return QSettings(ORG, APP)


def color_icon(hex_color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(QColor(hex_color or "#888888"))
    return QIcon(pm)


def natural_key(text: str) -> list:
    """Sort key putting "SH2" before "SH10" (digits compared as numbers)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class Context:
    project: dict
    episode: dict | None
    kind: str  # "shot" | "asset"
    task_type: dict

    @property
    def key(self) -> tuple:
        return (self.project["id"], (self.episode or {}).get("id"), self.kind, self.task_type["id"])


class StatusCombo(QComboBox):
    def __init__(self, parent=None, allow_default: str | None = None):
        super().__init__(parent)
        self._allow_default = allow_default
        self.setMinimumContentsLength(14)

    def set_statuses(self, statuses: list[dict], current_id: str | None = None) -> None:
        self.blockSignals(True)
        self.clear()
        if self._allow_default:
            self.addItem(self._allow_default, None)
        for s in statuses:
            self.addItem(color_icon(s.get("color")), f"{s.get('short_name', '').upper()}  {s['name']}", s["id"])
        idx = self.findData(current_id) if current_id else -1
        self.setCurrentIndex(max(idx, 0))
        self.blockSignals(False)

    def current_id(self) -> str | None:
        return self.currentData()


class ContextBar(QWidget):
    """Project / Episode / Shots-or-Assets / Task type pickers.

    Emits ``context_changed(Context | None)`` once all four are known, and
    ``statuses_loaded(list)`` when the project's statuses arrive."""

    context_changed = pyqtSignal(object)
    statuses_loaded = pyqtSignal(list)
    busy = pyqtSignal(str)

    def __init__(self, service: KitsuService, settings_prefix: str, parent=None):
        super().__init__(parent)
        self.service = service
        self.prefix = settings_prefix
        self.projects: list[dict] = []
        self.episodes: list[dict] = []
        self.task_types: list[dict] = []
        self._restoring = True

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.project_cb = QComboBox()
        self.project_cb.setMinimumContentsLength(16)
        self.episode_cb = QComboBox()
        self.episode_cb.setMinimumContentsLength(10)
        self.episode_lbl = QLabel("Episode")
        self.shot_rb = QRadioButton("Shots")
        self.asset_rb = QRadioButton("Assets")
        self.kind_group = QButtonGroup(self)
        self.kind_group.addButton(self.shot_rb)
        self.kind_group.addButton(self.asset_rb)
        self.shot_rb.setChecked(True)
        self.task_cb = QComboBox()
        self.task_cb.setMinimumContentsLength(14)
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("↻")
        self.refresh_btn.setToolTip("Reload from Kitsu")

        lay.addWidget(QLabel("Project"))
        lay.addWidget(self.project_cb, 2)
        lay.addWidget(self.episode_lbl)
        lay.addWidget(self.episode_cb, 1)
        lay.addSpacing(8)
        lay.addWidget(self.shot_rb)
        lay.addWidget(self.asset_rb)
        lay.addSpacing(8)
        lay.addWidget(QLabel("Task"))
        lay.addWidget(self.task_cb, 1)
        lay.addWidget(self.refresh_btn)

        self.project_cb.currentIndexChanged.connect(self._on_project)
        self.episode_cb.currentIndexChanged.connect(self._emit)
        self.shot_rb.toggled.connect(self._on_kind)
        self.task_cb.currentIndexChanged.connect(self._emit)
        self.refresh_btn.clicked.connect(self.reload)

    # -- state ------------------------------------------------------------

    @property
    def kind(self) -> str:
        return "shot" if self.shot_rb.isChecked() else "asset"

    def context(self) -> Context | None:
        p = self.project_cb.currentData()
        t = self.task_cb.currentData()
        if not p or not t:
            return None
        return Context(project=p, episode=self.episode_cb.currentData(), kind=self.kind, task_type=t)

    def _save(self) -> None:
        if self._restoring:
            return
        s = settings()
        p = self.project_cb.currentData()
        e = self.episode_cb.currentData()
        t = self.task_cb.currentData()
        s.setValue(f"{self.prefix}/project", p["id"] if p else "")
        s.setValue(f"{self.prefix}/episode", e["id"] if e else "")
        s.setValue(f"{self.prefix}/kind", self.kind)
        s.setValue(f"{self.prefix}/task_type", t["name"] if t else "")

    # -- loading ----------------------------------------------------------

    def reload(self) -> None:
        self._restoring = True
        self.busy.emit("Loading projects...")
        kind = settings().value(f"{self.prefix}/kind", "shot")
        self.shot_rb.blockSignals(True)
        (self.asset_rb if kind == "asset" else self.shot_rb).setChecked(True)
        self.shot_rb.blockSignals(False)
        run_async(self.service.projects, self._got_projects, self._failed)

    def _failed(self, msg: str) -> None:
        self._restoring = False
        self.busy.emit(f"Error: {msg}")

    def _got_projects(self, projects: list[dict]) -> None:
        self.projects = sorted(projects, key=lambda p: p["name"].lower())
        want = settings().value(f"{self.prefix}/project", "")
        self.project_cb.blockSignals(True)
        self.project_cb.clear()
        self.project_cb.addItem("Select a project...", None)
        for p in self.projects:
            self.project_cb.addItem(p["name"], p)
        idx = next((i for i in range(self.project_cb.count())
                    if (self.project_cb.itemData(i) or {}).get("id") == want), 0)
        if idx == 0 and len(self.projects) == 1:
            idx = 1
        self.project_cb.setCurrentIndex(idx)
        self.project_cb.blockSignals(False)
        self.busy.emit("")
        self._on_project()

    def _on_project(self) -> None:
        project = self.project_cb.currentData()
        self.episode_cb.blockSignals(True)
        self.episode_cb.clear()
        self.episode_cb.blockSignals(False)
        self.task_cb.blockSignals(True)
        self.task_cb.clear()
        self.task_cb.blockSignals(False)
        if not project:
            self._restoring = False
            self.context_changed.emit(None)
            return
        self.busy.emit(f"Loading {project['name']}...")
        svc, kind = self.service, self.kind

        def load():
            return (svc.episodes(project), svc.task_types(project, kind), svc.statuses(project))

        def got(res):
            if (self.project_cb.currentData() or {}).get("id") != project["id"]:
                return  # user moved on
            episodes, task_types, statuses = res
            self._fill_episodes(episodes)
            self._fill_task_types(task_types)
            self.statuses_loaded.emit(statuses)
            self.busy.emit("")
            self._restoring = False
            self._emit()

        run_async(load, got, self._failed)

    def _fill_episodes(self, episodes: list[dict]) -> None:
        self.episodes = episodes
        has = bool(episodes)
        self.episode_cb.setVisible(has)
        self.episode_lbl.setVisible(has)
        self.episode_cb.blockSignals(True)
        self.episode_cb.clear()
        if has:
            self.episode_cb.addItem("All episodes", None)
            for e in episodes:
                self.episode_cb.addItem(e["name"], e)
            want = settings().value(f"{self.prefix}/episode", "")
            idx = next((i for i in range(self.episode_cb.count())
                        if (self.episode_cb.itemData(i) or {}).get("id") == want), 0)
            self.episode_cb.setCurrentIndex(idx)
        self.episode_cb.blockSignals(False)

    def _fill_task_types(self, task_types: list[dict]) -> None:
        self.task_types = task_types
        prev = self.task_cb.currentData()
        want = prev["name"] if prev else settings().value(f"{self.prefix}/task_type", "")
        self.task_cb.blockSignals(True)
        self.task_cb.clear()
        if not task_types:
            self.task_cb.addItem(f"No {self.kind} task types in project", None)
        for t in task_types:
            self.task_cb.addItem(color_icon(t.get("color")), t["name"], t)
        idx = self.task_cb.findText(want, Qt.MatchFlag.MatchExactly)
        self.task_cb.setCurrentIndex(max(idx, 0))
        self.task_cb.blockSignals(False)

    def _on_kind(self) -> None:
        project = self.project_cb.currentData()
        if not project:
            return
        kind = self.kind
        run_async(
            lambda: self.service.task_types(project, kind),
            lambda tts: (self._fill_task_types(tts), self._emit()),
            self._failed,
        )

    def _emit(self, *_):
        if self._restoring:
            return
        self._save()
        self.context_changed.emit(self.context())
