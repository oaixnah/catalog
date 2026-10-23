from __future__ import annotations

import sqlite3
from typing import Any, Sequence, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Header, Static

import catalog

from . import data
from .screens import (
    BindScreen,
    ConfirmScreen,
    DetailScreen,
    HelpScreen,
    LinkFormScreen,
    ModelFormScreen,
    ProviderFormScreen,
    RelatedChoiceScreen,
    RelatedScreen,
)
from .widgets import ActionBar, CatalogChanged, CatalogTable, StatusBar, TabBar


class CatalogApp(App[None]):
    CSS_PATH = "styles.tcss"
    TITLE = "Ravel Catalog"
    EFFORTS = catalog.EFFORTS
    AUTHENTICATIONS = catalog.AUTHENTICATIONS
    PROTOCOLS = catalog.PROTOCOLS
    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
    ]

    def __init__(self, db_path: str, initial_view: str = "models") -> None:
        super().__init__()
        self.db_path = db_path
        self.initial_view = initial_view if initial_view in ("models", "providers", "links") else "models"
        self.conn: sqlite3.Connection | None = None
        self.main_screen: MainScreen | None = None

    def on_mount(self) -> None:
        self.conn = catalog.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        sequence = data.fetch_sequence(self.conn)
        self.sub_title = f"sequence {sequence}" if sequence is not None else "sequence -"
        self.main_screen = MainScreen(self.initial_view)
        self.push_screen(self.main_screen)
        problems = data.validation_problems(self.conn)
        if problems:
            self.notify(
                f"数据库有 {len(problems)} 个校验问题，运行 catalog-cli validate 查看",
                severity="warning",
                timeout=8,
            )

    def on_unmount(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("database connection is not ready")
        return self.conn

    def refresh_all(self) -> None:
        if self.main_screen is not None:
            self.main_screen.refresh_panes()

    def on_catalog_changed(self, event: CatalogChanged) -> None:
        self.refresh_all()
        for screen in self.screen_stack:
            reload = getattr(screen, "reload", None)
            if callable(reload):
                reload()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.exit()


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("1", "show_models", "Models", priority=True, show=False),
        Binding("2", "show_providers", "Providers", priority=True, show=False),
        Binding("3", "show_links", "Links", priority=True, show=False),
        Binding("right", "pane_detail", "Detail", priority=True, show=False),
        Binding("left", "pane_back", "Back", priority=True, show=False),
        Binding("space", "pane_select", "Select", priority=True, show=False),
        Binding("enter", "pane_action", "Actions", priority=True, show=False),
        Binding("tab", "pane_tab", "Switch action", priority=True, show=False),
        Binding("escape", "pane_escape", "Cancel", priority=True, show=False),
        Binding("d", "pane_delete", "Delete", priority=True, show=False),
        Binding("e", "pane_edit", "Edit", priority=True, show=False),
        Binding("m", "pane_related", "Related", priority=True, show=False),
        Binding("n", "pane_new", "New", priority=True, show=False),
        Binding("r", "pane_refresh", "Refresh", priority=True, show=False),
    ]

    def __init__(self, initial_view: str) -> None:
        super().__init__()
        self.active_view = initial_view

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield TabBar(id="tab-bar")
        with ContentSwitcher(initial=f"{self.active_view}-pane", id="views"):
            yield ModelListPane(id="models-pane", classes="list-pane")
            yield ProviderListPane(id="providers-pane", classes="list-pane")
            yield LinkListPane(id="links-pane", classes="list-pane")
        yield Static("", id="keybar")

    def on_mount(self) -> None:
        self.query_one(TabBar).set_active(self.active_view)
        self._set_keybar()
        self.current_pane().focus_table()

    def current_pane(self) -> EntityListPane:
        return self.query_one(f"#{self.active_view}-pane", EntityListPane)

    def refresh_panes(self) -> None:
        for pane in self.query(EntityListPane):
            pane.refresh_data()

    def _set_keybar(self) -> None:
        if self.active_view == "links":
            text = (
                "up/down move   right detail   left clear   space select   enter actions   "
                "d unbind   e edit   m related   n new link   r refresh   1/2/3 view   ? help   q quit"
            )
        else:
            text = (
                "up/down move   right detail   left clear   space select   enter actions   "
                "d delete   e edit   m related   n new   r refresh   1/2/3 view   ? help   q quit"
            )
        self.query_one("#keybar", Static).update(text)

    def _show_view(self, view: str) -> None:
        if view == self.active_view:
            self.current_pane().focus_table()
            return
        self.current_pane().cancel_actions(clear_selection=False)
        self.active_view = view
        self.query_one(ContentSwitcher).current = f"{view}-pane"
        self.query_one(TabBar).set_active(view)
        self._set_keybar()
        self.current_pane().focus_table()

    def action_show_models(self) -> None:
        self._show_view("models")

    def action_show_providers(self) -> None:
        self._show_view("providers")

    def action_show_links(self) -> None:
        self._show_view("links")

    def action_pane_detail(self) -> None:
        self.current_pane().open_detail()

    def action_pane_back(self) -> None:
        self.current_pane().back()

    def action_pane_select(self) -> None:
        self.current_pane().toggle_select()

    def action_pane_action(self) -> None:
        self.current_pane().enter_action()

    def action_pane_tab(self) -> None:
        self.current_pane().switch_action()

    def action_pane_escape(self) -> None:
        self.current_pane().back()

    def action_pane_delete(self) -> None:
        self.current_pane().request_delete()

    def action_pane_edit(self) -> None:
        self.current_pane().edit_current()

    def action_pane_related(self) -> None:
        self.current_pane().related_current()

    def action_pane_new(self) -> None:
        self.current_pane().create_new()

    def action_pane_refresh(self) -> None:
        pane = self.current_pane()
        pane.refresh_data()
        pane.set_message("refreshed")


