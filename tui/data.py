from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

import catalog


class CatalogValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LinkRecord:
    provider_id: str
    model_id: str
    model_ref: str
    protocol_type: str
    request_path: str

    @property
    def key(self) -> str:
        return f"{self.provider_id}\x1f{self.model_id}"


@dataclass(frozen=True)
class ModelRecord:
    id: str
    display_name: str
    context_limit: int
    input_limit: int | None
    output_limit: int
    tool_calling: bool
    structured_output: bool
    temperature: bool
    efforts: tuple[str, ...]
    provider_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProviderRecord:
    id: str
    display_name: str
    authentication: str
    base_url: str
    model_list_path: str
    logo_source: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class RelatedRecord:
    entity_id: str
    display_name: str
    link_count: int
    link: LinkRecord | None
    detail: str

    @property
    def linked(self) -> bool:
        return self.link_count > 0


DEFAULT_PROTOCOL = "openai-compatible"
DEFAULT_REQUEST_PATH = "/v1/chat/completions"
PROTOCOL_PATHS = {
    "openai-responses": "/v1/responses",
    "anthropic-messages": "/v1/messages",
    "openai-compatible": "/v1/chat/completions",
}


def alpha_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text and not allow_empty:
        raise CatalogValidationError(f"{field} 不能为空")
    return text


def _integer(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None:
        if optional:
            return None
        raise CatalogValidationError(f"{field} 不能为空")
    text = str(value).strip()
    if text.casefold() in ("", "null", "none", "-"):
        if optional:
            return None
        raise CatalogValidationError(f"{field} 不能为空")
    try:
        return int(text)
    except ValueError as error:
        raise CatalogValidationError(f"{field} 必须是整数") from error


def _boolean(value: object) -> int:
    return 1 if bool(value) else 0


def _validate_efforts(efforts: Iterable[str]) -> list[str]:
    values = [str(effort).strip() for effort in efforts]
    if any(not effort for effort in values):
        raise CatalogValidationError("reasoning effort 不能为空")
    if len(set(values)) != len(values):
        raise CatalogValidationError("reasoning effort 不能重复")
    for effort in values:
        if effort not in catalog.EFFORTS:
            raise CatalogValidationError(f"无效的 reasoning effort: {effort}")
    return values


def _validate_protocol(protocol: str) -> str:
    protocol = _text(protocol, "protocol")
    if protocol not in catalog.PROTOCOLS:
        raise CatalogValidationError(f"无效的 protocol: {protocol}")
    return protocol


def _validate_authentication(authentication: str) -> str:
    authentication = _text(authentication, "authentication")
    if authentication not in catalog.AUTHENTICATIONS:
        raise CatalogValidationError(f"无效的 authentication: {authentication}")
    return authentication


def _raise_integrity(error: sqlite3.IntegrityError) -> None:
    message = str(error)
    if "UNIQUE constraint failed: model.id" in message:
        raise CatalogValidationError("model id 已存在") from error
    if "UNIQUE constraint failed: provider.id" in message:
        raise CatalogValidationError("provider id 已存在") from error
    if "UNIQUE constraint failed: provider_model.provider_id, provider_model.model_id" in message:
        raise CatalogValidationError("该 provider 已存在相同的 API model id 链接") from error
    if "FOREIGN KEY constraint failed" in message:
        raise CatalogValidationError("引用的 model 或 provider 不存在") from error
    raise CatalogValidationError(message) from error


def _existing_model(conn: sqlite3.Connection, model_id: str) -> None:
    if conn.execute("SELECT 1 FROM model WHERE id = ?", (model_id,)).fetchone() is None:
        raise CatalogValidationError(f"model '{model_id}' 不存在")


def _existing_provider(conn: sqlite3.Connection, provider_id: str) -> None:
    if conn.execute("SELECT 1 FROM provider WHERE id = ?", (provider_id,)).fetchone() is None:
        raise CatalogValidationError(f"provider '{provider_id}' 不存在")


def fetch_models(conn: sqlite3.Connection) -> list[ModelRecord]:
    efforts: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT model_id, effort FROM model_reasoning_effort ORDER BY sort_order, rowid"
    ):
        efforts[row["model_id"]].append(row["effort"])

    providers: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT provider_id, model_ref FROM provider_model ORDER BY rowid"):
        providers[row["model_ref"]].append(row["provider_id"])

    models = []
    for row in conn.execute("SELECT * FROM model"):
        model_id = row["id"]
        provider_ids = sorted(set(providers.get(model_id, [])), key=alpha_key)
        models.append(
            ModelRecord(
                id=model_id,
                display_name=row["display_name"],
                context_limit=row["context_limit"],
                input_limit=row["input_limit"],
                output_limit=row["output_limit"],
                tool_calling=bool(row["tool_calling"]),
                structured_output=bool(row["structured_output"]),
                temperature=bool(row["temperature"]),
                efforts=tuple(efforts.get(model_id, [])),
                provider_ids=tuple(provider_ids),
            )
        )
    return sorted(models, key=lambda model: alpha_key(model.id))


