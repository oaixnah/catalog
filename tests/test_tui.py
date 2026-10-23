from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import catalog

from tui import data
from tui.app import CatalogApp, MainScreen
from tui.screens import (
    BindScreen,
    ConfirmScreen,
    LinkFormScreen,
    ModelFormScreen,
    ProviderFormScreen,
    RelatedBindScreen,
    RelatedScreen,
)
from tui.widgets import ActionBar, CatalogTable
from textual.widgets import Checkbox, Input, Select, Static


def build_database(path: str) -> None:
    conn = catalog.connect(path, init=True)
    try:
        conn.execute("INSERT INTO registry (format_version, sequence) VALUES (2, 0)")
        conn.executemany(
            "INSERT INTO provider (id, display_name, authentication, base_url, model_list_path) VALUES (?, ?, ?, ?, ?)",
            [
                ("zeta", "Zeta", "x-api-key", "https://zeta.example", "/models"),
                ("alpha", "Alpha", "bearer-api-key", "https://alpha.example", "/v1/models"),
                ("Beta", "Beta", "bearer-api-key", "https://beta.example", "/v1/models"),
            ],
        )
        conn.executemany(
            """INSERT INTO model (id, display_name, context_limit, input_limit, output_limit,
                                  tool_calling, structured_output, temperature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("zeta-model", "Zeta Model", 200, None, 20, 1, 1, 0),
                ("alpha-model", "Alpha Model", 100, 50, 10, 1, 1, 1),
                ("Beta-model", "Beta 模型", 300, 100, 30, 0, 1, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO model_reasoning_effort (model_id, effort, sort_order) VALUES (?, ?, ?)",
            [
                ("zeta-model", "max", 0),
                ("zeta-model", "low", 1),
                ("alpha-model", "low", 0),
                ("alpha-model", "high", 1),
            ],
        )
        conn.executemany(
            """INSERT INTO provider_model (provider_id, model_id, model_ref, protocol_type, request_path)
               VALUES (?, ?, ?, ?, ?)""",
            [
                ("zeta", "zeta-model", "zeta-model", "openai-compatible", "/v1/chat/completions"),
                ("alpha", "zeta-model", "zeta-model", "openai-compatible", "/v1/chat/completions"),
                ("alpha", "alpha-model", "alpha-model", "openai-responses", "/v1/responses"),
                ("Beta", "beta-api", "Beta-model", "anthropic-messages", "/v1/messages"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def plain_rows(table: CatalogTable) -> list[list[object]]:
    rows = []
    for index in range(table.row_count):
        row = []
        for cell in table.get_row_at(index):
            row.append(getattr(cell, "plain", cell))
        rows.append(row)
    return rows


class DataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "catalog.sqlite")
        build_database(self.db_path)
        self.conn = catalog.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_sorting_and_grouping(self) -> None:
        models = data.fetch_models(self.conn)
        providers = data.fetch_providers(self.conn)
        links = data.fetch_links(self.conn)
        self.assertEqual([model.id for model in models], ["alpha-model", "Beta-model", "zeta-model"])
        self.assertEqual([provider.id for provider in providers], ["alpha", "Beta", "zeta"])
        self.assertEqual(
            [(link.provider_id, link.model_id) for link in links],
            [
                ("alpha", "alpha-model"),
                ("alpha", "zeta-model"),
                ("Beta", "beta-api"),
                ("zeta", "zeta-model"),
            ],
        )
        groups = data.group_links_by_provider(links)
        self.assertEqual([provider for provider, _ in groups], ["alpha", "Beta", "zeta"])
        self.assertEqual(len(groups[0][1]), 2)

    def test_export_order_remains_rowid(self) -> None:
        document = catalog.build_document(self.conn)
        self.assertEqual(
            [model["id"] for model in document["models"]["models"]],
            ["zeta-model", "alpha-model", "Beta-model"],
        )
        self.assertEqual(
            [provider["id"] for provider in document["providers"]["providers"]],
            ["zeta", "alpha", "Beta"],
        )
        self.assertEqual(
            [model["modelId"] for model in document["providers"]["providers"][1]["models"]],
            ["zeta-model", "alpha-model"],
        )

    def test_model_crud_and_cascade(self) -> None:
        data.create_model(
            self.conn,
            model_id="new-model",
            display_name="New Model",
            context_limit="1000",
            input_limit="",
            output_limit="100",
            tool_calling=True,
            structured_output=False,
            temperature=True,
            efforts=["high", "low"],
        )
        model = data.fetch_model(self.conn, "new-model")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.efforts, ("high", "low"))
        self.assertIsNone(model.input_limit)

        data.update_model(
            self.conn,
            model_id="new-model",
            display_name="Renamed",
            context_limit=2000,
            input_limit="500",
            output_limit=200,
            tool_calling=False,
            structured_output=True,
            temperature=False,
            efforts=["low"],
        )
        model = data.fetch_model(self.conn, "new-model")
        assert model is not None
        self.assertEqual(model.display_name, "Renamed")
        self.assertEqual(model.efforts, ("low",))
        self.assertEqual(model.input_limit, 500)

        data.create_link(
            self.conn,
            provider_id="alpha",
            api_model_id="new-model",
            model_ref="new-model",
            protocol_type="openai-compatible",
            request_path="/v1/chat/completions",
        )
        deleted, efforts, links = data.delete_models(self.conn, ["new-model"])
        self.assertEqual((deleted, efforts, links), (1, 1, 1))
        self.assertIsNone(data.fetch_model(self.conn, "new-model"))

    def test_provider_crud(self) -> None:
        data.create_provider(
            self.conn,
            provider_id="new-provider",
            display_name="New Provider",
            authentication="bearer-api-key",
            base_url="https://new.example",
            model_list_path="/v1/models",
            logo_source="https://logos.example/{provider}.svg",
        )
        provider = data.fetch_provider(self.conn, "new-provider")
        assert provider is not None
        self.assertEqual(provider.display_name, "New Provider")
        self.assertEqual(provider.logo_source, "https://logos.example/{provider}.svg")
        data.update_provider(
            self.conn,
            provider_id="new-provider",
            display_name="Renamed Provider",
            authentication="x-api-key",
            base_url="https://renamed.example",
            model_list_path="/models",
            logo_source="./assets/{provider}.svg",
        )
        provider = data.fetch_provider(self.conn, "new-provider")
        assert provider is not None
        self.assertEqual(provider.authentication, "x-api-key")
        self.assertEqual(provider.logo_source, "./assets/{provider}.svg")
        deleted, links = data.delete_providers(self.conn, ["new-provider"])
        self.assertEqual((deleted, links), (1, 0))

    def test_link_update_preserves_rowid(self) -> None:
        before = self.conn.execute(
            "SELECT rowid FROM provider_model WHERE provider_id = 'alpha' AND model_id = 'alpha-model'"
        ).fetchone()[0]
        data.update_link(
            self.conn,
            provider_id="alpha",
            old_model_id="alpha-model",
            api_model_id="alpha-model-api",
            model_ref="alpha-model",
            protocol_type="openai-responses",
            request_path="/v1/responses",
        )
        row = self.conn.execute(
            "SELECT rowid, model_id FROM provider_model WHERE provider_id = 'alpha' AND model_ref = 'alpha-model'"
        ).fetchone()
        self.assertEqual(row["rowid"], before)
        self.assertEqual(row["model_id"], "alpha-model-api")

    def test_bind_many_and_related_changes(self) -> None:
        bound, skipped = data.bind_many(
            self.conn,
            pairs=[
                ("zeta", "Beta-model", "beta-zeta"),
                ("alpha", "alpha-model", "duplicate"),
                ("Beta", "alpha-model", "alpha-beta"),
            ],
            protocol_type="openai-compatible",
            request_path="/v1/chat/completions",
        )
        self.assertEqual(bound, 2)
        self.assertEqual(len(skipped), 1)

        bound, unbound = data.apply_related_changes(
            self.conn,
            kind="model",
            entity_id="Beta-model",
            binds=[
                {
                    "target_id": "alpha",
                    "api_model_id": "beta-alpha",
                    "protocol_type": "openai-compatible",
                    "request_path": "/v1/chat/completions",
                }
            ],
            unbind_ids=["Beta"],
        )
        self.assertEqual((bound, unbound), (1, 1))

        bound, unbound = data.apply_related_changes(
            self.conn,
            kind="provider",
            entity_id="alpha",
            binds=[],
            unbind_ids=["zeta-model"],
        )
        self.assertEqual((bound, unbound), (0, 1))

    def test_validation_rolls_back(self) -> None:
        with self.assertRaises(data.CatalogValidationError):
            data.create_model(
                self.conn,
                model_id="bad",
                display_name="Bad",
                context_limit="not-an-int",
                input_limit="",
                output_limit="10",
                tool_calling=True,
                structured_output=True,
                temperature=False,
                efforts=[],
            )
        self.assertIsNone(data.fetch_model(self.conn, "bad"))

        with self.assertRaises(data.CatalogValidationError):
            data.create_link(
                self.conn,
                provider_id="alpha",
                api_model_id="alpha-model",
                model_ref="alpha-model",
                protocol_type="openai-compatible",
                request_path="/duplicate",
            )

    def test_defaults_and_counts(self) -> None:
        self.assertEqual(
            data.most_common_link_defaults(self.conn, "alpha"),
            ("openai-compatible", "/v1/chat/completions"),
        )
        self.assertEqual(
            data.most_common_link_defaults(self.conn, "missing"),
            (data.DEFAULT_PROTOCOL, data.DEFAULT_REQUEST_PATH),
        )
        efforts, links = data.fetch_cascade_counts(self.conn, kind="model", ids=["zeta-model"])
        self.assertEqual((efforts, links), (2, 2))
        _, links = data.fetch_cascade_counts(self.conn, kind="provider", ids=["zeta"])
        self.assertEqual(links, 1)

    def test_related_records(self) -> None:
        providers = data.fetch_related_providers(self.conn, "zeta-model")
        self.assertEqual([record.entity_id for record in providers], ["alpha", "Beta", "zeta"])
        self.assertEqual([record.linked for record in providers], [True, False, True])
        models = data.fetch_related_models(self.conn, "alpha")
        self.assertEqual([record.entity_id for record in models], ["alpha-model", "Beta-model", "zeta-model"])
        self.assertEqual([record.linked for record in models], [True, False, True])


class UiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "catalog.sqlite")
        build_database(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def wait_for_screen(self, pilot, screen_type, attempts: int = 30):
        for _ in range(attempts):
            await pilot.pause(0.05)
            if isinstance(self.app.screen, screen_type):
                return self.app.screen
        self.fail(f"screen {screen_type.__name__} did not appear; got {type(self.app.screen).__name__}")

    async def test_lists_navigation_and_help(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            self.assertEqual([row[1] for row in plain_rows(table)], ["alpha-model", "Beta-model", "zeta-model"])
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "DetailScreen")
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "MainScreen")

            await pilot.press("2")
            await pilot.pause()
            provider_table = self.app.screen.query_one("#provider-table", CatalogTable)
            self.assertEqual([row[1] for row in plain_rows(provider_table)], ["alpha", "Beta", "zeta"])

            await pilot.press("3")
            await pilot.pause()
            link_table = self.app.screen.query_one("#link-table", CatalogTable)
            rows = plain_rows(link_table)
            self.assertEqual([row[1] for row in rows], ["alpha", "", "Beta", "zeta"])
            self.assertEqual([row[2] for row in rows], ["alpha-model", "zeta-model", "beta-api", "zeta-model"])

            await pilot.press("question_mark")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "HelpScreen")

    async def test_tab_cycles_views(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            self.assertIsInstance(self.app.screen, MainScreen)
            assert isinstance(self.app.screen, MainScreen)
            self.assertEqual(self.app.screen.active_view, "models")

            for expected in ("providers", "links", "models"):
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(self.app.screen.active_view, expected)
                self.assertEqual(
                    self.app.screen.query_one("#views").current, f"{expected}-pane"
                )
                self.assertIs(self.app.focused, self.app.screen.current_pane().table)

            for expected in ("links", "providers", "models"):
                await pilot.press("shift+tab")
                await pilot.pause()
                self.assertEqual(self.app.screen.active_view, expected)
                self.assertEqual(
                    self.app.screen.query_one("#views").current, f"{expected}-pane"
                )
                self.assertIs(self.app.focused, self.app.screen.current_pane().table)

    async def test_action_bar_and_delete_confirmation(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("alpha-model")
            await pilot.press("space")
            table.focus_key("zeta-model")
            await pilot.press("space")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, MainScreen)
            assert isinstance(self.app.screen, MainScreen)
            pane = self.app.screen.current_pane()
            self.assertEqual(pane.selected, {"alpha-model", "zeta-model"})

            await pilot.press("enter")
            await pilot.pause()
            bar = pane.query_one(ActionBar)
            self.assertTrue(bar.actions_visible)
            self.assertEqual(bar.active_action, 0)
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(bar.active_action, 1)
            self.assertEqual(self.app.screen.active_view, "models")
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(bar.active_action, 0)
            self.assertEqual(self.app.screen.active_view, "models")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(bar.actions_visible)
            self.assertEqual(pane.selected, {"alpha-model", "zeta-model"})

            await pilot.press("d")
            await self.wait_for_screen(pilot, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM model").fetchone()[0], 3)
            finally:
                conn.close()

            await pilot.press("d")
            await self.wait_for_screen(pilot, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM model").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM model_reasoning_effort").fetchone()[0], 0
                )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM provider_model").fetchone()[0], 1
                )
            finally:
                conn.close()

    async def test_detail_delete(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("alpha-model")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "DetailScreen")
            await pilot.press("d")
            await self.wait_for_screen(pilot, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "MainScreen")
            conn = catalog.connect(self.db_path)
            try:
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM model WHERE id = 'alpha-model'").fetchone()
                )
            finally:
                conn.close()

    async def test_detail_edit_refreshes_detail(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("zeta-model")
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("e")
            form = await self.wait_for_screen(pilot, ModelFormScreen)
            form.query_one("#model-name", Input).value = "Zeta Detail Renamed"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(type(self.app.screen).__name__, "DetailScreen")
            body = self.app.screen.query_one("#detail-body", Static).render()
            self.assertIn("Zeta Detail Renamed", str(body))

    async def test_edit_model_preserves_effort_order(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("zeta-model")
            await pilot.press("e")
            await self.wait_for_screen(pilot, ModelFormScreen)
            screen = self.app.screen
            screen.query_one("#model-name", Input).value = "Zeta Renamed"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertEqual(
                    conn.execute("SELECT display_name FROM model WHERE id = 'zeta-model'").fetchone()[0],
                    "Zeta Renamed",
                )
                self.assertEqual(
                    [row[0] for row in conn.execute(
                        "SELECT effort FROM model_reasoning_effort WHERE model_id = 'zeta-model' ORDER BY sort_order"
                    )],
                    ["max", "low"],
                )
            finally:
                conn.close()

    async def test_add_model_and_provider(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            form = await self.wait_for_screen(pilot, ModelFormScreen)
            form.query_one("#model-id", Input).value = "added-model"
            form.query_one("#model-name", Input).value = "Added Model"
            form.query_one("#model-context", Input).value = "1234"
            form.query_one("#model-output", Input).value = "56"
            form.query_one("#effort-high", Checkbox).value = True
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM model WHERE id = 'added-model'").fetchone()
                )
                self.assertEqual(
                    [row[0] for row in conn.execute(
                        "SELECT effort FROM model_reasoning_effort WHERE model_id = 'added-model'"
                    )],
                    ["high"],
                )
            finally:
                conn.close()

            await pilot.press("2")
            await pilot.pause()
            await pilot.press("n")
            form = await self.wait_for_screen(pilot, ProviderFormScreen)
            form.query_one("#provider-id", Input).value = "added-provider"
            form.query_one("#provider-name", Input).value = "Added Provider"
            form.query_one("#provider-base-url", Input).value = "https://added.example"
            form.query_one("#provider-list-path", Input).value = "/v1/models"
            self.assertEqual(form.query_one("#provider-logo-kind", Select).value, "default")
            self.assertTrue(form.query_one("#provider-logo-value", Input).disabled)
            await pilot.pause()
            with mock.patch("catalog.sync_provider_logo"):
                await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT logo_source FROM provider WHERE id = 'added-provider'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["logo_source"], "default")
            finally:
                conn.close()

    async def test_provider_logo_local_file(self) -> None:
        svg_path = os.path.join(self.temp.name, "source.svg")
        with open(svg_path, "w", encoding="utf-8") as handle:
            handle.write('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>')
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("n")
            form = await self.wait_for_screen(pilot, ProviderFormScreen)
            form.query_one("#provider-id", Input).value = "local-logo"
            form.query_one("#provider-name", Input).value = "Local Logo"
            form.query_one("#provider-base-url", Input).value = "https://local.example"
            form.query_one("#provider-list-path", Input).value = "/v1/models"
            form.query_one("#provider-logo-kind", Select).value = "file"
            await pilot.pause()
            self.assertFalse(form.query_one("#provider-logo-value", Input).disabled)
            form.query_one("#provider-logo-value", Input).value = svg_path
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT logo_source FROM provider WHERE id = 'local-logo'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["logo_source"], svg_path)
            finally:
                conn.close()
            logo_dir = os.path.join(self.temp.name, "logo")
            light_path = os.path.join(logo_dir, "local-logo.svg")
            dark_path = os.path.join(logo_dir, "local-logo_dark.svg")
            self.assertTrue(os.path.isfile(light_path))
            self.assertTrue(os.path.isfile(dark_path))
            with open(light_path, "rb") as handle:
                self.assertEqual(
                    handle.read(), b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
                )
            with open(dark_path, encoding="utf-8") as handle:
                self.assertIn('fill="#CED0D6"', handle.read())

    async def test_link_merge_and_edit_keeps_rowid(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            table = self.app.screen.query_one("#link-table", CatalogTable)
            rows = plain_rows(table)
            self.assertEqual(rows[1][1], "")
            table.focus_key("alpha\x1falpha-model")
            conn = catalog.connect(self.db_path)
            try:
                before = conn.execute(
                    "SELECT rowid FROM provider_model WHERE provider_id = 'alpha' AND model_id = 'alpha-model'"
                ).fetchone()[0]
            finally:
                conn.close()
            await pilot.press("e")
            await self.wait_for_screen(pilot, LinkFormScreen)
            self.app.screen.query_one("#link-api-model-id", Input).value = "alpha-model-api"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                after = conn.execute(
                    "SELECT rowid FROM provider_model WHERE provider_id = 'alpha' AND model_id = 'alpha-model-api'"
                ).fetchone()[0]
                self.assertEqual(after, before)
            finally:
                conn.close()

    async def test_related_bind_and_unbind(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("zeta-model")
            await pilot.press("m")
            related = await self.wait_for_screen(pilot, RelatedScreen)
            related_table = related.query_one("#related-table", CatalogTable)
            related_table.focus_key("zeta")
            await pilot.press("d")
            related_table.focus_key("Beta")
            await pilot.press("space")
            await pilot.press("enter")
            bind_screen = await self.wait_for_screen(pilot, RelatedBindScreen)
            self.assertEqual(bind_screen.query_one("#related-api-model-id", Input).value, "zeta-model")
            await pilot.press("ctrl+s")
            confirm = await self.wait_for_screen(pilot, ConfirmScreen)
            self.assertIsInstance(confirm, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(0.2)
            conn = catalog.connect(self.db_path)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM provider_model WHERE provider_id = 'zeta' AND model_ref = 'zeta-model'"
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM provider_model WHERE provider_id = 'Beta' AND model_ref = 'zeta-model'"
                    ).fetchone()
                )
            finally:
                conn.close()

    async def test_bulk_bind_and_add_link(self) -> None:
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            table = self.app.screen.query_one("#model-table", CatalogTable)
            table.focus_key("alpha-model")
            await pilot.press("space")
            table.focus_key("Beta-model")
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.press("tab")
            await pilot.press("enter")
            bind_screen = await self.wait_for_screen(pilot, BindScreen)
            bind_screen.query_one("#bind-target", Select).value = "zeta"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM provider_model WHERE provider_id = 'zeta' AND model_ref IN ('alpha-model', 'Beta-model')"
                    ).fetchone()[0],
                    2,
                )
            finally:
                conn.close()

            await pilot.press("3")
            await pilot.pause()
            await pilot.press("n")
            form = await self.wait_for_screen(pilot, LinkFormScreen)
            form.query_one("#link-provider", Select).value = "zeta"
            await pilot.pause()
            form.query_one("#link-model", Select).value = "Beta-model"
            await pilot.pause()
            form.query_one("#link-api-model-id", Input).value = "beta-zeta-2"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            conn = catalog.connect(self.db_path)
            try:
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM provider_model WHERE provider_id = 'zeta' AND model_id = 'beta-zeta-2'"
                    ).fetchone()
                )
            finally:
                conn.close()

    async def test_navigation_does_not_change_export(self) -> None:
        conn = catalog.connect(self.db_path)
        try:
            before = catalog.render_document(catalog.build_document(conn))
        finally:
            conn.close()
        self.app = CatalogApp(self.db_path)
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("left")
            await pilot.press("2")
            await pilot.press("right")
            await pilot.press("left")
            await pilot.press("3")
            await pilot.press("right")
            await pilot.press("left")
            await pilot.pause()
        conn = catalog.connect(self.db_path)
        try:
            after = catalog.render_document(catalog.build_document(conn))
        finally:
            conn.close()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