class EntityListPane(Vertical):
    view_name = ""
    table_id = ""
    action_labels: tuple[str, str] = ("Delete", "Bind")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.selected: set[str] = set()
        self._records: dict[str, object] = {}
        self._message = ""
        self._action_mode = False
        self._action_index = 0

    def compose(self) -> ComposeResult:
        yield CatalogTable(id=self.table_id, classes="table")
        yield ActionBar(classes="action-bar")
        yield StatusBar(classes="status-bar")

    def on_mount(self) -> None:
        self.setup_columns()
        self.refresh_data()

    @property
    def catalog(self) -> CatalogApp:
        return cast(CatalogApp, self.app)

    @property
    def table(self) -> CatalogTable:
        return self.query_one(f"#{self.table_id}", CatalogTable)

    @property
    def action_bar(self) -> ActionBar:
        return self.query_one(ActionBar)

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    def setup_columns(self) -> None:
        raise NotImplementedError

    def load_records(self) -> list[object]:
        raise NotImplementedError

    def record_key(self, record: object) -> str:
        raise NotImplementedError

    def row_cells(self, record: object) -> list[object]:
        raise NotImplementedError

    def status_for(self, record: object) -> str:
        raise NotImplementedError

    def push_edit(self, record: object) -> None:
        raise NotImplementedError

    def push_new(self) -> None:
        raise NotImplementedError

    def prepare_rows(self) -> None:
        pass

    def focus_table(self) -> None:
        self.table.focus()

    def marker(self, key: str) -> Text:
        if key in self.selected:
            return Text("[x]", style="bold #fbbf24")
        return Text("[ ]", style="#64748b")

    def refresh_data(self) -> None:
        table = self.table
        current = table.current_key
        index = table.current_index()
        table.clear()
        records = self.load_records()
        self._records = {self.record_key(record): record for record in records}  # type: ignore[arg-type]
        self.selected &= set(self._records)
        self.prepare_rows()
        for record in records:
            key = self.record_key(record)
            table.add_row(*self.row_cells(record), key=key)
        if table.row_count:
            if current in self._records:
                target = current
            elif index >= 0:
                target = self.record_key(records[min(index, len(records) - 1)])
            else:
                target = self.record_key(records[0])
            table.focus_key(target)
        else:
            self.cancel_actions(clear_selection=False)
        self.update_status()

    def current_key(self) -> str | None:
        return self.table.current_key

    def current_record(self) -> object | None:
        key = self.current_key()
        return self._records.get(key) if key else None

    def target_keys(self) -> list[str]:
        if self.selected:
            return [key for key in self._records if key in self.selected]
        key = self.current_key()
        return [key] if key is not None else []

    def target_records(self) -> list[object]:
        return [self._records[key] for key in self.target_keys() if key in self._records]

    def status_for_current(self) -> str:
        record = self.current_record()
        return self.status_for(record) if record is not None else ""

    def update_status(self) -> None:
        self.status_bar.set_status(self.status_for_current(), len(self.selected), self._message)

    def set_message(self, message: str) -> None:
        self._message = message
        self.update_status()

    def on_data_table_row_highlighted(self) -> None:
        self.update_status()

    def _refresh_markers(self) -> None:
        for key in self._records:
            self.table.update_cell(key, "sel", self.marker(key))

    def toggle_select(self) -> None:
        key = self.current_key()
        if key is None:
            return
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self.table.update_cell(key, "sel", self.marker(key))
        self.update_status()

    def enter_action(self) -> None:
        if self._action_mode:
            if self._action_index == 0:
                self.request_delete()
            else:
                self.request_bind()
            return
        if not self._records:
            self.catalog.notify(f"没有可操作的 {self.view_name}", severity="warning")
            return
        self._action_mode = True
        self._action_index = 0
        self.action_bar.show_actions(self.action_labels, 0)
        self.update_status()

    def switch_action(self) -> None:
        if not self._action_mode:
            return
        self._action_index = (self._action_index + 1) % 2
        self.action_bar.set_active(self._action_index)

    def cancel_actions(self, *, clear_selection: bool = False) -> None:
        self._action_mode = False
        self._action_index = 0
        self.action_bar.hide_actions()
        if clear_selection:
            self.selected.clear()
            self._refresh_markers()
        self.update_status()

    def back(self) -> None:
        if self._action_mode:
            self.cancel_actions(clear_selection=False)
            return
        if self.selected:
            self.selected.clear()
            self._refresh_markers()
            self.update_status()
            return
        self.set_message("")

    def open_detail(self) -> None:
        key = self.current_key()
        if key is not None:
            self.catalog.push_screen(DetailScreen(self.view_name, key))

    def edit_current(self) -> None:
        record = self.current_record()
        if record is None:
            return
        if len(self.selected) > 1:
            self.catalog.notify("编辑只作用于当前行", severity="warning")
        self.push_edit(record)

    def request_delete(self) -> None:
        raise NotImplementedError

    def request_bind(self) -> None:
        raise NotImplementedError

    def related_current(self) -> None:
        record = self.current_record()
        if record is None:
            return
        if self.view_name in ("model", "provider"):
            self.catalog.push_screen(RelatedScreen(self.view_name, self.record_key(record)))
        elif self.view_name == "link":
            link = record
            if not isinstance(link, data.LinkRecord):
                return

            def chosen(choice: str | None) -> None:
                if choice == "model":
                    self.catalog.push_screen(RelatedScreen("model", link.model_ref))
                elif choice == "provider":
                    self.catalog.push_screen(RelatedScreen("provider", link.provider_id))

            self.catalog.push_screen(RelatedChoiceScreen(), chosen)

    def create_new(self) -> None:
        self.push_new()

    def _after_delete(self, message: str, keys: Sequence[str]) -> None:
        for key in keys:
            self.selected.discard(key)
        self._action_mode = False
        self.action_bar.hide_actions()
        self.post_message(CatalogChanged())
        self.catalog.notify(message)
        self.refresh_data()