def fetch_providers(conn: sqlite3.Connection) -> list[ProviderRecord]:
    models: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT provider_id, model_ref FROM provider_model ORDER BY rowid"):
        models[row["provider_id"]].append(row["model_ref"])

    providers = []
    for row in conn.execute("SELECT * FROM provider"):
        provider_id = row["id"]
        model_ids = sorted(set(models.get(provider_id, [])), key=alpha_key)
        providers.append(
            ProviderRecord(
                id=provider_id,
                display_name=row["display_name"],
                authentication=row["authentication"],
                base_url=row["base_url"],
                model_list_path=row["model_list_path"],
                logo_source=row["logo_source"],
                model_ids=tuple(model_ids),
            )
        )
    return sorted(providers, key=lambda provider: alpha_key(provider.id))


def _link_from_row(row: sqlite3.Row) -> LinkRecord:
    return LinkRecord(
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        model_ref=row["model_ref"],
        protocol_type=row["protocol_type"],
        request_path=row["request_path"],
    )


def sort_links(links: Iterable[LinkRecord]) -> list[LinkRecord]:
    return sorted(
        links,
        key=lambda link: (
            link.provider_id.casefold(),
            link.provider_id,
            link.model_id.casefold(),
            link.model_id,
        ),
    )


def fetch_links(conn: sqlite3.Connection) -> list[LinkRecord]:
    return sort_links(_link_from_row(row) for row in conn.execute("SELECT * FROM provider_model"))


def group_links_by_provider(links: Iterable[LinkRecord]) -> list[tuple[str, list[LinkRecord]]]:
    groups: dict[str, list[LinkRecord]] = {}
    for link in sort_links(links):
        groups.setdefault(link.provider_id, []).append(link)
    return list(groups.items())


def fetch_model_links(conn: sqlite3.Connection, model_id: str) -> list[LinkRecord]:
    return sort_links(
        _link_from_row(row)
        for row in conn.execute("SELECT * FROM provider_model WHERE model_ref = ?", (model_id,))
    )


def fetch_provider_links(conn: sqlite3.Connection, provider_id: str) -> list[LinkRecord]:
    return sort_links(
        _link_from_row(row)
        for row in conn.execute("SELECT * FROM provider_model WHERE provider_id = ?", (provider_id,))
    )


def fetch_link(conn: sqlite3.Connection, provider_id: str, model_id: str) -> LinkRecord | None:
    row = conn.execute(
        "SELECT * FROM provider_model WHERE provider_id = ? AND model_id = ?",
        (provider_id, model_id),
    ).fetchone()
    return _link_from_row(row) if row is not None else None


