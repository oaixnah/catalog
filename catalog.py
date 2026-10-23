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
import string
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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

DEFAULT_LOGO_SOURCE = "default"
DEFAULT_LOGO_TEMPLATE = "https://models.dev/logos/{provider}.svg"
DARK_LOGO_FILL = "#CED0D6"
LOGO_DIR_NAME = "logo"
LOGO_TIMEOUT_SECONDS = 15
LOGO_MAX_BYTES = 2 * 1024 * 1024
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"


class LogoError(RuntimeError):
    pass


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
    model_list_path TEXT NOT NULL,
    logo_source     TEXT NOT NULL DEFAULT 'default'
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


def ensure_schema(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "provider" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(provider)")}
    if "logo_source" not in columns:
        conn.execute("ALTER TABLE provider ADD COLUMN logo_source TEXT NOT NULL DEFAULT 'default'")
        conn.commit()


def connect(path, init=False):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.executescript(SCHEMA_SQL)
    ensure_schema(conn)
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


def write_bytes_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".logo-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# provider logos
# ---------------------------------------------------------------------------


def logo_dir_for_db(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), LOGO_DIR_NAME)


def validate_logo_source(source):
    if source is None:
        return DEFAULT_LOGO_SOURCE
    value = str(source).strip()
    if not value:
        raise LogoError("logo source must not be empty")
    if value.casefold() in ("default", "models.dev", "modelsdev"):
        return DEFAULT_LOGO_SOURCE
    if value.startswith(("http://", "https://")):
        if any(character.isspace() for character in value):
            raise LogoError("logo URL must not contain whitespace")
        parts = urllib.parse.urlsplit(value.replace("{provider}", "provider"))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise LogoError(f"invalid logo URL '{value}'")
        try:
            parsed = list(string.Formatter().parse(value))
        except ValueError as error:
            raise LogoError(f"invalid logo URL template: {error}") from error
        for _literal, field, spec, conversion in parsed:
            if field is None:
                continue
            if field != "provider" or spec or conversion:
                raise LogoError(f"unsupported logo URL placeholder '{{{field}}}' (only {{provider}} is supported)")
        return value
    if "://" in value:
        raise LogoError(f"unsupported logo URL scheme in '{value}' (use http or https)")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise LogoError("invalid logo file path")
    return value


def resolve_logo_source(source, provider_id):
    source = validate_logo_source(source)
    if source == DEFAULT_LOGO_SOURCE:
        template = DEFAULT_LOGO_TEMPLATE
    elif source.startswith(("http://", "https://")):
        template = source
    else:
        expanded = os.path.expanduser(source)
        if os.path.isabs(expanded):
            return expanded
        return os.path.join(SCRIPT_DIR, expanded)
    return template.format(provider=urllib.parse.quote(provider_id, safe=""))