class ModelListPane(EntityListPane):
    view_name = "model"
    table_id = "model-table"
    action_labels = ("Delete", "Bind")

    def setup_columns(self) -> None:
        table = self.table
        table.add_column("", key="sel", width=3)
        table.add_column("ID", key="id")
        table.add_column("DISPLAY NAME", key="name")
        table.add_column("CTX", key="context")
        table.add_column("OUT", key="output")
        table.add_column("EFFORTS", key="efforts")
        table.add_column("PROVIDERS", key="providers")

    def load_records(self) -> list[object]:
        return list(data.fetch_models(self.catalog.connection))

    def record_key(self, record: object) -> str:
        return record.id  # type: ignore[attr-defined]

    def row_cells(self, record: object) -> list[object]:
        model = record
        return [
            self.marker(model.id),  # type: ignore[attr-defined]
            model.id,  # type: ignore[attr-defined]
            model.display_name,  # type: ignore[attr-defined]
            model.context_limit,  # type: ignore[attr-defined]
            model.output_limit,  # type: ignore[attr-defined]
            ",".join(model.efforts) or "-",  # type: ignore[attr-defined]
            ",".join(model.provider_ids) or "-",  # type: ignore[attr-defined]
        ]

    def status_for(self, record: object) -> str:
        model = record
        return (
            f"model: {model.id}  |  {model.display_name}  |  ctx {model.context_limit}  "  # type: ignore[attr-defined]
            f"out {model.output_limit}  |  efforts {','.join(model.efforts) or '-'}  "  # type: ignore[attr-defined]
            f"|  providers {','.join(model.provider_ids) or '-'}"  # type: ignore[attr-defined]
        )

    def push_edit(self, record: object) -> None:
        self.catalog.push_screen(ModelFormScreen(record))  # type: ignore[arg-type]

    def push_new(self) -> None:
        self.catalog.push_screen(ModelFormScreen())

    def request_delete(self) -> None:
        keys = self.target_keys()
        if not keys:
            return
        efforts, links = data.fetch_cascade_counts(self.catalog.connection, kind="model", ids=keys)
        lines = [f"  {key}" for key in keys[:12]]
        if len(keys) > 12:
            lines.append(f"  ... and {len(keys) - 12} more")
        lines.extend(
            [
                "",
                f"Cascades: {efforts} reasoning efforts, {links} provider links",
                "This writes directly to catalog.sqlite.",
            ]
        )

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                deleted, _, _ = data.delete_models(self.catalog.connection, keys)
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._after_delete(f"deleted {deleted} model(s)", keys)

        self.catalog.push_screen(ConfirmScreen("DELETE MODEL(S)?", lines, "Delete"), confirmed)

    def request_bind(self) -> None:
        records = self.target_records()
        if not records:
            return
        self.cancel_actions(clear_selection=False)
        self.catalog.push_screen(BindScreen(kind="models", records=records))  # type: ignore[arg-type]