def fetch_related_providers(conn: sqlite3.Connection, model_id: str) -> list[RelatedRecord]:
    links: dict[str, list[LinkRecord]] = defaultdict(list)
    for link in fetch_model_links(conn, model_id):
        links[link.provider_id].append(link)

    related = []
    for provider in fetch_providers(conn):
        provider_links = links.get(provider.id, [])
        detail = f"{provider.authentication}  {provider.base_url}"
        if provider_links:
            detail += f"  |  {provider_links[0].protocol_type} {provider_links[0].request_path}"
        if len(provider_links) > 1:
            detail += f"  (+{len(provider_links) - 1} links)"
        related.append(
            RelatedRecord(
                entity_id=provider.id,
                display_name=provider.display_name,
                link_count=len(provider_links),
                link=provider_links[0] if provider_links else None,
                detail=detail,
            )
        )
    return related


def fetch_related_models(conn: sqlite3.Connection, provider_id: str) -> list[RelatedRecord]:
    links: dict[str, list[LinkRecord]] = defaultdict(list)
    for link in fetch_provider_links(conn, provider_id):
        links[link.model_ref].append(link)

    related = []
    for model in fetch_models(conn):
        model_links = links.get(model.id, [])
        efforts = ",".join(model.efforts) or "-"
        detail = f"ctx {model.context_limit}  out {model.output_limit}  efforts {efforts}"
        if model_links:
            detail += f"  |  api {model_links[0].model_id}  {model_links[0].protocol_type} {model_links[0].request_path}"
        if len(model_links) > 1:
            detail += f"  (+{len(model_links) - 1} links)"
        related.append(
            RelatedRecord(
                entity_id=model.id,
                display_name=model.display_name,
                link_count=len(model_links),
                link=model_links[0] if model_links else None,
                detail=detail,
            )
        )
    return related


def fetch_sequence(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT sequence FROM registry ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["sequence"]) if row is not None else None


def validation_problems(conn: sqlite3.Connection) -> list[str]:
    return catalog.collect_problems(conn)


def create_model(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    display_name: str,
    context_limit: object,
    input_limit: object,
    output_limit: object,
    tool_calling: object,
    structured_output: object,
    temperature: object,
    efforts: Iterable[str],
) -> None:
    model_id = _text(model_id, "id")
    display_name = _text(display_name, "display name")
    context = _integer(context_limit, "context limit")
    input_value = _integer(input_limit, "input limit", optional=True)
    output = _integer(output_limit, "output limit")
    effort_values = _validate_efforts(efforts)
    try:
        with conn:
            conn.execute(
                """INSERT INTO model (id, display_name, context_limit, input_limit, output_limit,
                                      tool_calling, structured_output, temperature)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model_id,
                    display_name,
                    context,
                    input_value,
                    output,
                    _boolean(tool_calling),
                    _boolean(structured_output),
                    _boolean(temperature),
                ),
            )
            for order, effort in enumerate(effort_values):
                conn.execute(
                    "INSERT INTO model_reasoning_effort (model_id, effort, sort_order) VALUES (?, ?, ?)",
                    (model_id, effort, order),
                )
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)


def update_model(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    display_name: str,
    context_limit: object,
    input_limit: object,
    output_limit: object,
    tool_calling: object,
    structured_output: object,
    temperature: object,
    efforts: Iterable[str],
) -> None:
    _existing_model(conn, model_id)
    display_name = _text(display_name, "display name")
    context = _integer(context_limit, "context limit")
    input_value = _integer(input_limit, "input limit", optional=True)
    output = _integer(output_limit, "output limit")
    effort_values = _validate_efforts(efforts)
    try:
        with conn:
            conn.execute(
                """UPDATE model
                      SET display_name = ?, context_limit = ?, input_limit = ?, output_limit = ?,
                          tool_calling = ?, structured_output = ?, temperature = ?
                    WHERE id = ?""",
                (
                    display_name,
                    context,
                    input_value,
                    output,
                    _boolean(tool_calling),
                    _boolean(structured_output),
                    _boolean(temperature),
                    model_id,
                ),
            )
            conn.execute("DELETE FROM model_reasoning_effort WHERE model_id = ?", (model_id,))
            for order, effort in enumerate(effort_values):
                conn.execute(
                    "INSERT INTO model_reasoning_effort (model_id, effort, sort_order) VALUES (?, ?, ?)",
                    (model_id, effort, order),
                )
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)


def _logo_source(value: object) -> str:
    try:
        return catalog.validate_logo_source(value)
    except catalog.LogoError as error:
        raise CatalogValidationError(str(error)) from error


def create_provider(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    display_name: str,
    authentication: str,
    base_url: str,
    model_list_path: str,
    logo_source: str = catalog.DEFAULT_LOGO_SOURCE,
) -> None:
    provider_id = _text(provider_id, "id")
    display_name = _text(display_name, "display name")
    authentication = _validate_authentication(authentication)
    base_url = _text(base_url, "base url")
    model_list_path = _text(model_list_path, "model list path")
    logo_source = _logo_source(logo_source)
    try:
        with conn:
            conn.execute(
                """INSERT INTO provider (id, display_name, authentication, base_url, model_list_path, logo_source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (provider_id, display_name, authentication, base_url, model_list_path, logo_source),
            )
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)