def logo_file_paths(provider_id, logo_dir):
    if (
        not provider_id
        or provider_id in (".", "..")
        or any(character in provider_id for character in ("/", "\\", "\x00", "\n", "\r"))
    ):
        raise LogoError(f"provider id '{provider_id}' cannot be used as a logo file name")
    return (
        os.path.join(logo_dir, f"{provider_id}.svg"),
        os.path.join(logo_dir, f"{provider_id}_dark.svg"),
    )


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_svg(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise LogoError(f"invalid SVG: {error}") from error
    if _local_name(root.tag) != "svg":
        raise LogoError("logo source is not an SVG document")
    return root


def fetch_logo_bytes(source, provider_id):
    resolved = resolve_logo_source(source, provider_id)
    if resolved.startswith(("http://", "https://")):
        request = urllib.request.Request(
            resolved,
            headers={"User-Agent": "catalog-cli", "Accept": "image/svg+xml"},
        )
        try:
            with urllib.request.urlopen(request, timeout=LOGO_TIMEOUT_SECONDS) as response:
                data = response.read(LOGO_MAX_BYTES + 1)
        except (urllib.error.URLError, OSError) as error:
            raise LogoError(f"cannot download {resolved}: {error}") from error
    else:
        if not os.path.isfile(resolved):
            raise LogoError(f"logo file not found: {resolved}")
        try:
            with open(resolved, "rb") as handle:
                data = handle.read(LOGO_MAX_BYTES + 1)
        except OSError as error:
            raise LogoError(f"cannot read logo file {resolved}: {error}") from error
    if len(data) > LOGO_MAX_BYTES:
        raise LogoError(f"logo exceeds {LOGO_MAX_BYTES} bytes: {resolved}")
    _parse_svg(data)
    return data


def dark_svg_bytes(data):
    ET.register_namespace("", SVG_NAMESPACE)
    ET.register_namespace("xlink", XLINK_NAMESPACE)
    root = _parse_svg(data)
    for element in root.iter():
        if _local_name(element.tag) != "path":
            continue
        element.set("fill", DARK_LOGO_FILL)
        style = element.get("style")
        if style:
            declarations = [part.strip() for part in style.split(";") if part.strip()]
            replaced = False
            for index, declaration in enumerate(declarations):
                name, _, _value = declaration.partition(":")
                if name.strip().casefold() == "fill":
                    declarations[index] = f"fill:{DARK_LOGO_FILL}"
                    replaced = True
            if replaced:
                element.set("style", ";".join(declarations))
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def sync_provider_logo(provider_id, source, logo_dir, *, force=False):
    light_path, dark_path = logo_file_paths(provider_id, logo_dir)
    os.makedirs(logo_dir, exist_ok=True)
    if not force:
        if os.path.isfile(light_path) and os.path.isfile(dark_path):
            return light_path, dark_path
        if os.path.isfile(light_path):
            with open(light_path, "rb") as handle:
                light_data = handle.read(LOGO_MAX_BYTES + 1)
            if len(light_data) > LOGO_MAX_BYTES:
                raise LogoError(f"logo exceeds {LOGO_MAX_BYTES} bytes: {light_path}")
            write_bytes_atomic(dark_path, dark_svg_bytes(light_data))
            return light_path, dark_path
    light_data = fetch_logo_bytes(source, provider_id)
    dark_data = dark_svg_bytes(light_data)
    write_bytes_atomic(light_path, light_data)
    write_bytes_atomic(dark_path, dark_data)
    return light_path, dark_path


def delete_provider_logos(provider_id, logo_dir):
    removed = []
    for path in logo_file_paths(provider_id, logo_dir):
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise LogoError(f"cannot remove {path}: {error}") from error
    return removed


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
    for row in conn.execute("SELECT id, authentication, logo_source FROM provider"):
        if row["authentication"] not in AUTHENTICATIONS:
            problems.append(f"provider '{row['id']}': invalid authentication '{row['authentication']}'")
        try:
            validate_logo_source(row["logo_source"])
        except LogoError as error:
            problems.append(f"provider '{row['id']}': {error}")
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
    print(f"logo_source:     {provider['logo_source']}")
    print("models:")
    for row in conn.execute(
        "SELECT model_id, model_ref, protocol_type, request_path FROM provider_model WHERE provider_id = ? ORDER BY rowid",
        (args.id,),
    ):
        print(f"  - {row['model_id']} (ref: {row['model_ref']}, {row['protocol_type']} {row['request_path']})")


def cmd_provider_add(args):
    if args.authentication not in AUTHENTICATIONS:
        die(f"invalid authentication '{args.authentication}' (choose from: {', '.join(AUTHENTICATIONS)})")
    try:
        logo_source = validate_logo_source(args.logo_source)
    except LogoError as error:
        die(str(error))
    conn = connect(args.db)
    try:
        conn.execute(
            """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path, logo_source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                args.id,
                args.name,
                args.authentication,
                args.base_url,
                args.model_list_path,
                logo_source,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as error:
        die(f"cannot add provider: {error}")
    conn.close()
    print(f"added provider '{args.id}'")
    logo_dir = logo_dir_for_db(args.db)
    try:
        light_path, dark_path = sync_provider_logo(args.id, logo_source, logo_dir, force=True)
    except (LogoError, OSError) as error:
        print(f"warning: logo not synced for '{args.id}': {error}", file=sys.stderr)
    else:
        print(f"logo: {light_path}, {dark_path}")


def cmd_provider_set(args):
    if args.authentication is not None and args.authentication not in AUTHENTICATIONS:
        die(f"invalid authentication '{args.authentication}' (choose from: {', '.join(AUTHENTICATIONS)})")
    logo_source = None
    if args.logo_source is not None:
        try:
            logo_source = validate_logo_source(args.logo_source)
        except LogoError as error:
            die(str(error))
        args.logo_source = logo_source
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
        ("logo_source", "logo_source"),
    ):
        value = getattr(args, field)
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    if not updates:
        die("nothing to update; pass at least one option")
    conn.execute(f"UPDATE provider SET {', '.join(updates)} WHERE id = ?", (*values, args.id))
    conn.commit()
    conn.close()
    print(f"updated provider '{args.id}'")
    if logo_source is not None:
        logo_dir = logo_dir_for_db(args.db)
        try:
            light_path, dark_path = sync_provider_logo(args.id, logo_source, logo_dir, force=True)
        except (LogoError, OSError) as error:
            print(f"warning: logo not synced for '{args.id}': {error}", file=sys.stderr)
        else:
            print(f"logo: {light_path}, {dark_path}")


def cmd_provider_rm(args):
    conn = connect(args.db)
    cursor = conn.execute("DELETE FROM provider WHERE id = ?", (args.id,))
    conn.commit()
    if cursor.rowcount == 0:
        die(f"provider '{args.id}' not found")
    conn.close()
    print(f"removed provider '{args.id}' (and its model links)")
    try:
        removed = delete_provider_logos(args.id, logo_dir_for_db(args.db))
    except LogoError as error:
        print(f"warning: logo cleanup failed for '{args.id}': {error}", file=sys.stderr)
    else:
        if removed:
            print(f"removed logo files: {', '.join(removed)}")


# ---------------------------------------------------------------------------
# logo commands
# ---------------------------------------------------------------------------


def cmd_logo_sync(args):
    conn = connect(args.db)
    if args.providers:
        rows = []
        missing = 0
        for provider_id in args.providers:
            row = conn.execute(
                "SELECT id, logo_source FROM provider WHERE id = ?", (provider_id,)
            ).fetchone()
            if row is None:
                print(f"error: provider '{provider_id}' not found", file=sys.stderr)
                missing += 1
            else:
                rows.append(row)
    else:
        rows = list(conn.execute("SELECT id, logo_source FROM provider ORDER BY rowid"))
        missing = 0
    conn.close()
    if not rows:
        if missing:
            sys.exit(1)
        print("(no providers)")
        return
    logo_dir = logo_dir_for_db(args.db)
    failures = missing
    for row in rows:
        try:
            light_path, dark_path = sync_provider_logo(
                row["id"], row["logo_source"], logo_dir, force=args.force
            )
        except (LogoError, OSError) as error:
            print(f"error: {row['id']}: {error}", file=sys.stderr)
            failures += 1
        else:
            print(f"ok: {row['id']}  {light_path}  {dark_path}")
    if failures:
        sys.exit(1)


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
    provider_add.add_argument(
        "--logo-source",
        help=(
            "logo source: 'default' for models.dev, an http(s) URL template containing "
            "{provider}, or a local SVG file path (default: models.dev)"
        ),
    )
    provider_add.set_defaults(func=cmd_provider_add)

    provider_set = provider_sub.add_parser("set", help="update a provider")
    provider_set.add_argument("id")
    provider_set.add_argument("--name")
    provider_set.add_argument("--authentication", choices=AUTHENTICATIONS)
    provider_set.add_argument("--base-url")
    provider_set.add_argument("--model-list-path")
    provider_set.add_argument(
        "--logo-source",
        help=(
            "logo source: 'default' for models.dev, an http(s) URL template containing "
            "{provider}, or a local SVG file path"
        ),
    )
    provider_set.set_defaults(func=cmd_provider_set)

    provider_rm = provider_sub.add_parser("rm", help="delete a provider (cascades links)")
    provider_rm.add_argument("id")
    provider_rm.set_defaults(func=cmd_provider_rm)

    # logo ---------------------------------------------------------------
    logo_parser = subparsers.add_parser("logo", help="manage provider logo assets")
    logo_sub = logo_parser.add_subparsers(dest="subcommand", required=True)

    logo_sync = logo_sub.add_parser(
        "sync", help="generate logo/<providerId>.svg and logo/<providerId>_dark.svg"
    )
    logo_sync.add_argument("providers", nargs="*", help="provider ids (default: all)")
    logo_sync.add_argument(
        "--force",
        action="store_true",
        help="re-fetch even when both logo files already exist",
    )
    logo_sync.set_defaults(func=cmd_logo_sync)

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