class ProviderListPane(EntityListPane):
    view_name = "provider"
    table_id = "provider-table"
    action_labels = ("Delete", "Bind")

    def setup_columns(self) -> None:
        table = self.table
        table.add_column("", key="sel", width=3)
        table.add_column("ID", key="id")
        table.add_column("DISPLAY NAME", key="name")
        table.add_column("AUTH", key="auth")
        table.add_column("BASE URL", key="base-url")
        table.add_column("MODELS", key="models")

    def load_records(self) -> list[object]:
        return list(data.fetch_providers(self.catalog.connection))

    def record_key(self, record: object) -> str:
        return record.id  # type: ignore[attr-defined]

    def row_cells(self, record: object) -> list[object]:
        provider = record
        return [
            self.marker(provider.id),  # type: ignore[attr-defined]
            provider.id,  # type: ignore[attr-defined]
            provider.display_name,  # type: ignore[attr-defined]
            provider.authentication,  # type: ignore[attr-defined]
            provider.base_url,  # type: ignore[attr-defined]
            ",".join(provider.model_ids) or "-",  # type: ignore[attr-defined]
        ]

    def status_for(self, record: object) -> str:
        provider = record
        return (
            f"provider: {provider.id}  |  {provider.display_name}  |  {provider.authentication}  "  # type: ignore[attr-defined]
            f"|  {provider.base_url}  |  models {','.join(provider.model_ids) or '-'}"  # type: ignore[attr-defined]
        )

    def push_edit(self, record: object) -> None:
        self.catalog.push_screen(ProviderFormScreen(record))  # type: ignore[arg-type]

    def push_new(self) -> None:
        self.catalog.push_screen(ProviderFormScreen())

    def request_delete(self) -> None:
        keys = self.target_keys()
        if not keys:
            return
        _, links = data.fetch_cascade_counts(self.catalog.connection, kind="provider", ids=keys)
        lines = [f"  {key}" for key in keys[:12]]
        if len(keys) > 12:
            lines.append(f"  ... and {len(keys) - 12} more")
        lines.extend(["", f"Cascades: {links} provider links", "This writes directly to catalog.sqlite."])

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                deleted, _ = data.delete_providers(self.catalog.connection, keys)
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._after_delete(f"deleted {deleted} provider(s)", keys)

        self.catalog.push_screen(ConfirmScreen("DELETE PROVIDER(S)?", lines, "Delete"), confirmed)

    def request_bind(self) -> None:
        records = self.target_records()
        if not records:
            return
        self.cancel_actions(clear_selection=False)
        self.catalog.push_screen(BindScreen(kind="providers", records=records))  # type: ignore[arg-type]


