#!/usr/bin/env python3
"""Manage catalog.sqlite (CRUD) and build the reviewed-registry artifacts.

Data flow:
    catalog.sqlite (source of truth, main branch)
        -> export  -> reviewed-registry.json
        -> sign    -> reviewed-registry.signed.json
        -> GitHub Actions -> prod branch (only the two JSON files) -> CDN

Core CRUD/export commands require only the Python standard library. The
interactive TUI (`catalog-cli tui`) additionally requires Textual, declared in
pyproject.toml. `sign` needs an OpenSSL >= 3 binary (ed25519 support) on PATH.
"""

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import NoReturn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "catalog.sqlite")
DEFAULT_REGISTRY = os.path.join(SCRIPT_DIR, "reviewed-registry.json")
DEFAULT_SIGNED = os.path.join(SCRIPT_DIR, "reviewed-registry.signed.json")
DEFAULT_KEY = os.path.join(SCRIPT_DIR, "catalog.pem")

DEFAULT_KEY_ID = "ravel-public-ed25519"
FORMAT_VERSION = 2
SIGNED_FORMAT_VERSION = 1
MODELS_SCHEMA_VERSION = 1
PROVIDERS_SCHEMA_VERSION = 2
MODELS_SCHEMA = "./reviewed-models-v1.schema.json"
PROVIDERS_SCHEMA = "./reviewed-providers-v2.schema.json"

EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
AUTHENTICATIONS = ("bearer-api-key", "x-api-key")
PROTOCOLS = ("openai-responses", "anthropic-messages", "openai-compatible")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    format_version  INTEGER NOT NULL,
    sequence        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model (
    id                TEXT PRIMARY KEY,
    display_name      TEXT    NOT NULL,
    context_limit     INTEGER NOT NULL,
    input_limit       INTEGER,
    output_limit      INTEGER NOT NULL,
    tool_calling      INTEGER NOT NULL DEFAULT 1,
    structured_output INTEGER NOT NULL DEFAULT 1,
    temperature       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_reasoning_effort (
    model_id   TEXT NOT NULL,
    effort     TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    PRIMARY KEY (model_id, effort),
    FOREIGN KEY (model_id) REFERENCES model(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS provider (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    authentication  TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    model_list_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_model (
    provider_id   TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    model_ref     TEXT NOT NULL,
    protocol_type TEXT NOT NULL,
    request_path  TEXT NOT NULL,
    PRIMARY KEY (provider_id, model_id),
    FOREIGN KEY (provider_id) REFERENCES provider(id) ON DELETE CASCADE,
    FOREIGN KEY (model_ref)   REFERENCES model(id)    ON DELETE CASCADE
);
"""


def die(message) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def connect(path, init=False):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.executescript(SCHEMA_SQL)
    return conn


def latest_registry(conn):
    row = conn.execute("SELECT * FROM registry ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        die("registry table is empty; run 'catalog.py init' first")
    return row


def parse_efforts(value):
    efforts = [e.strip() for e in value.split(",") if e.strip()]
    if not efforts:
        die("--efforts must not be empty")
    if len(set(efforts)) != len(efforts):
        die("--efforts contains duplicates")
    for effort in efforts:
        if effort not in EFFORTS:
            die(f"invalid effort '{effort}' (choose from: {', '.join(EFFORTS)})")
    return efforts


def parse_optional_int(value):
    if value is None:
        return None
    if value.strip().lower() in ("null", "none", "-"):
        return None
    try:
        return int(value)
    except ValueError:
        die(f"expected an integer or 'null', got '{value}'")


def parse_bool(value):
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return 1
    if lowered in ("0", "false", "no", "off"):
        return 0
    die(f"expected a boolean (true/false), got '{value}'")


def add_bool_flag(parser, name, help_text):
    dest = name.replace("-", "_")
    parser.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_text)
    parser.add_argument(f"--no-{name}", dest=dest, action="store_false", default=None, help=argparse.SUPPRESS)


def write_text(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def collect_problems(conn):
    problems = []
    for table, rowid, parent, _fkid in conn.execute("PRAGMA foreign_key_check"):
        problems.append(f"foreign key violation: {table} rowid={rowid} -> {parent}")
    if conn.execute("SELECT count(*) FROM registry").fetchone()[0] == 0:
        problems.append("registry table is empty")
    for row in conn.execute("SELECT model_id, effort FROM model_reasoning_effort"):
        if row["effort"] not in EFFORTS:
            problems.append(f"model '{row['model_id']}': invalid effort '{row['effort']}'")
    for row in conn.execute("SELECT id, authentication FROM provider"):
        if row["authentication"] not in AUTHENTICATIONS:
            problems.append(f"provider '{row['id']}': invalid authentication '{row['authentication']}'")
    for row in conn.execute("SELECT provider_id, model_id, protocol_type, request_path FROM provider_model"):
        if not row["model_id"].strip():
            problems.append(f"provider '{row['provider_id']}': empty provider_model.model_id")
        if row["protocol_type"] not in PROTOCOLS:
            problems.append(
                f"link '{row['provider_id']}' -> '{row['model_id']}': invalid protocol_type '{row['protocol_type']}'"
            )
        if not row["request_path"].strip():
            problems.append(f"link '{row['provider_id']}' -> '{row['model_id']}': empty request_path")
    return problems


# ---------------------------------------------------------------------------
# registry document
# ---------------------------------------------------------------------------


def build_document(conn):
    registry = latest_registry(conn)

    models = []
    for model in conn.execute("SELECT * FROM model ORDER BY rowid"):
        efforts = [
            row["effort"]
            for row in conn.execute(
                "SELECT effort FROM model_reasoning_effort WHERE model_id = ? ORDER BY sort_order",
                (model["id"],),
            )
        ]
        models.append(
            {
                "id": model["id"],
                "displayName": model["display_name"],
                "limits": {
                    "context": model["context_limit"],
                    "input": model["input_limit"],
                    "output": model["output_limit"],
                },
                "capabilities": {
                    "toolCalling": bool(model["tool_calling"]),
                    "structuredOutput": bool(model["structured_output"]),
                    "temperature": bool(model["temperature"]),
                    "reasoningEfforts": efforts,
                },
            }
        )

    providers = []
    for provider in conn.execute("SELECT * FROM provider ORDER BY rowid"):
        provider_models = [
            {
                "modelId": row["model_id"],
                "modelRef": row["model_ref"],
                "protocol": {
                    "type": row["protocol_type"],
                    "requestPath": row["request_path"],
                },
            }
            for row in conn.execute(
                "SELECT * FROM provider_model WHERE provider_id = ? ORDER BY rowid",
                (provider["id"],),
            )
        ]
        providers.append(
            {
                "id": provider["id"],
                "displayName": provider["display_name"],
                "baseUrl": provider["base_url"],
                "modelList": {
                    "path": provider["model_list_path"],
                    "authentication": provider["authentication"],
                },
                "models": provider_models,
            }
        )

    return {
        "formatVersion": registry["format_version"],
        "sequence": registry["sequence"],
        "models": {
            "$schema": MODELS_SCHEMA,
            "schemaVersion": MODELS_SCHEMA_VERSION,
            "models": models,
        },
        "providers": {
            "$schema": PROVIDERS_SCHEMA,
            "schemaVersion": PROVIDERS_SCHEMA_VERSION,
            "providers": providers,
        },
    }


def render_document(document):
    return json.dumps(document, indent=4, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# model commands
# ---------------------------------------------------------------------------


def cmd_model_list(args):
    conn = connect(args.db)
    rows = list(conn.execute("SELECT * FROM model ORDER BY rowid"))
    if not rows:
        print("(no models)")
        return
    headers = ("id", "display_name", "context", "input", "output", "tools", "struct", "temp", "efforts")
    table = []
    for model in rows:
        efforts = [
            row["effort"]
            for row in conn.execute(
                "SELECT effort FROM model_reasoning_effort WHERE model_id = ? ORDER BY sort_order",
                (model["id"],),
            )
        ]
        table.append(
            (
                model["id"],
                model["display_name"],
                str(model["context_limit"]),
                "-" if model["input_limit"] is None else str(model["input_limit"]),
                str(model["output_limit"]),
                str(model["tool_calling"]),
                str(model["structured_output"]),
                str(model["temperature"]),
                ",".join(efforts),
            )
        )
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    for row in table:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())


def cmd_model_show(args):
    conn = connect(args.db)
    model = conn.execute("SELECT * FROM model WHERE id = ?", (args.id,)).fetchone()
    if model is None:
        die(f"model '{args.id}' not found")
    efforts = [
        row["effort"]
        for row in conn.execute(
            "SELECT effort FROM model_reasoning_effort WHERE model_id = ? ORDER BY sort_order",
            (args.id,),
        )
    ]
    links = [
        row["provider_id"]
        for row in conn.execute("SELECT provider_id FROM provider_model WHERE model_ref = ? ORDER BY rowid", (args.id,))
    ]
    print(f"id:                {model['id']}")
    print(f"display_name:      {model['display_name']}")
    print(f"context_limit:     {model['context_limit']}")
    print(f"input_limit:       {model['input_limit']}")
    print(f"output_limit:      {model['output_limit']}")
    print(f"tool_calling:      {model['tool_calling']}")
    print(f"structured_output: {model['structured_output']}")
    print(f"temperature:       {model['temperature']}")
    print(f"efforts:           {','.join(efforts) if efforts else '-'}")
    print(f"providers:         {','.join(links) if links else '-'}")


def cmd_model_add(args):
    efforts = parse_efforts(args.efforts) if args.efforts else []
    conn = connect(args.db)
    try:
        conn.execute(
            """INSERT INTO model (id, display_name, context_limit, input_limit, output_limit,
                                  tool_calling, structured_output, temperature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                args.id,
                args.name,
                args.context,
                args.input,
                args.output,
                1 if args.tool_calling is None else args.tool_calling,
                1 if args.structured_output is None else args.structured_output,
                0 if args.temperature is None else args.temperature,
            ),
        )
        for order, effort in enumerate(efforts):
            conn.execute(
                "INSERT INTO model_reasoning_effort (model_id, effort, sort_order) VALUES (?, ?, ?)",
                (args.id, effort, order),
            )
        conn.commit()
    except sqlite3.IntegrityError as error:
        die(f"cannot add model: {error}")
    print(f"added model '{args.id}' ({len(efforts)} efforts)")


def cmd_model_set(args):
    conn = connect(args.db)
    if conn.execute("SELECT 1 FROM model WHERE id = ?", (args.id,)).fetchone() is None:
        die(f"model '{args.id}' not found")
    updates = []
    values = []
    for field, column in (
        ("name", "display_name"),
        ("context", "context_limit"),
        ("input", "input_limit"),
        ("output", "output_limit"),
        ("tool_calling", "tool_calling"),
        ("structured_output", "structured_output"),
        ("temperature", "temperature"),
    ):
        value = getattr(args, field)
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    if isinstance(args.efforts, str):
        parse_efforts(args.efforts)
    if not updates and not isinstance(args.efforts, str):
        die("nothing to update; pass at least one option")
    if updates:
        conn.execute(f"UPDATE model SET {', '.join(updates)} WHERE id = ?", (*values, args.id))
    if isinstance(args.efforts, str):
        efforts = parse_efforts(args.efforts)
        conn.execute("DELETE FROM model_reasoning_effort WHERE model_id = ?", (args.id,))
        for order, effort in enumerate(efforts):
            conn.execute(
                "INSERT INTO model_reasoning_effort (model_id, effort, sort_order) VALUES (?, ?, ?)",
                (args.id, effort, order),
            )
    conn.commit()
    print(f"updated model '{args.id}'")


def cmd_model_rm(args):
    conn = connect(args.db)
    cursor = conn.execute("DELETE FROM model WHERE id = ?", (args.id,))
    conn.commit()
    if cursor.rowcount == 0:
        die(f"model '{args.id}' not found")
    print(f"removed model '{args.id}' (and its efforts / provider links)")


# ---------------------------------------------------------------------------
# provider commands
# ---------------------------------------------------------------------------


def cmd_provider_list(args):
    conn = connect(args.db)
    rows = list(conn.execute("SELECT * FROM provider ORDER BY rowid"))
    if not rows:
        print("(no providers)")
        return
    headers = ("id", "display_name", "authentication", "base_url", "model_list_path", "models")
    table = []
    for provider in rows:
        model_ids = [
            row["model_id"]
            for row in conn.execute(
                "SELECT model_id FROM provider_model WHERE provider_id = ? ORDER BY rowid",
                (provider["id"],),
            )
        ]
        table.append(
            (
                provider["id"],
                provider["display_name"],
                provider["authentication"],
                provider["base_url"],
                provider["model_list_path"],
                ",".join(model_ids),
            )
        )
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    for row in table:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())


def cmd_provider_show(args):
    conn = connect(args.db)
    provider = conn.execute("SELECT * FROM provider WHERE id = ?", (args.id,)).fetchone()
    if provider is None:
        die(f"provider '{args.id}' not found")
    print(f"id:              {provider['id']}")
    print(f"display_name:    {provider['display_name']}")
    print(f"authentication:  {provider['authentication']}")
    print(f"base_url:        {provider['base_url']}")
    print(f"model_list_path: {provider['model_list_path']}")
    print("models:")
    for row in conn.execute(
        "SELECT model_id, model_ref, protocol_type, request_path FROM provider_model WHERE provider_id = ? ORDER BY rowid",
        (args.id,),
    ):
        print(f"  - {row['model_id']} (ref: {row['model_ref']}, {row['protocol_type']} {row['request_path']})")


def cmd_provider_add(args):
    if args.authentication not in AUTHENTICATIONS:
        die(f"invalid authentication '{args.authentication}' (choose from: {', '.join(AUTHENTICATIONS)})")
    conn = connect(args.db)
    try:
        conn.execute(
            """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path)
               VALUES (?, ?, ?, ?, ?)""",
            (args.id, args.name, args.authentication, args.base_url, args.model_list_path),
        )
        conn.commit()
    except sqlite3.IntegrityError as error:
        die(f"cannot add provider: {error}")
    print(f"added provider '{args.id}'")


def cmd_provider_set(args):
    if args.authentication is not None and args.authentication not in AUTHENTICATIONS:
        die(f"invalid authentication '{args.authentication}' (choose from: {', '.join(AUTHENTICATIONS)})")
    conn = connect(args.db)
    if conn.execute("SELECT 1 FROM provider WHERE id = ?", (args.id,)).fetchone() is None:
        die(f"provider '{args.id}' not found")
    updates = []
    values = []
    for field, column in (
        ("name", "display_name"),
        ("authentication", "authentication"),
        ("base_url", "base_url"),
        ("model_list_path", "model_list_path"),
    ):
        value = getattr(args, field)
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    if not updates:
        die("nothing to update; pass at least one option")
    conn.execute(f"UPDATE provider SET {', '.join(updates)} WHERE id = ?", (*values, args.id))
    conn.commit()
    print(f"updated provider '{args.id}'")


def cmd_provider_rm(args):
    conn = connect(args.db)
    cursor = conn.execute("DELETE FROM provider WHERE id = ?", (args.id,))
    conn.commit()
    if cursor.rowcount == 0:
        die(f"provider '{args.id}' not found")
    print(f"removed provider '{args.id}' (and its model links)")


# ---------------------------------------------------------------------------
# link commands
# ---------------------------------------------------------------------------


def cmd_link_list(args):
    conn = connect(args.db)
    rows = list(conn.execute("SELECT * FROM provider_model ORDER BY rowid"))
    if not rows:
        print("(no links)")
        return
    for row in rows:
        print(f"{row['provider_id']}  {row['model_id']}  (ref: {row['model_ref']}, {row['protocol_type']} {row['request_path']})")


def cmd_link_bind(args):
    if args.protocol not in PROTOCOLS:
        die(f"invalid protocol '{args.protocol}' (choose from: {', '.join(PROTOCOLS)})")
    conn = connect(args.db)
    api_model_id = args.api_model_id or args.model_ref
    if conn.execute("SELECT 1 FROM provider WHERE id = ?", (args.provider,)).fetchone() is None:
        die(f"provider '{args.provider}' not found")
    if conn.execute("SELECT 1 FROM model WHERE id = ?", (args.model_ref,)).fetchone() is None:
        die(f"model '{args.model_ref}' not found (model_ref must exist in model table)")
    try:
        conn.execute(
            "INSERT INTO provider_model (provider_id, model_id, model_ref, protocol_type, request_path) VALUES (?, ?, ?, ?, ?)",
            (args.provider, api_model_id, args.model_ref, args.protocol, args.request_path),
        )
        conn.commit()
    except sqlite3.IntegrityError as error:
        die(f"cannot bind: {error}")
    print(f"bound {args.provider} -> {api_model_id} (ref: {args.model_ref}, {args.protocol} {args.request_path})")


def cmd_link_unbind(args):
    conn = connect(args.db)
    cursor = conn.execute(
        "DELETE FROM provider_model WHERE provider_id = ? AND model_id = ?",
        (args.provider, args.model_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        die(f"link {args.provider} -> {args.model_id} not found")
    print(f"unbound {args.provider} -> {args.model_id}")


# ---------------------------------------------------------------------------
# registry / init / validate
# ---------------------------------------------------------------------------


def cmd_registry_show(args):
    conn = connect(args.db)
    registry = latest_registry(conn)
    print(f"format_version: {registry['format_version']}")
    print(f"sequence:       {registry['sequence']}")
    print(f"created_at:     {registry['created_at']}")


def cmd_init(args):
    conn = connect(args.db, init=True)
    if conn.execute("SELECT count(*) FROM registry").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO registry (format_version, sequence) VALUES (?, 0)",
            (FORMAT_VERSION,),
        )
        conn.commit()
        print(f"initialized {args.db} (format_version={FORMAT_VERSION}, sequence=0)")
    else:
        print(f"{args.db} already initialized")


def cmd_validate(args):
    conn = connect(args.db)
    problems = collect_problems(conn)
    if problems:
        print(f"validation failed with {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    models = conn.execute("SELECT count(*) FROM model").fetchone()[0]
    providers = conn.execute("SELECT count(*) FROM provider").fetchone()[0]
    links = conn.execute("SELECT count(*) FROM provider_model").fetchone()[0]
    print(f"ok: {models} models, {providers} providers, {links} links")


# ---------------------------------------------------------------------------
# export / sign
# ---------------------------------------------------------------------------


def cmd_export(args):
    conn = connect(args.db)
    problems = collect_problems(conn)
    if problems:
        print("refusing to export, run 'validate' for details:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    if args.bump_sequence:
        conn.execute(
            "UPDATE registry SET sequence = sequence + 1 WHERE id = (SELECT id FROM registry ORDER BY id DESC LIMIT 1)"
        )
        conn.commit()
    document = build_document(conn)
    write_text(args.out, render_document(document))
    print(
        f"exported {args.out} (sequence={document['sequence']}, "
        f"{len(document['models']['models'])} models, {len(document['providers']['providers'])} providers)"
    )


def cmd_sign(args):
    if not os.path.isfile(args.key):
        die(f"signing key not found: {args.key}")
    if not os.path.isfile(args.infile):
        die(f"registry file not found: {args.infile} (run 'export' first)")
    openssl = shutil.which("openssl")
    if openssl is None:
        die("openssl not found on PATH; OpenSSL >= 3 is required for ed25519 signing")
    version = subprocess.run([openssl, "version"], capture_output=True, text=True).stdout
    match = re.match(r"OpenSSL (\d+)", version)
    if match is None or int(match.group(1)) < 3:
        die(f"OpenSSL >= 3 required, found '{version.strip()}' (LibreSSL is not supported)")

    with open(args.infile, "rb") as handle:
        payload_bytes = handle.read()

    in_fd, in_path = tempfile.mkstemp(prefix="catalog-payload-", suffix=".bin")
    out_fd, out_path = tempfile.mkstemp(prefix="catalog-signature-", suffix=".bin")
    os.close(out_fd)
    try:
        with os.fdopen(in_fd, "wb") as handle:
            handle.write(payload_bytes)
        result = subprocess.run(
            [openssl, "pkeyutl", "-sign", "-rawin", "-inkey", args.key, "-in", in_path, "-out", out_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die(f"openssl signing failed: {result.stderr.strip()}")
        with open(out_path, "rb") as handle:
            signature = handle.read()
    finally:
        for path in (in_path, out_path):
            try:
                os.remove(path)
            except OSError:
                pass

    if len(signature) != 64:
        die(f"unexpected signature length {len(signature)} (expected 64 bytes)")

    payload_field = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    if re.fullmatch(r"[A-Za-z0-9_-]+", payload_field) is None:
        die("payload is not canonical unpadded base64url (URL-safe alphabet, no padding)")

    signed = {
        "formatVersion": SIGNED_FORMAT_VERSION,
        "algorithm": "ed25519",
        "keyId": args.key_id,
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "payload": payload_field,
    }
    write_text(args.out, json.dumps(signed, indent=2, ensure_ascii=False))
    print(f"signed {args.infile} -> {args.out} (keyId={args.key_id})")


# ---------------------------------------------------------------------------
# tui
# ---------------------------------------------------------------------------


def cmd_tui(args):
    if not os.path.isfile(args.db):
        die(f"database not found: {args.db}")
    conn = connect(args.db)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    required = {"registry", "model", "model_reasoning_effort", "provider", "provider_model"}
    missing = sorted(required - tables)
    if missing:
        die(f"database is not initialized (missing tables: {', '.join(missing)}); run 'catalog.py init' first")
    try:
        from tui import run_tui

        run_tui(args.db, args.view)
    except ImportError as error:
        die(f"TUI requires Textual; run 'python3 -m pip install -e .' ({error})")
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="Manage catalog.sqlite and build the reviewed-registry artifacts.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create the schema in a fresh database")
    init_parser.set_defaults(func=cmd_init)

    validate_parser = subparsers.add_parser("validate", help="check the database for consistency problems")
    validate_parser.set_defaults(func=cmd_validate)

    # model --------------------------------------------------------------
    model_parser = subparsers.add_parser("model", help="manage models")
    model_sub = model_parser.add_subparsers(dest="subcommand", required=True)

    model_list = model_sub.add_parser("list", help="list models")
    model_list.set_defaults(func=cmd_model_list)

    model_show = model_sub.add_parser("show", help="show one model")
    model_show.add_argument("id")
    model_show.set_defaults(func=cmd_model_show)

    model_add = model_sub.add_parser("add", help="add a model")
    model_add.add_argument("--id", required=True)
    model_add.add_argument("--name", required=True, help="display name")
    model_add.add_argument("--context", type=int, required=True, help="context window limit")
    model_add.add_argument("--input", help="input limit (integer or 'null')")
    model_add.add_argument("--output", type=int, required=True, help="output limit")
    model_add.add_argument("--efforts", help=f"comma-separated ordered list from: {', '.join(EFFORTS)}")
    add_bool_flag(model_add, "tool-calling", "supports tool calling (default: true)")
    add_bool_flag(model_add, "structured-output", "supports structured output (default: true)")
    add_bool_flag(model_add, "temperature", "supports temperature (default: false)")
    model_add.set_defaults(func=cmd_model_add)

    model_set = model_sub.add_parser("set", help="update a model")
    model_set.add_argument("id")
    model_set.add_argument("--name")
    model_set.add_argument("--context", type=int)
    model_set.add_argument("--input", help="input limit (integer or 'null')")
    model_set.add_argument("--output", type=int)
    model_set.add_argument("--efforts", help="replaces the full ordered effort list")
    add_bool_flag(model_set, "tool-calling", "supports tool calling")
    add_bool_flag(model_set, "structured-output", "supports structured output")
    add_bool_flag(model_set, "temperature", "supports temperature")
    model_set.set_defaults(func=cmd_model_set)

    model_rm = model_sub.add_parser("rm", help="delete a model (cascades efforts and links)")
    model_rm.add_argument("id")
    model_rm.set_defaults(func=cmd_model_rm)

    # provider -----------------------------------------------------------
    provider_parser = subparsers.add_parser("provider", help="manage providers")
    provider_sub = provider_parser.add_subparsers(dest="subcommand", required=True)

    provider_list = provider_sub.add_parser("list", help="list providers")
    provider_list.set_defaults(func=cmd_provider_list)

    provider_show = provider_sub.add_parser("show", help="show one provider")
    provider_show.add_argument("id")
    provider_show.set_defaults(func=cmd_provider_show)

    provider_add = provider_sub.add_parser("add", help="add a provider")
    provider_add.add_argument("--id", required=True)
    provider_add.add_argument("--name", required=True, help="display name")
    provider_add.add_argument("--authentication", required=True, choices=AUTHENTICATIONS)
    provider_add.add_argument("--base-url", required=True)
    provider_add.add_argument("--model-list-path", required=True)
    provider_add.set_defaults(func=cmd_provider_add)

    provider_set = provider_sub.add_parser("set", help="update a provider")
    provider_set.add_argument("id")
    provider_set.add_argument("--name")
    provider_set.add_argument("--authentication", choices=AUTHENTICATIONS)
    provider_set.add_argument("--base-url")
    provider_set.add_argument("--model-list-path")
    provider_set.set_defaults(func=cmd_provider_set)

    provider_rm = provider_sub.add_parser("rm", help="delete a provider (cascades links)")
    provider_rm.add_argument("id")
    provider_rm.set_defaults(func=cmd_provider_rm)

    # link ---------------------------------------------------------------
    link_parser = subparsers.add_parser("link", help="manage provider<->model links")
    link_sub = link_parser.add_subparsers(dest="subcommand", required=True)

    link_list = link_sub.add_parser("list", help="list all links")
    link_list.set_defaults(func=cmd_link_list)

    link_bind = link_sub.add_parser("bind", help="link a provider to a model")
    link_bind.add_argument("provider")
    link_bind.add_argument("model_ref", help="model.id from the model table")
    link_bind.add_argument("--api-model-id", help="model id used in API calls (default: model_ref)")
    link_bind.add_argument("--protocol", required=True, choices=PROTOCOLS)
    link_bind.add_argument("--request-path", required=True)
    link_bind.set_defaults(func=cmd_link_bind)

    link_unbind = link_sub.add_parser("unbind", help="remove a provider<->model link")
    link_unbind.add_argument("provider")
    link_unbind.add_argument("model_id", help="provider_model.model_id as shown by 'link list'")
    link_unbind.set_defaults(func=cmd_link_unbind)

    # registry -----------------------------------------------------------
    registry_parser = subparsers.add_parser("registry", help="inspect registry metadata")
    registry_sub = registry_parser.add_subparsers(dest="subcommand", required=True)
    registry_show = registry_sub.add_parser("show", help="show the latest registry row")
    registry_show.set_defaults(func=cmd_registry_show)

    # tui ----------------------------------------------------------------
    tui_parser = subparsers.add_parser("tui", help="open the interactive Textual UI")
    tui_parser.add_argument(
        "--view",
        choices=("models", "providers", "links"),
        default="models",
        help="initial view (default: models)",
    )
    tui_parser.set_defaults(func=cmd_tui)

    # export / sign ------------------------------------------------------
    export_parser = subparsers.add_parser("export", help="build reviewed-registry.json from the database")
    export_parser.add_argument("--out", default=DEFAULT_REGISTRY)
    export_parser.add_argument("--bump-sequence", action="store_true", help="increment registry.sequence before export")
    export_parser.set_defaults(func=cmd_export)

    sign_parser = subparsers.add_parser("sign", help="build reviewed-registry.signed.json (ed25519 via OpenSSL)")
    sign_parser.add_argument("--key", default=DEFAULT_KEY, help=f"PKCS8 ed25519 private key PEM (default: {DEFAULT_KEY})")
    sign_parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    sign_parser.add_argument("--in", dest="infile", default=DEFAULT_REGISTRY)
    sign_parser.add_argument("--out", default=DEFAULT_SIGNED)
    sign_parser.set_defaults(func=cmd_sign)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "input"):
        args.input = parse_optional_int(args.input)
    args.func(args)


if __name__ == "__main__":
    main()
