from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Mapping
from urllib.parse import urlsplit

from muscles_data.config import DataResourceConfig
from muscles_data.errors import DataError
from muscles_data.models import DataCapability, HealthResult, InspectResult, SearchHit, WriteResult


_CLIENT_UNSET = object()
_RANGE_OPERATORS = {"gt", "gte", "lt", "lte"}
DEFAULT_INDEX_MAPPING = {
    "properties": {
        "text": {"type": "text"},
        "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "metadata": {"type": "object", "dynamic": True},
    }
}
_ALLOWED_OPTIONS = {
    "url",
    "url_env",
    "username",
    "password",
    "http_auth",
    "index",
    "timeout",
    "verify_certs",
    "use_ssl",
    "ssl_assert_hostname",
    "ssl_show_warn",
    "ca_certs",
    "native_client",
    "text_field",
    "metadata_field",
    "mapping",
    "settings",
}


class OpenSearchAdapterError(DataError):
    """Base error for OpenSearch search adapter failures."""


class OpenSearchConfigError(ValueError, OpenSearchAdapterError):
    """Raised when an OpenSearch resource config cannot be mapped safely."""


class OpenSearchClientMissingError(OpenSearchAdapterError):
    """Raised when OpenSearch adapter is used without an available client."""


class OpenSearchConnectionError(OpenSearchAdapterError):
    """Raised when an OpenSearch operation cannot reach or use the backend."""


class OpenSearchFilterError(OpenSearchAdapterError):
    """Raised when a data filter cannot be translated to OpenSearch."""