def update_provider(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    display_name: str,
    authentication: str,
    base_url: str,
    model_list_path: str,
    logo_source: str,
) -> None:
    _existing_provider(conn, provider_id)
    display_name = _text(display_name, "display name")
    authentication = _validate_authentication(authentication)
    base_url = _text(base_url, "base url")
    model_list_path = _text(model_list_path, "model list path")
    logo_source = _logo_source(logo_source)
    with conn:
        conn.execute(
            """UPDATE provider
                  SET display_name = ?, authentication = ?, base_url = ?, model_list_path = ?,
                      logo_source = ?
                WHERE id = ?""",
            (display_name, authentication, base_url, model_list_path, logo_source, provider_id),
        )


def create_link(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    api_model_id: str,
    model_ref: str,
    protocol_type: str,
    request_path: str,
) -> None:
    provider_id = _text(provider_id, "provider")
    api_model_id = _text(api_model_id, "API model id")
    model_ref = _text(model_ref, "model")
    protocol_type = _validate_protocol(protocol_type)
    request_path = _text(request_path, "request path")
    _existing_provider(conn, provider_id)
    _existing_model(conn, model_ref)
    try:
        with conn:
            conn.execute(
                """INSERT INTO provider_model (provider_id, model_id, model_ref, protocol_type, request_path)
                   VALUES (?, ?, ?, ?, ?)""",
                (provider_id, api_model_id, model_ref, protocol_type, request_path),
            )
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)


