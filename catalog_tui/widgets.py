from __future__ import annotations

from typing import Any, Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Checkbox, DataTable, Static

import catalog


class CatalogChanged(Message):
    pass


class CatalogTable(DataTable):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.show_cursor = True

    @property
    def current_key(self) -> str | None:
        if self.row_count == 0 or not self.is_valid_coordinate(self.cursor_coordinate):
            return None
        key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key
        return key.value

    def focus_key(self, key: str | None) -> None:
        if key is None or self.row_count == 0:
            return
        try:
            row = self.get_row_index(key)
        except Exception:
            return
        self.move_cursor(row=row, column=0, animate=False)

    def current_index(self) -> int:
        return self.cursor_row if self.row_count else -1


class TabBar(Static):
    def set_active(self, active: str) -> None:
        text = Text()
        for key, view, label in (
            ("1", "models", "Models"),
            ("2", "providers", "Providers"),
            ("3", "links", "Links"),
        ):
            if view == active:
                text.append(f" {key} {label} ", style="bold black on #7dd3fc")
            else:
                text.append(f" {key} {label} ", style="bold #94a3b8")
            text.append("  ")
        self.update(text)


class StatusBar(Static):
    def set_status(self, current: str = "", selected: int = 0, message: str = "") -> None:
        text = Text()
        if current:
            text.append(current, style="bold #e2e8f0")
        if selected:
            if text:
                text.append("  |  ", style="#475569")
            text.append(f"selected: {selected}", style="bold #fbbf24")
        if message:
            if text:
                text.append("  |  ", style="#475569")
            text.append(message, style="#86efac")
        if not text:
            text.append(" ")
        self.update(text)


class ActionBar(Static):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action_labels: tuple[str, str] = ("Delete", "Bind")
        self.active_action = 0
        self._actions_visible = False
        self.display = False

    def show_actions(self, labels: tuple[str, str], active: int = 0) -> None:
        self.action_labels = labels
        self.active_action = active
        self._actions_visible = True
        self.display = True
        self.refresh_actions()

    def hide_actions(self) -> None:
        self._actions_visible = False
        self.display = False

    @property
    def actions_visible(self) -> bool:
        return self._actions_visible

    def set_active(self, index: int) -> None:
        self.active_action = index % 2
        self.refresh_actions()

    def refresh_actions(self) -> None:
        text = Text("ACTIONS:  ", style="bold #94a3b8")
        for index, label in enumerate(self.action_labels):
            if index == self.active_action:
                text.append(f"[ {label} ]", style="bold black on #fbbf24")
            else:
                text.append(f"  {label}  ", style="bold #e2e8f0")
        text.append("    Tab switch   Enter run   Esc cancel", style="#64748b")
        self.update(text)


class EffortSelector(Vertical):
    def __init__(self, efforts: Iterable[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        current = [effort for effort in efforts if effort in catalog.EFFORTS]
        self._order = current + [effort for effort in catalog.EFFORTS if effort not in current]
        self._selected = set(current)

    def compose(self) -> ComposeResult:
        for effort in self._order:
            yield Checkbox(effort, value=effort in self._selected, id=f"effort-{effort}")

    def values(self) -> list[str]:
        return [
            effort
            for effort in self._order
            if self.query_one(f"#effort-{effort}", Checkbox).value
        ]
