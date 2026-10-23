from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Iterable, Sequence

import catalog

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from . import data
from .widgets import CatalogChanged, CatalogTable, EffortSelector

if TYPE_CHECKING:
    from .app import CatalogApp


def _marker(text: str, style: str) -> Text:
    return Text(text, style=style)


def _safe_defaults(protocol: str, path: str) -> tuple[str, str]:
    if protocol not in catalog.PROTOCOLS:
        protocol = data.DEFAULT_PROTOCOL
    if not path:
        path = data.PROTOCOL_PATHS.get(protocol, data.DEFAULT_REQUEST_PATH)
    return protocol, path


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Confirm", priority=True, show=False),
        Binding("n", "cancel", "Cancel", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, title: str, lines: Iterable[str], confirm_label: str = "Delete") -> None:
        super().__init__()
        self.dialog_title = title
        self.dialog_lines = list(lines)
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Static(Text("\n".join(self.dialog_lines)), classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant="error")
        yield Static("y confirm   n / Esc cancel", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True, show=False),
        Binding("q", "close", "Close", priority=True, show=False),
    ]

    HELP = (
        "GLOBAL\n"
        "  1 / 2 / 3      Models / Providers / Links\n"
        "  q              quit\n"
        "  ?              help\n"
        "\n"
        "LIST\n"
        "  up / down      move cursor\n"
        "  right          open detail\n"
        "  left           close actions / clear selection\n"
        "  space          select / unselect row\n"
        "  enter          open action bar (or run action)\n"
        "  tab            switch Delete / Bind in action bar\n"
        "  d              delete (unbind in Links)\n"
        "  e              edit current row\n"
        "  m              manage related model/provider\n"
        "  n              create new record\n"
        "  r              refresh\n"
        "  Esc            cancel action bar / selection\n"
        "\n"
        "DETAIL\n"
        "  left / Esc     back\n"
        "  e / d / m      edit / delete / related\n"
        "\n"
        "RELATED\n"
        "  space          mark bind or unbind\n"
        "  enter          apply pending changes\n"
        "  Esc            back\n"
        "\n"
        "FORM\n"
        "  Tab            next field\n"
        "  space          toggle switch / checkbox\n"
        "  Ctrl+S / F2    save\n"
        "  Esc            cancel\n"
    )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog help-dialog"):
            yield Static("KEYBOARD HELP", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.HELP)
            yield Static("Esc / q close", classes="dialog-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class RelatedChoiceScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("1", "model", "Model", priority=True, show=False),
        Binding("2", "provider", "Provider", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("MANAGE RELATIONS", classes="dialog-title")
            yield Static(
                "This link belongs to a Model and a Provider.\nChoose which side to manage.",
                classes="dialog-body",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Model -> Providers", id="model", variant="primary")
                yield Button("Provider -> Models", id="provider")
                yield Button("Cancel", id="cancel")
        yield Static("1 model side   2 provider side   Esc cancel", classes="dialog-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_model(self) -> None:
        self.dismiss("model")

    def action_provider(self) -> None:
        self.dismiss("provider")

    def action_cancel(self) -> None:
        self.dismiss(None)


class BaseFormScreen(Screen[bool]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True, show=False),
        Binding("f2", "save", "Save", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.screen_title = title

    def compose(self) -> ComposeResult:
        yield Static(self.screen_title, classes="screen-title")
        with VerticalScroll(classes="form-scroll"):
            with Grid(classes="form-grid"):
                yield from self.form_fields()
            yield Static("", id="form-error", classes="error")
        with Horizontal(classes="form-buttons"):
            yield Button("Save", id="save", variant="primary")
            yield Button("Cancel", id="cancel")
        yield Static("Tab next   Space toggle   Ctrl+S / F2 save   Esc cancel", classes="keybar")

    def on_mount(self) -> None:
        for widget in self.query("Input, Select, Switch"):
            if getattr(widget, "disabled", False):
                continue
            widget.focus()
            break

    def form_fields(self) -> ComposeResult:
        raise NotImplementedError

    def persist(self) -> None:
        raise NotImplementedError

    def show_error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(Text(message, style="bold #f87171"))

    def action_save(self) -> None:
        self.query_one("#form-error", Static).update("")
        try:
            self.persist()
        except data.CatalogValidationError as error:
            self.show_error(str(error))
            return
        except sqlite3.Error as error:
            self.show_error(f"database error: {error}")
            return
        self.post_message(CatalogChanged())
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()


class ModelFormScreen(BaseFormScreen):
    def __init__(self, record: data.ModelRecord | None = None) -> None:
        super().__init__("NEW MODEL" if record is None else f"EDIT MODEL {record.id}")
        self.record = record

    def form_fields(self) -> ComposeResult:
        record = self.record
        yield Label("ID", classes="form-label")
        yield Input(
            value="" if record is None else record.id,
            id="model-id",
            disabled=record is not None,
        )
        yield Label("Display name", classes="form-label")
        yield Input(value="" if record is None else record.display_name, id="model-name")
        yield Label("Context limit", classes="form-label")
        yield Input(value="" if record is None else str(record.context_limit), id="model-context")
        yield Label("Input limit", classes="form-label")
        yield Input(
            value="" if record is None or record.input_limit is None else str(record.input_limit),
            id="model-input",
            placeholder="empty = null",
        )
        yield Label("Output limit", classes="form-label")
        yield Input(value="" if record is None else str(record.output_limit), id="model-output")
        yield Label("Tool calling", classes="form-label")
        yield Switch(value=True if record is None else record.tool_calling, id="model-tools")
        yield Label("Structured output", classes="form-label")
        yield Switch(value=True if record is None else record.structured_output, id="model-structured")
        yield Label("Temperature", classes="form-label")
        yield Switch(value=False if record is None else record.temperature, id="model-temperature")
        yield Label("Reasoning efforts", classes="form-section span-2")
        yield EffortSelector(
            [] if record is None else record.efforts,
            id="model-efforts",
            classes="span-2 effort-selector",
        )

    def persist(self) -> None:
        model_id = self.query_one("#model-id", Input).value
        display_name = self.query_one("#model-name", Input).value
        context_limit = self.query_one("#model-context", Input).value
        input_limit = self.query_one("#model-input", Input).value
        output_limit = self.query_one("#model-output", Input).value
        tool_calling = self.query_one("#model-tools", Switch).value
        structured_output = self.query_one("#model-structured", Switch).value
        temperature = self.query_one("#model-temperature", Switch).value
        efforts = self.query_one("#model-efforts", EffortSelector).values()
        if self.record is None:
            data.create_model(
                self.catalog.connection,
                model_id=model_id,
                display_name=display_name,
                context_limit=context_limit,
                input_limit=input_limit,
                output_limit=output_limit,
                tool_calling=tool_calling,
                structured_output=structured_output,
                temperature=temperature,
                efforts=efforts,
            )
            self.catalog.notify(f"created model '{model_id}'")
        else:
            data.update_model(
                self.catalog.connection,
                model_id=self.record.id,
                display_name=display_name,
                context_limit=context_limit,
                input_limit=input_limit,
                output_limit=output_limit,
                tool_calling=tool_calling,
                structured_output=structured_output,
                temperature=temperature,
                efforts=efforts,
            )
            self.catalog.notify(f"updated model '{self.record.id}'")

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]


class ProviderFormScreen(BaseFormScreen):
    def __init__(self, record: data.ProviderRecord | None = None) -> None:
        super().__init__("NEW PROVIDER" if record is None else f"EDIT PROVIDER {record.id}")
        self.record = record

    def form_fields(self) -> ComposeResult:
        record = self.record
        yield Label("ID", classes="form-label")
        yield Input(
            value="" if record is None else record.id,
            id="provider-id",
            disabled=record is not None,
        )
        yield Label("Display name", classes="form-label")
        yield Input(value="" if record is None else record.display_name, id="provider-name")
        yield Label("Authentication", classes="form-label")
        authentication = (
            record.authentication
            if record is not None and record.authentication in self.catalog.AUTHENTICATIONS
            else self.catalog.AUTHENTICATIONS[0]
        )
        yield Select(
            [(value, value) for value in self.catalog.AUTHENTICATIONS],
            value=authentication,
            allow_blank=False,
            id="provider-auth",
        )
        yield Label("Base URL", classes="form-label")
        yield Input(value="" if record is None else record.base_url, id="provider-base-url")
        yield Label("Model list path", classes="form-label")
        yield Input(
            value="" if record is None else record.model_list_path,
            id="provider-list-path",
        )

    def persist(self) -> None:
        provider_id = self.query_one("#provider-id", Input).value
        display_name = self.query_one("#provider-name", Input).value
        authentication = str(self.query_one("#provider-auth", Select).value)
        base_url = self.query_one("#provider-base-url", Input).value
        model_list_path = self.query_one("#provider-list-path", Input).value
        if self.record is None:
            data.create_provider(
                self.catalog.connection,
                provider_id=provider_id,
                display_name=display_name,
                authentication=authentication,
                base_url=base_url,
                model_list_path=model_list_path,
            )
            self.catalog.notify(f"created provider '{provider_id}'")
        else:
            data.update_provider(
                self.catalog.connection,
                provider_id=self.record.id,
                display_name=display_name,
                authentication=authentication,
                base_url=base_url,
                model_list_path=model_list_path,
            )
            self.catalog.notify(f"updated provider '{self.record.id}'")

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]


class LinkFormScreen(BaseFormScreen):
    def __init__(
        self,
        *,
        record: data.LinkRecord | None = None,
        providers: Sequence[data.ProviderRecord],
        models: Sequence[data.ModelRecord],
        initial_provider_id: str | None = None,
        initial_model_ref: str | None = None,
    ) -> None:
        super().__init__("NEW LINK" if record is None else f"EDIT LINK {record.provider_id} -> {record.model_id}")
        self.record = record
        self.providers = list(providers)
        self.models = list(models)
        self.provider_id = record.provider_id if record else initial_provider_id
        self.model_ref = record.model_ref if record else initial_model_ref
        if self.provider_id is None and self.providers:
            self.provider_id = self.providers[0].id
        if self.model_ref is None and self.models:
            self.model_ref = self.models[0].id
        self._last_model_ref = self.model_ref

    def form_fields(self) -> ComposeResult:
        record = self.record
        provider_options = [
            (f"{provider.id}  {provider.display_name}", provider.id) for provider in self.providers
        ] or [("(no providers)", "")]
        model_options = [
            (f"{model.id}  {model.display_name}", model.id) for model in self.models
        ] or [("(no models)", "")]
        yield Label("Provider", classes="form-label")
        yield Select(
            provider_options,
            value=self.provider_id or Select.BLANK,
            allow_blank=True,
            disabled=record is not None,
            id="link-provider",
        )
        yield Label("Model", classes="form-label")
        yield Select(
            model_options,
            value=self.model_ref or Select.BLANK,
            allow_blank=True,
            id="link-model",
        )
        yield Label("API model id", classes="form-label")
        yield Input(
            value=(record.model_id if record else (self.model_ref or "")),
            id="link-api-model-id",
        )
        if record is not None:
            protocol_default = record.protocol_type
            path_default = record.request_path
        else:
            protocol_default, path_default = data.DEFAULT_PROTOCOL, data.DEFAULT_REQUEST_PATH
            if self.provider_id:
                protocol_default, path_default = data.most_common_link_defaults(
                    self.catalog.connection, self.provider_id
                )
        protocol_default, path_default = _safe_defaults(protocol_default, path_default)
        yield Label("Protocol", classes="form-label")
        yield Select(
            [(value, value) for value in self.catalog.PROTOCOLS],
            value=protocol_default,
            allow_blank=False,
            id="link-protocol",
        )
        yield Label("Request path", classes="form-label")
        yield Input(value=path_default, id="link-request-path")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "link-model":
            if event.value is Select.BLANK or not event.value:
                return
            new_model = str(event.value)
            api_input = self.query_one("#link-api-model-id", Input)
            if api_input.value in ("", self._last_model_ref or ""):
                api_input.value = new_model
            self._last_model_ref = new_model
        elif event.select.id == "link-provider" and self.record is None:
            if event.value is Select.BLANK or not event.value:
                return
            provider_id = str(event.value)
            if provider_id:
                protocol, path = _safe_defaults(
                    *data.most_common_link_defaults(self.catalog.connection, provider_id)
                )
                self.query_one("#link-protocol", Select).value = protocol
                self.query_one("#link-request-path", Input).value = path

    def persist(self) -> None:
        provider_select = self.query_one("#link-provider", Select)
        model_select = self.query_one("#link-model", Select)
        if provider_select.is_blank() or not provider_select.value:
            raise data.CatalogValidationError("请选择 provider")
        if model_select.is_blank() or not model_select.value:
            raise data.CatalogValidationError("请选择 model")
        provider_id = str(provider_select.value)
        api_model_id = self.query_one("#link-api-model-id", Input).value
        model_ref = str(model_select.value)
        protocol_type = str(self.query_one("#link-protocol", Select).value)
        request_path = self.query_one("#link-request-path", Input).value
        if self.record is None:
            data.create_link(
                self.catalog.connection,
                provider_id=provider_id,
                api_model_id=api_model_id,
                model_ref=model_ref,
                protocol_type=protocol_type,
                request_path=request_path,
            )
            self.catalog.notify(f"created link {provider_id} -> {api_model_id}")
        else:
            data.update_link(
                self.catalog.connection,
                provider_id=self.record.provider_id,
                old_model_id=self.record.model_id,
                api_model_id=api_model_id,
                model_ref=model_ref,
                protocol_type=protocol_type,
                request_path=request_path,
            )
            self.catalog.notify(f"updated link {self.record.provider_id} -> {self.record.model_id}")

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]


class DetailScreen(Screen[None]):
    BINDINGS = [
        Binding("left", "back", "Back", priority=True, show=False),
        Binding("escape", "back", "Back", priority=True, show=False),
        Binding("e", "edit", "Edit", priority=True, show=False),
        Binding("d", "delete", "Delete", priority=True, show=False),
        Binding("m", "related", "Related", priority=True, show=False),
        Binding("r", "reload", "Refresh", priority=True, show=False),
    ]

    def __init__(self, kind: str, key: str) -> None:
        super().__init__()
        self.kind = kind
        self.key = key

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-title", classes="screen-title")
        with VerticalScroll():
            yield Static("", id="detail-body", classes="detail-body")
        yield Static(
            "e edit   d delete   m related   left / Esc back   q quit",
            classes="keybar",
        )

    def on_mount(self) -> None:
        self.reload()

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]

    def _model(self) -> data.ModelRecord | None:
        return data.fetch_model(self.catalog.connection, self.key)

    def _provider(self) -> data.ProviderRecord | None:
        return data.fetch_provider(self.catalog.connection, self.key)

    def _link(self) -> data.LinkRecord | None:
        provider_id, _, model_id = self.key.partition("\x1f")
        return data.fetch_link(self.catalog.connection, provider_id, model_id)

    def reload(self) -> None:
        text = Text()
        title = ""
        if self.kind == "model":
            model = self._model()
            if model is None:
                title = "MODEL NOT FOUND"
                text.append(f"model '{self.key}' no longer exists", style="bold #f87171")
            else:
                title = f"MODEL DETAIL  {model.id}"
                self._render_model(text, model)
        elif self.kind == "provider":
            provider = self._provider()
            if provider is None:
                title = "PROVIDER NOT FOUND"
                text.append(f"provider '{self.key}' no longer exists", style="bold #f87171")
            else:
                title = f"PROVIDER DETAIL  {provider.id}"
                self._render_provider(text, provider)
        elif self.kind == "link":
            link = self._link()
            if link is None:
                title = "LINK NOT FOUND"
                text.append(f"link '{self.key}' no longer exists", style="bold #f87171")
            else:
                title = f"LINK DETAIL  {link.provider_id} -> {link.model_id}"
                self._render_link(text, link)
        self.query_one("#detail-title", Static).update(title)
        self.query_one("#detail-body", Static).update(text)

    def _line(self, text: Text, label: str, value: object, *, style: str = "#e2e8f0") -> None:
        text.append(f"{label:<18}", style="bold #7dd3fc")
        text.append(f"{value}\n", style=style)

    def _render_model(self, text: Text, record: data.ModelRecord) -> None:
        self._line(text, "id", record.id)
        self._line(text, "display_name", record.display_name)
        self._line(text, "context_limit", record.context_limit)
        self._line(text, "input_limit", "-" if record.input_limit is None else record.input_limit)
        self._line(text, "output_limit", record.output_limit)
        self._line(text, "tool_calling", "yes" if record.tool_calling else "no")
        self._line(text, "structured_output", "yes" if record.structured_output else "no")
        self._line(text, "temperature", "yes" if record.temperature else "no")
        self._line(text, "efforts", ",".join(record.efforts) or "-")
        text.append("\nLINKED PROVIDERS\n", style="bold #fbbf24")
        links = data.fetch_model_links(self.catalog.connection, record.id)
        if not links:
            text.append("  -\n", style="#94a3b8")
        for link in links:
            text.append(
                f"  {link.provider_id:<18} {link.model_id:<24} {link.protocol_type:<20} {link.request_path}\n",
                style="#cbd5e1",
            )

    def _render_provider(self, text: Text, record: data.ProviderRecord) -> None:
        self._line(text, "id", record.id)
        self._line(text, "display_name", record.display_name)
        self._line(text, "authentication", record.authentication)
        self._line(text, "base_url", record.base_url)
        self._line(text, "model_list_path", record.model_list_path)
        text.append("\nLINKED MODELS\n", style="bold #fbbf24")
        links = data.fetch_provider_links(self.catalog.connection, record.id)
        if not links:
            text.append("  -\n", style="#94a3b8")
        for link in links:
            text.append(
                f"  {link.model_id:<24} ref={link.model_ref:<22} {link.protocol_type:<20} {link.request_path}\n",
                style="#cbd5e1",
            )

    def _render_link(self, text: Text, record: data.LinkRecord) -> None:
        provider = self._provider_by_id(record.provider_id)
        model = self._model_by_id(record.model_ref)
        self._line(text, "provider_id", record.provider_id)
        self._line(text, "provider_name", provider.display_name if provider else "-")
        self._line(text, "base_url", provider.base_url if provider else "-")
        self._line(text, "model_id", record.model_id)
        self._line(text, "model_ref", record.model_ref)
        self._line(text, "model_name", model.display_name if model else "-")
        self._line(text, "protocol_type", record.protocol_type)
        self._line(text, "request_path", record.request_path)

    def _provider_by_id(self, provider_id: str) -> data.ProviderRecord | None:
        return data.fetch_provider(self.catalog.connection, provider_id)

    def _model_by_id(self, model_id: str) -> data.ModelRecord | None:
        return data.fetch_model(self.catalog.connection, model_id)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self.reload()

    def action_edit(self) -> None:
        if self.kind == "model":
            model = self._model()
            if model is not None:
                self.app.push_screen(ModelFormScreen(model))
        elif self.kind == "provider":
            provider = self._provider()
            if provider is not None:
                self.app.push_screen(ProviderFormScreen(provider))
        elif self.kind == "link":
            link = self._link()
            if link is not None:
                self.app.push_screen(
                    LinkFormScreen(
                        record=link,
                        providers=data.fetch_providers(self.catalog.connection),
                        models=data.fetch_models(self.catalog.connection),
                    )
                )

    def action_delete(self) -> None:
        if self.kind == "model":
            self._delete_model()
        elif self.kind == "provider":
            self._delete_provider()
        elif self.kind == "link":
            self._delete_link()

    def _finish_delete(self, message: str) -> None:
        self.catalog.notify(message)
        self.post_message(CatalogChanged())
        self.app.pop_screen()

    def _delete_model(self) -> None:
        model = self._model()
        if model is None:
            self.notify("model 已不存在", severity="warning")
            return
        efforts, links = data.fetch_cascade_counts(
            self.catalog.connection, kind="model", ids=[model.id]
        )
        lines = [
            f"  {model.id}  {model.display_name}",
            "",
            f"Cascades: {efforts} reasoning efforts, {links} provider links",
            "This writes directly to catalog.sqlite.",
        ]

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                data.delete_models(self.catalog.connection, [model.id])
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._finish_delete(f"deleted model '{model.id}'")

        self.app.push_screen(ConfirmScreen("DELETE MODEL?", lines, "Delete"), confirmed)

    def _delete_provider(self) -> None:
        provider = self._provider()
        if provider is None:
            self.notify("provider 已不存在", severity="warning")
            return
        _, links = data.fetch_cascade_counts(
            self.catalog.connection, kind="provider", ids=[provider.id]
        )
        lines = [
            f"  {provider.id}  {provider.display_name}",
            "",
            f"Cascades: {links} provider links",
            "This writes directly to catalog.sqlite.",
        ]

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                data.delete_providers(self.catalog.connection, [provider.id])
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._finish_delete(f"deleted provider '{provider.id}'")

        self.app.push_screen(ConfirmScreen("DELETE PROVIDER?", lines, "Delete"), confirmed)

    def _delete_link(self) -> None:
        link = self._link()
        if link is None:
            self.notify("link 已不存在", severity="warning")
            return
        lines = [
            f"  {link.provider_id} -> {link.model_id}",
            "",
            "This writes directly to catalog.sqlite.",
        ]

        def confirmed(result: bool | None) -> None:
            if not result:
                return
            try:
                data.delete_links(self.catalog.connection, [(link.provider_id, link.model_id)])
            except (data.CatalogValidationError, sqlite3.Error) as error:
                self.catalog.notify(str(error), severity="error")
                return
            self._finish_delete(f"unbound {link.provider_id} -> {link.model_id}")

        self.app.push_screen(ConfirmScreen("UNBIND LINK?", lines, "Unbind"), confirmed)

    def action_related(self) -> None:
        if self.kind == "model":
            self.app.push_screen(RelatedScreen("model", self.key))
        elif self.kind == "provider":
            self.app.push_screen(RelatedScreen("provider", self.key))
        elif self.kind == "link":
            link = self._link()
            if link is None:
                return

            def chosen(choice: str | None) -> None:
                if choice == "model":
                    self.app.push_screen(RelatedScreen("model", link.model_ref))
                elif choice == "provider":
                    self.app.push_screen(RelatedScreen("provider", link.provider_id))

            self.app.push_screen(RelatedChoiceScreen(), chosen)


class RelatedScreen(Screen[None]):
    BINDINGS = [
        Binding("space", "toggle_pending", "Toggle", priority=True, show=False),
        Binding("enter", "apply", "Apply", priority=True, show=False),
        Binding("escape", "back", "Back", priority=True, show=False),
        Binding("r", "reload", "Refresh", priority=True, show=False),
        Binding("b", "bind_only", "Bind", priority=True, show=False),
        Binding("d", "unbind_only", "Unbind", priority=True, show=False),
        Binding("u", "unbind_only", "Unbind", priority=True, show=False),
    ]

    def __init__(self, kind: str, entity_id: str) -> None:
        super().__init__()
        self.kind = kind
        self.entity_id = entity_id
        self._records: dict[str, data.RelatedRecord] = {}
        self._desired: dict[str, bool] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="related-title", classes="screen-title")
        yield CatalogTable(id="related-table")
        yield Static("", id="related-status", classes="status-bar")
        yield Static(
            "space toggle   b bind   d/u unbind   enter apply   Esc back   r refresh",
            classes="keybar",
        )

    def on_mount(self) -> None:
        table = self.query_one("#related-table", CatalogTable)
        table.add_column("ST", key="st", width=4)
        table.add_column("ID", key="id")
        table.add_column("DISPLAY NAME", key="name")
        table.add_column("DETAIL", key="detail")
        self.reload()
        table.focus()

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]

    def _title(self) -> str:
        if self.kind == "model":
            return f"RELATED PROVIDERS for model: {self.entity_id}"
        return f"RELATED MODELS for provider: {self.entity_id}"

    def _load(self) -> list[data.RelatedRecord]:
        if self.kind == "model":
            return data.fetch_related_providers(self.catalog.connection, self.entity_id)
        return data.fetch_related_models(self.catalog.connection, self.entity_id)

    def reload(self) -> None:
        table = self.query_one("#related-table", CatalogTable)
        current = table.current_key
        table.clear()
        records = self._load()
        self._records = {record.entity_id: record for record in records}
        for record in records:
            self._desired[record.entity_id] = record.linked
        for record in records:
            table.add_row(
                self._marker(record),
                record.entity_id,
                record.display_name,
                record.detail,
                key=record.entity_id,
            )
        table.focus_key(current)
        self.query_one("#related-title", Static).update(self._title())
        self.update_status()

    def _marker(self, record: data.RelatedRecord) -> Text:
        desired = self._desired.get(record.entity_id, record.linked)
        if desired and not record.linked:
            return _marker("[+]", "bold #4ade80")
        if not desired and record.linked:
            return _marker("[-]", "bold #f87171")
        if record.linked:
            return _marker("[x]", "bold #fbbf24")
        return _marker("[ ]", "#64748b")

    def update_status(self) -> None:
        table = self.query_one("#related-table", CatalogTable)
        pending_bind = 0
        pending_unbind = 0
        for record in self._records.values():
            desired = self._desired.get(record.entity_id, record.linked)
            if desired and not record.linked:
                pending_bind += 1
            elif not desired and record.linked:
                pending_unbind += 1
        status = f"pending: bind {pending_bind}, unbind {pending_unbind}"
        current = table.current_key
        if current and current in self._records:
            record = self._records[current]
            status += f"  |  {record.entity_id}: {record.detail}"
        self.query_one("#related-status", Static).update(status)

    def on_data_table_row_highlighted(self) -> None:
        self.update_status()

    def _current(self) -> data.RelatedRecord | None:
        key = self.query_one("#related-table", CatalogTable).current_key
        return self._records.get(key) if key else None

    def action_toggle_pending(self) -> None:
        record = self._current()
        if record is None:
            return
        self._desired[record.entity_id] = not self._desired.get(record.entity_id, record.linked)
        self.query_one("#related-table", CatalogTable).update_cell(
            record.entity_id, "st", self._marker(record)
        )
        self.update_status()

    def action_bind_only(self) -> None:
        record = self._current()
        if record is None or record.linked:
            return
        self._desired[record.entity_id] = True
        self.query_one("#related-table", CatalogTable).update_cell(
            record.entity_id, "st", self._marker(record)
        )
        self.update_status()

    def action_unbind_only(self) -> None:
        record = self._current()
        if record is None or not record.linked:
            return
        self._desired[record.entity_id] = False
        self.query_one("#related-table", CatalogTable).update_cell(
            record.entity_id, "st", self._marker(record)
        )
        self.update_status()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self.reload()

    def action_apply(self) -> None:
        self._apply_changes()

    @work(exclusive=True)
    async def _apply_changes(self) -> None:
        pending_bind = [
            record
            for record in self._records.values()
            if self._desired.get(record.entity_id, record.linked) and not record.linked
        ]
        pending_unbind = [
            record
            for record in self._records.values()
            if not self._desired.get(record.entity_id, record.linked) and record.linked
        ]
        if not pending_bind and not pending_unbind:
            self.notify("没有待应用的关联变更", severity="warning")
            return

        configs: list[dict[str, str]] = []
        for record in pending_bind:
            if self.kind == "model":
                protocol, path = _safe_defaults(
                    *data.most_common_link_defaults(self.catalog.connection, record.entity_id)
                )
                default_api_id = self.entity_id
                label = f"model {self.entity_id} -> provider {record.entity_id}"
            else:
                protocol, path = _safe_defaults(
                    *data.most_common_link_defaults(self.catalog.connection, self.entity_id)
                )
                default_api_id = record.entity_id
                label = f"provider {self.entity_id} -> model {record.entity_id}"
            result = await self.catalog.push_screen_wait(
                RelatedBindScreen(
                    label=label,
                    target_id=record.entity_id,
                    api_model_id=default_api_id,
                    protocol_type=protocol,
                    request_path=path,
                )
            )
            if result is None:
                self.notify("已取消，未修改任何关联", severity="warning")
                return
            configs.append(result)

        lines = []
        if pending_unbind:
            lines.append("Unbind:")
            lines.extend(f"  {record.entity_id}" for record in pending_unbind)
            if any(record.link_count > 1 for record in pending_unbind):
                lines.append("  (a target with multiple links will lose all of them)")
        if configs:
            lines.append("Bind:")
            lines.extend(f"  {config['target_id']}  {config['protocol_type']} {config['request_path']}" for config in configs)
        lines.append("")
        lines.append("Applied atomically in one transaction.")

        confirmed = await self.catalog.push_screen_wait(
            ConfirmScreen("APPLY RELATION CHANGES?", lines, "Apply")
        )
        if not confirmed:
            return
        try:
            bound, unbound = data.apply_related_changes(
                self.catalog.connection,
                kind=self.kind,
                entity_id=self.entity_id,
                binds=configs,
                unbind_ids=[record.entity_id for record in pending_unbind],
            )
        except (data.CatalogValidationError, sqlite3.Error) as error:
            self.notify(str(error), severity="error")
            return
        self.post_message(CatalogChanged())
        self.notify(f"applied: bound {bound}, unbound {unbound}")
        self.reload()