class LinkListPane(EntityListPane):
    view_name = "link"
    table_id = "link-table"
    action_labels = ("Unbind", "Bind")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._last_provider: str | None = None

    def setup_columns(self) -> None:
        table = self.table
        table.add_column("", key="sel", width=3)
        table.add_column("ProviderId", key="provider")
        table.add_column("modelId", key="model")
        table.add_column("协议", key="protocol")
        table.add_column("协议路径", key="path")

    def load_records(self) -> list[object]:
        return list(data.fetch_links(self.catalog.connection))

    def record_key(self, record: object) -> str:
        return record.key  # type: ignore[attr-defined]

    def prepare_rows(self) -> None:
        self._last_provider = None

    def row_cells(self, record: object) -> list[object]:
        link = record
        if link.provider_id != self._last_provider:  # type: ignore[attr-defined]
            provider_cell: object = Text(link.provider_id, style="bold #7dd3fc")  # type: ignore[attr-defined]
            self._last_provider = link.provider_id  # type: ignore[attr-defined]
        else:
            provider_cell = ""
        return [
            self.marker(link.key),  # type: ignore[attr-defined]
            provider_cell,
            link.model_id,  # type: ignore[attr-defined]
            link.protocol_type,  # type: ignore[attr-defined]
            link.request_path,  # type: ignore[attr-defined]
        ]

    def status_for(self, record: object) -> str:
        link = record
        return (
            f"link: {link.provider_id} -> {link.model_id}  |  ref {link.model_ref}  "  # type: ignore[attr-defined]
            f"|  {link.protocol_type} {link.request_path}"  # type: ignore[attr-defined]
        )

    def push_edit(self, record: object) -> None:
        self.catalog.push_screen(
            LinkFormScreen(
                record=record,  # type: ignore[arg-type]
                providers=data.fetch_providers(self.catalog.connection),
                models=data.fetch_models(self.catalog.connection),
            )
        )

    def push_new(self) -> None:
        self.catalog.push_screen(
            LinkFormScreen(
                providers=data.fetch_providers(self.catalog.connection),
                models=data.fetch_models(self.catalog.connection),
            )
        )

    def request_delete(self) -> None:
        keys = self.target_keys()
        if not keys:
            return
        pairs = [tuple(key.split("\x1f", 1)) for key in keys]
        lines = [f"  {provider} -> {model}" for provider, model in pairs[:12]]
        if len(pairs) > 12:
            lines.append(f"  ... and {len(pairs) - 12} more")
        lines.extend(["", "This writes directly to catalog.sqlite."])

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                deleted = data.delete_links(self.catalog.connection, pairs)  # type: ignore[arg-type]
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._after_delete(f"unbound {deleted} link(s)", keys)

        self.catalog.push_screen(ConfirmScreen("UNBIND LINK(S)?", lines, "Unbind"), confirmed)

    def request_bind(self) -> None:
        if self.selected:
            self.catalog.notify("Bind 会新建链接；已选行不参与本次操作", severity="warning")
        self.cancel_actions(clear_selection=False)
        self.push_new()
