from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from unittest import mock

import catalog

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M0 0"/></svg>'
CUSTOM_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="none" d="M2 2"/></svg>'


def build_database(path: str) -> None:
    conn = catalog.connect(path, init=True)
    try:
        conn.execute(
            "INSERT INTO registry (format_version, sequence) VALUES (?, 0)",
            (catalog.FORMAT_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()


def run_cli(arguments: list[str]) -> tuple[str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        catalog.main(arguments)
    return stdout.getvalue(), stderr.getvalue()


class LogoSourceTests(unittest.TestCase):
    def test_validate_logo_source(self) -> None:
        self.assertEqual(catalog.validate_logo_source(None), "default")
        self.assertEqual(catalog.validate_logo_source("models.dev"), "default")
        self.assertEqual(
            catalog.validate_logo_source("https://example.com/{provider}.svg"),
            "https://example.com/{provider}.svg",
        )
        self.assertEqual(
            catalog.validate_logo_source("https://example.com/logo.svg"),
            "https://example.com/logo.svg",
        )
        self.assertEqual(catalog.validate_logo_source("./assets/logo.svg"), "./assets/logo.svg")
        with self.assertRaises(catalog.LogoError):
            catalog.validate_logo_source("")
        with self.assertRaises(catalog.LogoError):
            catalog.validate_logo_source("https://example.com/{other}.svg")
        with self.assertRaises(catalog.LogoError):
            catalog.validate_logo_source("https://example.com/{provider!r}.svg")
        with self.assertRaises(catalog.LogoError):
            catalog.validate_logo_source("ftp://example.com/logo.svg")
        with self.assertRaises(catalog.LogoError):
            catalog.validate_logo_source("https://example.com/a b.svg")

    def test_resolve_logo_source(self) -> None:
        self.assertEqual(
            catalog.resolve_logo_source("default", "openai"),
            "https://models.dev/logos/openai.svg",
        )
        self.assertEqual(
            catalog.resolve_logo_source("https://example.com/{provider}.svg", "a b"),
            "https://example.com/a%20b.svg",
        )
        self.assertEqual(
            catalog.resolve_logo_source("assets/{provider}.svg", "openai"),
            os.path.join(catalog.SCRIPT_DIR, "assets/{provider}.svg"),
        )
        absolute = os.path.join(tempfile.gettempdir(), "logo.svg")
        self.assertEqual(catalog.resolve_logo_source(absolute, "openai"), absolute)

    def test_logo_paths_and_dir(self) -> None:
        self.assertEqual(
            catalog.logo_file_paths("openai", "/tmp/logo"),
            ("/tmp/logo/openai.svg", "/tmp/logo/openai_dark.svg"),
        )
        self.assertEqual(catalog.logo_dir_for_db("/tmp/catalog.sqlite"), "/tmp/logo")
        with self.assertRaises(catalog.LogoError):
            catalog.logo_file_paths("../escape", "/tmp/logo")

    def test_dark_svg_recolors_paths(self) -> None:
        source = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<path fill="currentColor" d="M0 0"/>'
            b'<path style="fill:red;stroke:blue" d="M1 1"/>'
            b'<circle fill="red" cx="1" cy="1" r="1"/>'
            b"</svg>"
        )
        dark = catalog.dark_svg_bytes(source)
        root = ET.fromstring(dark)
        paths = [element for element in root.iter() if catalog._local_name(element.tag) == "path"]
        self.assertEqual([path.get("fill") for path in paths], ["#CED0D6", "#CED0D6"])
        self.assertEqual(paths[1].get("style"), "fill:#CED0D6;stroke:blue")
        circles = [element for element in root.iter() if catalog._local_name(element.tag) == "circle"]
        self.assertEqual(circles[0].get("fill"), "red")
        self.assertNotIn(b"ns0:", dark)

    def test_dark_svg_rejects_non_svg(self) -> None:
        with self.assertRaises(catalog.LogoError):
            catalog.dark_svg_bytes(b"<html></html>")
        with self.assertRaises(catalog.LogoError):
            catalog.dark_svg_bytes(b"not xml")

    def test_fetch_logo_bytes_from_url(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = SVG
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            data = catalog.fetch_logo_bytes("https://example.com/{provider}.svg", "openai")
        self.assertEqual(data, SVG)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/openai.svg")

    def test_fetch_logo_bytes_url_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(catalog.LogoError):
                catalog.fetch_logo_bytes("https://example.com/logo.svg", "openai")

    def test_sync_from_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_path = os.path.join(temp, "source.svg")
            with open(source_path, "wb") as handle:
                handle.write(SVG)
            logo_dir = os.path.join(temp, "logo")
            light, dark = catalog.sync_provider_logo("openai", source_path, logo_dir)
            with open(light, "rb") as handle:
                self.assertEqual(handle.read(), SVG)
            with open(dark, "rb") as handle:
                self.assertIn(b'fill="#CED0D6"', handle.read())

            os.remove(source_path)
            catalog.sync_provider_logo("openai", source_path, logo_dir)
            os.remove(dark)
            catalog.sync_provider_logo("openai", source_path, logo_dir)
            self.assertTrue(os.path.isfile(dark))
            with self.assertRaises(catalog.LogoError):
                catalog.sync_provider_logo("openai", source_path, logo_dir, force=True)

    def test_sync_rejects_unsafe_provider_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_path = os.path.join(temp, "source.svg")
            with open(source_path, "wb") as handle:
                handle.write(SVG)
            with self.assertRaises(catalog.LogoError):
                catalog.sync_provider_logo("../openai", source_path, os.path.join(temp, "logo"))

    def test_delete_provider_logos(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_path = os.path.join(temp, "source.svg")
            with open(source_path, "wb") as handle:
                handle.write(SVG)
            logo_dir = os.path.join(temp, "logo")
            light, dark = catalog.sync_provider_logo("openai", source_path, logo_dir)
            self.assertEqual(catalog.delete_provider_logos("openai", logo_dir), [light, dark])
            self.assertEqual(catalog.delete_provider_logos("openai", logo_dir), [])

    def test_schema_migration_adds_logo_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = os.path.join(temp, "catalog.sqlite")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """CREATE TABLE provider (
                        id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        authentication TEXT NOT NULL,
                        base_url TEXT NOT NULL,
                        model_list_path TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path)
                       VALUES ('openai', 'OpenAI', 'bearer-api-key', 'https://api.openai.com', '/v1/models')"""
                )
                conn.commit()
            finally:
                conn.close()
            conn = catalog.connect(db_path)
            try:
                row = conn.execute("SELECT logo_source FROM provider WHERE id = 'openai'").fetchone()
                self.assertEqual(row["logo_source"], "default")
            finally:
                conn.close()

    def test_validation_rejects_bad_logo_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = os.path.join(temp, "catalog.sqlite")
            build_database(db_path)
            conn = catalog.connect(db_path)
            try:
                conn.execute(
                    """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path, logo_source)
                       VALUES ('bad', 'Bad', 'bearer-api-key', 'https://bad.example', '/models', 'https://x/{other}.svg')"""
                )
                conn.commit()
                problems = catalog.collect_problems(conn)
            finally:
                conn.close()
        self.assertTrue(any("unsupported logo URL placeholder" in problem for problem in problems))

    def test_logo_source_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = os.path.join(temp, "catalog.sqlite")
            build_database(db_path)
            conn = catalog.connect(db_path)
            try:
                conn.execute(
                    """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path, logo_source)
                       VALUES ('openai', 'OpenAI', 'bearer-api-key', 'https://api.openai.com', '/models',
                               'https://example.com/{provider}.svg')"""
                )
                conn.commit()
                provider = catalog.build_document(conn)["providers"]["providers"][0]
            finally:
                conn.close()
        self.assertEqual(
            set(provider),
            {"id", "displayName", "baseUrl", "modelList", "models"},
        )


class LogoCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "catalog.sqlite")
        build_database(self.db_path)
        self.source_path = os.path.join(self.temp.name, "source.svg")
        with open(self.source_path, "wb") as handle:
            handle.write(SVG)
        self.logo_dir = os.path.join(self.temp.name, "logo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_provider(self, source: str | None = None) -> tuple[str, str]:
        arguments = [
            "--db",
            self.db_path,
            "provider",
            "add",
            "--id",
            "local",
            "--name",
            "Local",
            "--authentication",
            "bearer-api-key",
            "--base-url",
            "https://local.example",
            "--model-list-path",
            "/v1/models",
        ]
        if source is not None:
            arguments.extend(["--logo-source", source])
        return run_cli(arguments)

    def test_add_provider_with_local_logo(self) -> None:
        stdout, stderr = self.add_provider(self.source_path)
        self.assertIn("added provider 'local'", stdout)
        self.assertEqual(stderr, "")
        conn = catalog.connect(self.db_path)
        try:
            row = conn.execute("SELECT logo_source FROM provider WHERE id = 'local'").fetchone()
            self.assertEqual(row["logo_source"], self.source_path)
        finally:
            conn.close()
        with open(os.path.join(self.logo_dir, "local.svg"), "rb") as handle:
            self.assertEqual(handle.read(), SVG)
        with open(os.path.join(self.logo_dir, "local_dark.svg"), "rb") as handle:
            self.assertIn(b'fill="#CED0D6"', handle.read())

    def test_add_provider_warns_but_saves_when_logo_missing(self) -> None:
        missing = os.path.join(self.temp.name, "missing.svg")
        stdout, stderr = self.add_provider(missing)
        self.assertIn("added provider 'local'", stdout)
        self.assertIn("warning: logo not synced", stderr)
        conn = catalog.connect(self.db_path)
        try:
            row = conn.execute("SELECT logo_source FROM provider WHERE id = 'local'").fetchone()
            self.assertEqual(row["logo_source"], missing)
        finally:
            conn.close()
        self.assertFalse(os.path.exists(os.path.join(self.logo_dir, "local.svg")))

    def test_set_logo_source(self) -> None:
        with open(self.source_path, "wb") as handle:
            handle.write(CUSTOM_SVG)
        self.add_provider()
        stdout, stderr = run_cli(
            ["--db", self.db_path, "provider", "set", "local", "--logo-source", self.source_path]
        )
        self.assertIn("updated provider 'local'", stdout)
        self.assertEqual(stderr, "")
        with open(os.path.join(self.logo_dir, "local.svg"), "rb") as handle:
            self.assertEqual(handle.read(), CUSTOM_SVG)

    def test_logo_sync_force_and_remove(self) -> None:
        self.add_provider(self.source_path)
        with open(self.source_path, "wb") as handle:
            handle.write(CUSTOM_SVG)
        run_cli(["--db", self.db_path, "logo", "sync", "local"])
        with open(os.path.join(self.logo_dir, "local.svg"), "rb") as handle:
            self.assertEqual(handle.read(), SVG)
        run_cli(["--db", self.db_path, "logo", "sync", "local", "--force"])
        with open(os.path.join(self.logo_dir, "local.svg"), "rb") as handle:
            self.assertEqual(handle.read(), CUSTOM_SVG)

        stdout, stderr = run_cli(["--db", self.db_path, "provider", "rm", "local"])
        self.assertIn("removed provider 'local'", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(os.path.exists(os.path.join(self.logo_dir, "local.svg")))
        self.assertFalse(os.path.exists(os.path.join(self.logo_dir, "local_dark.svg")))

    def test_logo_sync_unknown_provider_fails(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as context:
                catalog.main(["--db", self.db_path, "logo", "sync", "missing"])
        self.assertEqual(context.exception.code, 1)
        self.assertIn("provider 'missing' not found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
