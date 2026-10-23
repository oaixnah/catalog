from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import catalog


def build_database(path: str, sequence: int = 0) -> None:
    conn = catalog.connect(path, init=True)
    try:
        conn.execute(
            "INSERT INTO registry (format_version, sequence) VALUES (?, ?)",
            (catalog.FORMAT_VERSION, sequence),
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


class ExportSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = os.path.join(self.tempdir.name, "catalog.sqlite")
        self.out = os.path.join(self.tempdir.name, "reviewed-registry.json")
        build_database(self.db, sequence=7)

    def registry_sequence(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT sequence FROM registry ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()

    def exported_sequence(self) -> int:
        with open(self.out, encoding="utf-8") as handle:
            return json.load(handle)["sequence"]

    def test_export_uses_database_sequence_by_default(self) -> None:
        run_cli(["--db", self.db, "export", "--out", self.out])
        self.assertEqual(self.exported_sequence(), 7)

    def test_export_sequence_override_does_not_modify_database(self) -> None:
        run_cli(["--db", self.db, "export", "--out", self.out, "--sequence", "42"])
        self.assertEqual(self.exported_sequence(), 42)
        self.assertEqual(self.registry_sequence(), 7)

    def test_export_accepts_zero_sequence_override(self) -> None:
        run_cli(["--db", self.db, "export", "--out", self.out, "--sequence", "0"])
        self.assertEqual(self.exported_sequence(), 0)
        self.assertEqual(self.registry_sequence(), 7)

    def test_export_rejects_negative_sequence(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            run_cli(["--db", self.db, "export", "--out", self.out, "--sequence", "-1"])
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.registry_sequence(), 7)


if __name__ == "__main__":
    unittest.main()