class OpenSearchSearchAdapter:
    resource_type = "opensearch"

    def __init__(
        self,
        config: DataResourceConfig,
        *,
        client_factory: Callable[[DataResourceConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: Any = _CLIENT_UNSET
        self._lock = threading.RLock()
        self._index_ready = False
        self.closed = False

    def search_text(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        options: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        options = dict(options or {})
        body: dict[str, Any] = {
            "query": self._query(query, filters),
            "size": max(0, int(limit)),
        }
        if options.pop("highlight", False):
            fields = options.pop("highlight_fields", [self.text_field()])
            body["highlight"] = {"fields": {str(field): {} for field in fields}}
        kwargs: dict[str, Any] = {"index": self.index_name(), "body": body}
        for key in ("from_", "sort", "track_total_hits", "routing", "preference", "search_after"):
            if key in options:
                kwargs[key] = options[key]
        try:
            response = self._client_instance().search(**kwargs)
        except (OpenSearchClientMissingError, OpenSearchConfigError, OpenSearchFilterError):
            raise
        except Exception as exc:
            raise OpenSearchConnectionError(self._safe_error(exc)) from exc
        return [
            _hit_from_response(hit, text_field=self.text_field(), metadata_field=self.metadata_field())
            for hit in _hits(response)
        ]

    def upsert_documents(self, items: list[Mapping[str, Any]], options: Mapping[str, Any] | None = None) -> WriteResult:
        options = dict(options or {})
        client = self._client_instance()
        try:
            for item in items:
                kwargs: dict[str, Any] = {
                    "index": self.index_name(),
                    "id": str(item["id"]),
                    "body": self._document_from_item(item),
                }
                for key in ("refresh", "routing", "pipeline"):
                    if key in options:
                        kwargs[key] = options[key]
                client.index(**kwargs)
        except KeyError as exc:
            raise OpenSearchConfigError(f"OpenSearch document item requires {exc.args[0]}") from exc
        except (OpenSearchClientMissingError, OpenSearchConfigError, OpenSearchFilterError):
            raise
        except Exception as exc:
            raise OpenSearchConnectionError(self._safe_error(exc)) from exc
        return WriteResult(written=len(items), matched=len(items))

    def delete_documents(
        self,
        ids: list[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> WriteResult:
        options = dict(options or {})
        if ids is None and not filters:
            return WriteResult()
        client = self._client_instance()
        try:
            if ids is not None:
                deleted = 0
                for document_id in ids:
                    kwargs: dict[str, Any] = {"index": self.index_name(), "id": str(document_id)}
                    for key in ("refresh", "routing"):
                        if key in options:
                            kwargs[key] = options[key]
                    client.delete(**kwargs)
                    deleted += 1
                return WriteResult(deleted=deleted, matched=deleted)
            kwargs = {
                "index": self.index_name(),
                "body": {"query": self._filter_query(filters)},
            }
            for key in ("refresh", "routing", "conflicts"):
                if key in options:
                    kwargs[key] = options[key]
            response = client.delete_by_query(**kwargs)
        except (OpenSearchClientMissingError, OpenSearchConfigError, OpenSearchFilterError):
            raise
        except Exception as exc:
            raise OpenSearchConnectionError(self._safe_error(exc)) from exc
        deleted = int(_mapping_get(response, "deleted", 0) or 0)
        return WriteResult(deleted=deleted, matched=deleted)

    def inspect(self) -> dict[str, Any]:
        return asdict(
            InspectResult(
                name=self.config.name,
                type=self.config.type,
                capabilities=[],
                initialized=self._client is not _CLIENT_UNSET,
                status="ok",
                options=self.config.safe_options(),
                details={"backend": "opensearch", "index": self.index_name()},
            )
        )

    def health(self) -> HealthResult:
        try:
            client = self._client_instance()
            ping = getattr(client, "ping", None)
            if callable(ping) and not bool(ping()):
                return HealthResult(
                    status="failed",
                    message="OpenSearch ping failed",
                    details={"index": self.index_name()},
                )
            indices = getattr(client, "indices", None)
            exists = self._index_ready
            if not exists and indices is not None and hasattr(indices, "exists"):
                exists = bool(indices.exists(index=self.index_name()))
        except Exception as exc:
            return HealthResult(status="failed", message=self._safe_error(exc), details={"index": self.index_name()})
        if not exists:
            return HealthResult(
                status="failed",
                message=f"OpenSearch index '{self.index_name()}' is not available",
                details={"index": self.index_name()},
            )
        return HealthResult(status="ok", message="OpenSearch index is available", details={"index": self.index_name()})

    def close(self) -> None:
        if self._client is _CLIENT_UNSET:
            self.closed = True
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self.closed = True

    def native_client(self):
        return self._client_instance()

    def index_name(self) -> str:
        return str(self.config.options["index"])

    def text_field(self) -> str:
        return str(self.config.options.get("text_field", "text"))

    def metadata_field(self) -> str:
        return str(self.config.options.get("metadata_field", "metadata"))

    def _query(self, query: str, filters: Mapping[str, Any] | None) -> dict[str, Any]:
        clauses: dict[str, Any] = {}
        if str(query).strip():
            clauses["must"] = [{"match": {self.text_field(): {"query": str(query)}}}]
        else:
            clauses["must"] = [{"match_all": {}}]
        filter_clauses = opensearch_filter_from_mapping(filters or {}, field_prefix=self.metadata_field())
        if filter_clauses:
            clauses["filter"] = filter_clauses
        return {"bool": clauses}

    def _filter_query(self, filters: Mapping[str, Any] | None) -> dict[str, Any]:
        filter_clauses = opensearch_filter_from_mapping(filters or {}, field_prefix=self.metadata_field())
        return {"bool": {"filter": filter_clauses}} if filter_clauses else {"match_all": {}}

    def _document_from_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
        metadata = dict(item.get("payload", {}) or {})
        metadata.update(dict(item.get("metadata", {}) or {}))
        document = {
            self.text_field(): str(item.get("text", "")),
            self.metadata_field(): metadata,
        }
        if item.get("title") is not None:
            document["title"] = str(item["title"])
        fields = item.get("fields")
        if isinstance(fields, Mapping):
            document.update(dict(fields))
        return document

    def _client_instance(self):
        if self._client is _CLIENT_UNSET:
            with self._lock:
                if self._client is _CLIENT_UNSET:
                    self._validate_options()
                    client = (
                        self._client_factory(self.config)
                        if self._client_factory
                        else _default_opensearch_client(self.config)
                    )
                    if client is None:
                        raise OpenSearchClientMissingError("OpenSearch client is not available")
                    self._client = client
                    try:
                        self._ensure_index(client)
                    except Exception:
                        self._client = _CLIENT_UNSET
                        close = getattr(client, "close", None)
                        if callable(close):
                            close()
                        raise
        return self._client

    def _validate_options(self) -> None:
        unknown = sorted(set(self.config.options) - _ALLOWED_OPTIONS)
        if unknown:
            names = ", ".join(unknown)
            raise OpenSearchConfigError(f"Unsupported OpenSearch resource options: {names}")
        http_auth = self.config.options.get("http_auth")
        if http_auth is not None and (not isinstance(http_auth, (list, tuple)) or len(http_auth) != 2):
            raise OpenSearchConfigError("OpenSearch http_auth must contain username and password")
        if not self.config.options.get("url") and not self.config.options.get("url_env"):
            raise OpenSearchConfigError("OpenSearch resource requires url or url_env")
        self.index_name()

    def mapping(self) -> dict[str, Any]:
        mapping = self.config.options.get("mapping")
        if mapping is None:
            return dict(DEFAULT_INDEX_MAPPING)
        if not isinstance(mapping, Mapping):
            raise OpenSearchConfigError("OpenSearch mapping must be a mapping")
        return dict(mapping)

    def _ensure_index(self, client: Any) -> None:
        indices = getattr(client, "indices", None)
        exists = getattr(indices, "exists", None)
        create = getattr(indices, "create", None)
        if not callable(exists) or not callable(create):
            return
        try:
            if bool(exists(index=self.index_name())):
                self._index_ready = True
                return
            body: dict[str, Any] = {"mappings": self.mapping()}
            settings = self.config.options.get("settings")
            if settings is not None:
                if not isinstance(settings, Mapping):
                    raise OpenSearchConfigError("OpenSearch settings must be a mapping")
                body["settings"] = dict(settings)
            create(index=self.index_name(), body=body)
            self._index_ready = True
        except OpenSearchConfigError:
            raise
        except Exception as exc:
            message = self._safe_error(exc)
            if "already exists" in message.lower() or "resource_already_exists" in message.lower():
                self._index_ready = True
                return
            raise OpenSearchConnectionError(message) from exc

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        try:
            options = self.config.resolved_options()
        except Exception:
            options = self.config.options
        for key, value in options.items():
            if key in {"url", "username", "password", "http_auth"} and value:
                if isinstance(value, (list, tuple)):
                    for item in value:
                        message = message.replace(str(item), "***")
                else:
                    message = message.replace(str(value), "***")
                    if key == "url":
                        parsed = urlsplit(str(value))
                        for item in (parsed.username, parsed.password):
                            if item:
                                message = message.replace(str(item), "***")
        return message


class OpenSearchSearchFactory:
    resource_type = "opensearch"

    def __init__(self, *, client_factory: Callable[[DataResourceConfig], Any] | None = None) -> None:
        self._client_factory = client_factory

    def capabilities(self, config: DataResourceConfig) -> set[DataCapability]:
        native = {DataCapability.NATIVE_CLIENT} if bool(config.options.get("native_client")) else set()
        return {DataCapability.KEYWORD_SEARCH, DataCapability.DOCUMENT_INDEX, DataCapability.HEALTHCHECK} | native

    def create(self, config: DataResourceConfig) -> OpenSearchSearchAdapter:
        return OpenSearchSearchAdapter(config, client_factory=self._client_factory)


def opensearch_filter_from_mapping(
    filters: Mapping[str, Any],
    *,
    field_prefix: str | None = "metadata",
) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for key in sorted(filters, key=_filter_sort_key):
        value = filters[key]
        if key == "$and":
            clauses.append({"bool": {"filter": _logical_conditions(value, field_prefix=field_prefix)}})
        elif key == "$or":
            clauses.append(
                {
                    "bool": {
                        "should": _logical_conditions(value, field_prefix=field_prefix),
                        "minimum_should_match": 1,
                    }
                }
            )
        elif key == "$not":
            clauses.append({"bool": {"must_not": _logical_conditions(value, field_prefix=field_prefix)}})
        else:
            clauses.append(_field_condition(str(key), value, field_prefix=field_prefix))
    return clauses


def _logical_conditions(value: Any, *, field_prefix: str | None) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return opensearch_filter_from_mapping(value, field_prefix=field_prefix)
    if isinstance(value, list):
        conditions: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise OpenSearchFilterError("OpenSearch logical filter items must be mappings")
            conditions.extend(opensearch_filter_from_mapping(item, field_prefix=field_prefix))
        return conditions
    raise OpenSearchFilterError("OpenSearch logical filters must be mappings or lists of mappings")


def _field_condition(key: str, value: Any, *, field_prefix: str | None) -> dict[str, Any]:
    field = _field_name(key, field_prefix)
    if isinstance(value, Mapping):
        operators = set(value)
        if operators <= _RANGE_OPERATORS:
            return {"range": {field: dict(value)}}
        if operators == {"eq"}:
            return {"term": {field: value["eq"]}}
        if operators == {"any"}:
            return {"terms": {field: list(value["any"])}}
        unsupported = ", ".join(sorted(str(operator) for operator in operators))
        raise OpenSearchFilterError(f"Unsupported OpenSearch filter operator for '{key}': {unsupported}")
    if isinstance(value, (list, tuple, set)):
        return {"terms": {field: list(value)}}
    return {"term": {field: value}}


def _field_name(key: str, field_prefix: str | None) -> str:
    if not field_prefix or "." in key:
        return key
    return f"{field_prefix}.{key}"


def _filter_sort_key(key: Any) -> tuple[int, str]:
    order = {"$and": 0, "$or": 1, "$not": 2}
    key_text = str(key)
    return (order.get(key_text, 10), key_text)


def _hit_from_response(hit: Mapping[str, Any], *, text_field: str, metadata_field: str) -> SearchHit:
    source = dict(hit.get("_source", {}) or {})
    metadata = dict(source.get(metadata_field, {}) or {})
    highlights = {
        str(key): [str(item) for item in value]
        for key, value in dict(hit.get("highlight", {}) or {}).items()
    }
    return SearchHit(
        id=str(hit.get("_id", "")),
        score=float(hit.get("_score", 0.0) or 0.0),
        text=source.get(text_field),
        metadata=metadata,
        highlights=highlights,
        title=source.get("title"),
    )


def _hits(response: Any) -> list[Mapping[str, Any]]:
    return list(_mapping_get(_mapping_get(response, "hits", {}), "hits", []) or [])


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    try:
        return value[key]
    except Exception:
        return default


def _default_opensearch_client(config: DataResourceConfig):
    try:
        client_module = importlib.import_module("opensearchpy")
    except Exception as exc:
        raise OpenSearchClientMissingError("Install opensearch-py or muscles-data-opensearch to use type=opensearch") from exc

    try:
        options = config.resolved_options()
    except Exception as exc:
        raise OpenSearchConfigError(str(exc)) from exc
    url = str(options["url"])
    parsed = urlsplit(url)
    kwargs: dict[str, Any] = {"hosts": [url]}
    if options.get("http_auth"):
        kwargs["http_auth"] = tuple(options["http_auth"])
    elif options.get("username") or options.get("password"):
        kwargs["http_auth"] = (options.get("username"), options.get("password"))
    if options.get("timeout") is not None:
        kwargs["timeout"] = options["timeout"]
    if options.get("verify_certs") is not None:
        kwargs["verify_certs"] = bool(options["verify_certs"])
    kwargs["use_ssl"] = bool(options.get("use_ssl", parsed.scheme == "https"))
    for key in ("ssl_assert_hostname", "ssl_show_warn", "ca_certs"):
        if key in options:
            kwargs[key] = options[key]

    try:
        return client_module.OpenSearch(**kwargs)
    except Exception as exc:
        raise OpenSearchConnectionError(str(exc)) from exc