def update_link(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    old_model_id: str,
    api_model_id: str,
    model_ref: str,
    protocol_type: str,
    request_path: str,
) -> None:
    provider_id = _text(provider_id, "provider")
    old_model_id = _text(old_model_id, "old API model id")
    api_model_id = _text(api_model_id, "API model id")
    model_ref = _text(model_ref, "model")
    protocol_type = _validate_protocol(protocol_type)
    request_path = _text(request_path, "request path")
    _existing_provider(conn, provider_id)
    _existing_model(conn, model_ref)
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE provider_model
                      SET model_id = ?, model_ref = ?, protocol_type = ?, request_path = ?
                    WHERE provider_id = ? AND model_id = ?""",
                (api_model_id, model_ref, protocol_type, request_path, provider_id, old_model_id),
            )
            if cursor.rowcount == 0:
                raise CatalogValidationError("链接不存在或已被删除")
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)


def delete_models(conn: sqlite3.Connection, model_ids: Iterable[str]) -> tuple[int, int, int]:
    ids = sorted(set(model_ids))
    if not ids:
        return 0, 0, 0
    placeholders = ",".join("?" for _ in ids)
    efforts = conn.execute(
        f"SELECT count(*) FROM model_reasoning_effort WHERE model_id IN ({placeholders})", ids
    ).fetchone()[0]
    links = conn.execute(
        f"SELECT count(*) FROM provider_model WHERE model_ref IN ({placeholders})", ids
    ).fetchone()[0]
    with conn:
        cursor = conn.execute(f"DELETE FROM model WHERE id IN ({placeholders})", ids)
    return cursor.rowcount, efforts, links


def delete_providers(conn: sqlite3.Connection, provider_ids: Iterable[str]) -> tuple[int, int]:
    ids = sorted(set(provider_ids))
    if not ids:
        return 0, 0
    placeholders = ",".join("?" for _ in ids)
    links = conn.execute(
        f"SELECT count(*) FROM provider_model WHERE provider_id IN ({placeholders})", ids
    ).fetchone()[0]
    with conn:
        cursor = conn.execute(f"DELETE FROM provider WHERE id IN ({placeholders})", ids)
    return cursor.rowcount, links


def delete_links(conn: sqlite3.Connection, keys: Iterable[tuple[str, str]]) -> int:
    pairs = sorted(set(keys))
    if not pairs:
        return 0
    with conn:
        deleted = 0
        for provider_id, model_id in pairs:
            cursor = conn.execute(
                "DELETE FROM provider_model WHERE provider_id = ? AND model_id = ?",
                (provider_id, model_id),
            )
            deleted += cursor.rowcount
    return deleted


def delete_related_links(
    conn: sqlite3.Connection, *, kind: str, entity_id: str, target_ids: Iterable[str]
) -> int:
    targets = sorted(set(target_ids))
    if not targets:
        return 0
    with conn:
        deleted = 0
        for target_id in targets:
            if kind == "model":
                cursor = conn.execute(
                    "DELETE FROM provider_model WHERE model_ref = ? AND provider_id = ?",
                    (entity_id, target_id),
                )
            elif kind == "provider":
                cursor = conn.execute(
                    "DELETE FROM provider_model WHERE provider_id = ? AND model_ref = ?",
                    (entity_id, target_id),
                )
            else:
                raise CatalogValidationError(f"无效的关联类型: {kind}")
            deleted += cursor.rowcount
    return deleted


def most_common_link_defaults(conn: sqlite3.Connection, provider_id: str) -> tuple[str, str]:
    row = conn.execute(
        """SELECT protocol_type, request_path, count(*) AS total
             FROM provider_model
            WHERE provider_id = ?
            GROUP BY protocol_type, request_path
            ORDER BY total DESC, protocol_type, request_path
            LIMIT 1""",
        (provider_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_PROTOCOL, DEFAULT_REQUEST_PATH
    return row["protocol_type"], row["request_path"]


def bind_many(
    conn: sqlite3.Connection,
    *,
    pairs: Iterable[tuple[str, str, str]],
    protocol_type: str,
    request_path: str,
) -> tuple[int, list[str]]:
    protocol_type = _validate_protocol(protocol_type)
    request_path = _text(request_path, "request path")
    normalized = [
        (_text(provider_id, "provider"), _text(model_ref, "model"), _text(api_model_id, "API model id"))
        for provider_id, model_ref, api_model_id in pairs
    ]
    if not normalized:
        return 0, []

    bound = 0
    skipped = []
    try:
        with conn:
            for provider_id, model_ref, api_model_id in normalized:
                _existing_provider(conn, provider_id)
                _existing_model(conn, model_ref)
                exists = conn.execute(
                    "SELECT 1 FROM provider_model WHERE provider_id = ? AND model_ref = ?",
                    (provider_id, model_ref),
                ).fetchone()
                if exists is not None:
                    skipped.append(f"{provider_id} -> {model_ref}")
                    continue
                conn.execute(
                    """INSERT INTO provider_model (provider_id, model_id, model_ref, protocol_type, request_path)
                       VALUES (?, ?, ?, ?, ?)""",
                    (provider_id, api_model_id, model_ref, protocol_type, request_path),
                )
                bound += 1
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)
    return bound, skipped


def apply_related_changes(
    conn: sqlite3.Connection,
    *,
    kind: str,
    entity_id: str,
    binds: Sequence[dict[str, str]],
    unbind_ids: Iterable[str],
) -> tuple[int, int]:
    if kind not in ("model", "provider"):
        raise CatalogValidationError(f"无效的关联类型: {kind}")
    if kind == "model":
        _existing_model(conn, entity_id)
    else:
        _existing_provider(conn, entity_id)
    unbind_values = sorted(set(unbind_ids))
    normalized_binds = []
    for bind in binds:
        target_id = _text(bind.get("target_id"), "target")
        protocol_type = _validate_protocol(bind.get("protocol_type", ""))
        request_path = _text(bind.get("request_path"), "request path")
        api_model_id = _text(bind.get("api_model_id"), "API model id")
        if kind == "model":
            _existing_provider(conn, target_id)
            _existing_model(conn, entity_id)
            normalized_binds.append((target_id, entity_id, api_model_id, protocol_type, request_path))
        else:
            _existing_provider(conn, entity_id)
            _existing_model(conn, target_id)
            normalized_binds.append((entity_id, target_id, api_model_id, protocol_type, request_path))

    unbound = 0
    bound = 0
    try:
        with conn:
            for target_id in unbind_values:
                if kind == "model":
                    cursor = conn.execute(
                        "DELETE FROM provider_model WHERE model_ref = ? AND provider_id = ?",
                        (entity_id, target_id),
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM provider_model WHERE provider_id = ? AND model_ref = ?",
                        (entity_id, target_id),
                    )
                unbound += cursor.rowcount
            for provider_id, model_ref, api_model_id, protocol_type, request_path in normalized_binds:
                exists = conn.execute(
                    "SELECT 1 FROM provider_model WHERE provider_id = ? AND model_ref = ?",
                    (provider_id, model_ref),
                ).fetchone()
                if exists is not None:
                    continue
                conn.execute(
                    """INSERT INTO provider_model (provider_id, model_id, model_ref, protocol_type, request_path)
                       VALUES (?, ?, ?, ?, ?)""",
                    (provider_id, api_model_id, model_ref, protocol_type, request_path),
                )
                bound += 1
    except sqlite3.IntegrityError as error:
        _raise_integrity(error)
    return bound, unbound


def fetch_model(conn: sqlite3.Connection, model_id: str) -> ModelRecord | None:
    for model in fetch_models(conn):
        if model.id == model_id:
            return model
    return None


def fetch_provider(conn: sqlite3.Connection, provider_id: str) -> ProviderRecord | None:
    for provider in fetch_providers(conn):
        if provider.id == provider_id:
            return provider
    return None


def fetch_cascade_counts(
    conn: sqlite3.Connection, *, kind: str, ids: Iterable[str]
) -> tuple[int, int]:
    values = sorted(set(ids))
    if not values:
        return 0, 0
    placeholders = ",".join("?" for _ in values)
    if kind == "model":
        efforts = conn.execute(
            f"SELECT count(*) FROM model_reasoning_effort WHERE model_id IN ({placeholders})", values
        ).fetchone()[0]
        links = conn.execute(
            f"SELECT count(*) FROM provider_model WHERE model_ref IN ({placeholders})", values
        ).fetchone()[0]
        return efforts, links
    if kind == "provider":
        links = conn.execute(
            f"SELECT count(*) FROM provider_model WHERE provider_id IN ({placeholders})", values
        ).fetchone()[0]
        return 0, links
    return 0, 0