class RelatedBindScreen(Screen[dict[str, str] | None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True, show=False),
        Binding("f2", "save", "Save", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        *,
        label: str,
        target_id: str,
        api_model_id: str,
        protocol_type: str,
        request_path: str,
    ) -> None:
        super().__init__()
        self.label = label
        self.target_id = target_id
        self.api_model_id = api_model_id
        self.protocol_type = protocol_type
        self.request_path = request_path

    def compose(self) -> ComposeResult:
        yield Static(f"BIND  {self.label}", classes="screen-title")
        with VerticalScroll(classes="form-scroll"):
            with Grid(classes="form-grid"):
                yield Label("Target", classes="form-label")
                yield Static(self.target_id)
                yield Label("API model id", classes="form-label")
                yield Input(value=self.api_model_id, id="related-api-model-id")
                yield Label("Protocol", classes="form-label")
                yield Select(
                    [(value, value) for value in self.catalog.PROTOCOLS],
                    value=self.protocol_type,
                    allow_blank=False,
                    id="related-protocol",
                )
                yield Label("Request path", classes="form-label")
                yield Input(value=self.request_path, id="related-request-path")
            yield Static("", id="form-error", classes="error")
        with Horizontal(classes="form-buttons"):
            yield Button("Bind", id="save", variant="primary")
            yield Button("Cancel", id="cancel")
        yield Static("Tab next   Ctrl+S / F2 bind   Esc cancel", classes="keybar")

    def on_mount(self) -> None:
        self.query_one("#related-api-model-id", Input).focus()

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(Text(message, style="bold #f87171"))

    def action_save(self) -> None:
        api_model_id = self.query_one("#related-api-model-id", Input).value.strip()
        request_path = self.query_one("#related-request-path", Input).value.strip()
        if not api_model_id:
            self._error("API model id 不能为空")
            return
        if not request_path:
            self._error("request path 不能为空")
            return
        self.dismiss(
            {
                "target_id": self.target_id,
                "api_model_id": api_model_id,
                "protocol_type": str(self.query_one("#related-protocol", Select).value),
                "request_path": request_path,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()


class BindScreen(BaseFormScreen):
    def __init__(
        self,
        *,
        kind: str,
        records: Sequence[data.ModelRecord] | Sequence[data.ProviderRecord],
    ) -> None:
        if kind == "models":
            title = f"BIND {len(records)} MODEL(S) TO PROVIDER"
        else:
            title = f"BIND {len(records)} PROVIDER(S) TO MODEL"
        super().__init__(title)
        self.kind = kind
        self.record_ids = [getattr(record, "id") for record in records]
        self._single = len(self.record_ids) == 1

    def _providers(self) -> list[data.ProviderRecord]:
        return data.fetch_providers(self.catalog.connection)

    def _models(self) -> list[data.ModelRecord]:
        return data.fetch_models(self.catalog.connection)

    def form_fields(self) -> ComposeResult:
        if self.kind == "models":
            options = [(f"{p.id}  {p.display_name}", p.id) for p in self._providers()]
            target_label = "Target provider"
        else:
            options = [(f"{m.id}  {m.display_name}", m.id) for m in self._models()]
            target_label = "Target model"
        options = options or [("(none)", "")]
        first = options[0][1]
        if self.kind == "models" and first:
            protocol, path = _safe_defaults(
                *data.most_common_link_defaults(self.catalog.connection, first)
            )
        else:
            protocol, path = data.DEFAULT_PROTOCOL, data.DEFAULT_REQUEST_PATH
        yield Label(target_label, classes="form-label")
        yield Select(options, value=first or Select.BLANK, allow_blank=True, id="bind-target")
        yield Label("Protocol", classes="form-label")
        yield Select(
            [(value, value) for value in self.catalog.PROTOCOLS],
            value=protocol,
            allow_blank=False,
            id="bind-protocol",
        )
        yield Label("Request path", classes="form-label")
        yield Input(value=path, id="bind-request-path")
        if self._single:
            if self.kind == "models":
                default_api_id = self.record_ids[0]
            else:
                default_api_id = first
            yield Label("API model id", classes="form-label")
            yield Input(value=default_api_id, id="bind-api-model-id")
        else:
            yield Label("API model id", classes="form-label")
            yield Static("each selected record uses its own id")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "bind-target":
            return
        if event.value is Select.BLANK or not event.value:
            return
        target = str(event.value)
        if self.kind == "models":
            protocol, path = _safe_defaults(
                *data.most_common_link_defaults(self.catalog.connection, target)
            )
        else:
            protocol, path = data.DEFAULT_PROTOCOL, data.DEFAULT_REQUEST_PATH
            if self.record_ids:
                protocol, path = _safe_defaults(
                    *data.most_common_link_defaults(self.catalog.connection, self.record_ids[0])
                )
        self.query_one("#bind-protocol", Select).value = protocol
        self.query_one("#bind-request-path", Input).value = path
        if self._single and self.kind == "providers":
            self.query_one("#bind-api-model-id", Input).value = target

    def persist(self) -> None:
        target_select = self.query_one("#bind-target", Select)
        if target_select.is_blank() or not target_select.value:
            raise data.CatalogValidationError("请选择绑定目标")
        target = str(target_select.value)
        protocol = str(self.query_one("#bind-protocol", Select).value)
        path = self.query_one("#bind-request-path", Input).value
        api_model_id = (
            self.query_one("#bind-api-model-id", Input).value
            if self._single
            else ""
        )
        if self._single and not api_model_id.strip():
            raise data.CatalogValidationError("API model id 不能为空")

        if self.kind == "models":
            pairs = [
                (target, record_id, api_model_id if self._single else record_id)
                for record_id in self.record_ids
            ]
        else:
            pairs = [
                (record_id, target, api_model_id if self._single else target)
                for record_id in self.record_ids
            ]
        bound, skipped = data.bind_many(
            self.catalog.connection,
            pairs=pairs,
            protocol_type=protocol,
            request_path=path,
        )
        if skipped:
            self.catalog.notify(
                f"bound {bound} link(s), skipped {len(skipped)} existing",
                severity="warning" if bound == 0 else "information",
            )
        else:
            self.catalog.notify(f"bound {bound} link(s)")

    @property
    def catalog(self) -> CatalogApp:
        return self.app  # type: ignore[return-value]
