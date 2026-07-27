from __future__ import annotations

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import SearchIndexPort
from muscles_data.runtime import DataRuntime

from muscles_data_opensearch import (
    OpenSearchClientMissingError,
    OpenSearchConnectionError,
    OpenSearchFilterError,
    OpenSearchSearchFactory,
    opensearch_filter_from_mapping,
)


class FakeIndices:
    def __init__(self, client: "FakeOpenSearchClient") -> None:
        self.client = client

    def exists(self, *, index: str) -> bool:
        self.client.index_checks.append(index)
        if self.client.fail_health:
            raise TimeoutError("opensearch password=secret timed out")
        return index == "docs"


class FakeOpenSearchClient:
    def __init__(self, *, fail_health: bool = False) -> None:
        self.fail_health = fail_health
        self.searches: list[dict] = []
        self.indexes: list[dict] = []
        self.delete_queries: list[dict] = []
        self.index_checks: list[str] = []
        self.closed = False
        self.indices = FakeIndices(self)

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return {"hits": {"hits": [{"_id": "doc-1", "_score": 4.2, "_source": {"title": "Ports", "text": "Muscles data ports", "metadata": {"section": "docs"}}}]}}

    def index(self, **kwargs):
        self.indexes.append(kwargs)
        return {"result": "created"}

    def delete_by_query(self, **kwargs):
        self.delete_queries.append(kwargs)
        return {"deleted": 3}

    def ping(self) -> bool:
        if self.fail_health:
            raise TimeoutError("opensearch password=secret timed out")
        return True

    def close(self) -> None:
        self.closed = True


def _config(url: str = "https://opensearch.example") -> dict:
    return {
        "data": {
            "resources": {
                "search.open": {
                    "type": "opensearch",
                    "url": url,
                    "username": "admin",
                    "password": "open-secret",
                    "index": "docs",
                    "native_client": True,
                }
            }
        }
    }


def _runtime(client: FakeOpenSearchClient | None, url: str = "https://opensearch.example") -> DataRuntime:
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(OpenSearchSearchFactory(client_factory=lambda _config: client))
    return DataRuntime(config=DataConfig.from_raw(_config(url)), catalog=catalog)


def test_opensearch_external_adapter_maps_search_index_delete_and_native_access():
    client = FakeOpenSearchClient()
    runtime = _runtime(client)

    listed = runtime.list_resources()[0]
    assert listed["type"] == "opensearch"
    assert {"keyword_search", "document_index"} <= set(listed["capabilities"])
    assert listed["initialized"] is False

    search = runtime.require_port("search.open", SearchIndexPort)
    hits = search.search_text("muscles", filters={"section": "docs"}, limit=2)
    write = search.upsert_documents([{"id": "doc-1", "title": "Ports", "text": "Muscles data ports", "metadata": {"section": "docs"}}])
    deleted = search.delete_documents(filters={"section": ["docs", "notes"]})

    assert [hit.id for hit in hits] == ["doc-1"]
    assert hits[0].title == "Ports"
    assert client.searches[0]["body"]["query"]["bool"]["filter"] == [{"term": {"metadata.section": "docs"}}]
    assert write.written == 1
    assert client.indexes[0]["body"]["title"] == "Ports"
    assert client.indexes[0]["body"]["metadata"] == {"section": "docs"}
    assert deleted.deleted == 3
    assert client.delete_queries[0]["body"]["query"]["bool"]["filter"][0] == {"terms": {"metadata.section": ["docs", "notes"]}}
    assert runtime.require_resource("search.open", DataCapability.NATIVE_CLIENT).native_client() is client
    assert runtime.doctor()["status"] == "ok"
    assert client.index_checks == ["docs"]
    assert runtime.close()["status"] == "ok"
    assert client.closed is True


def test_opensearch_external_adapter_filters_and_safe_failures():
    translated = opensearch_filter_from_mapping({"score": {"gte": 0.5}, "$not": {"archived": True}})
    assert translated[0] == {"bool": {"must_not": [{"term": {"metadata.archived": True}}]}}
    assert translated[1] == {"range": {"metadata.score": {"gte": 0.5}}}
    with pytest.raises(OpenSearchFilterError):
        opensearch_filter_from_mapping({"score": {"near": 1.0}})

    with pytest.raises(OpenSearchClientMissingError):
        _runtime(None).require_port("search.open", SearchIndexPort).search_text("x")

    failing = _runtime(FakeOpenSearchClient(fail_health=True), "https://user:secret@opensearch.example").doctor()
    assert failing["status"] == "failed"
    assert "secret" not in repr(failing)

    bad_client = FakeOpenSearchClient()
    bad_client.search = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable"))
    with pytest.raises(OpenSearchConnectionError):
        _runtime(bad_client).require_port("search.open", SearchIndexPort).search_text("x")
