from __future__ import annotations

import os
from uuid import uuid4

import pytest
from muscles.data.catalog import DataAdapterCatalog
from muscles.data.config import DataConfig
from muscles.data.models import DataCapability
from muscles.data.ports import SearchIndexPort
from muscles.data.runtime import DataRuntime

from muscles.data.opensearch import OpenSearchSearchFactory
from muscles.data.errors import DataWriteConflictError
from muscles.data.policies import DataPolicyDecision, DataPolicyPipeline, DataPredicate, DataPredicateOperator
from muscles.data.policy_search import PolicySearchIndex


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("MUSCLES_DATA_INTEGRATION"), reason="backend integration is disabled"),
]


def test_policy_snapshot_and_concurrent_write():
    index = f"muscles-policy-it-{uuid4().hex[:12]}"
    config = DataConfig.from_raw({"resources": {"search": {
        "type": "opensearch", "url_env": "OPENSEARCH_URL", "index": index,
    }}})
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(OpenSearchSearchFactory())
    runtime = DataRuntime(config=config, catalog=catalog)
    adapter = runtime.require_port("search", SearchIndexPort)
    client = adapter._client_instance()

    class Policy:
        def evaluate(self, request):
            return DataPolicyDecision(predicates=(DataPredicate("workspace", DataPredicateOperator.EQ, "a"),))

    def put(document_id, workspace):
        client.index(index=index, id=document_id, body={"text": "hello", "metadata": {"workspace": workspace}}, refresh=True)

    try:
        put("a", ["a", "b"])
        put("b", "a")
        put("c", "a")
        store = PolicySearchIndex(adapter, "search", policies=DataPolicyPipeline((Policy(),)))
        original_search = client.search
        calls = []

        def changing_search(*args, **kwargs):
            response = original_search(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                put("c", "b")
            return response

        client.search = changing_search
        assert {hit.id for hit in store.search_text("hello", limit=2)} == {"b", "c"}
        assert len(calls) >= 2
        original_get = adapter.policy_get_versioned

        def concurrent_get(document_id):
            result = original_get(document_id)
            put(document_id, "b")
            return result

        adapter.policy_get_versioned = concurrent_get
        with pytest.raises(DataWriteConflictError):
            store.upsert_documents([{"id": "b", "text": "hello", "metadata": {"workspace": "a"}}])
        assert client.get(index=index, id="b")["_source"]["metadata"]["workspace"] == "b"
    finally:
        client.indices.delete(index=index, ignore=[404])
        runtime.close()


def test_opensearch_real_search_index_lifecycle():
    index = f"muscles-data-it-{uuid4().hex[:12]}"
    config = DataConfig.from_raw(
        {
            "data": {
                "resources": {
                    "search.open": {
                        "type": "opensearch",
                        "url_env": "OPENSEARCH_URL",
                        "index": index,
                        "verify_certs": False,
                        "native_client": True,
                    }
                }
            }
        }
    )
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(OpenSearchSearchFactory())
    runtime = DataRuntime(config=config, catalog=catalog)

    client = None
    try:
        search = runtime.require_port("search.open", SearchIndexPort)
        assert search.upsert_documents(
            [
                {"id": "alpha", "title": "Alpha", "text": "alpha document", "metadata": {"status": "ready"}},
                {"id": "beta", "title": "Beta", "text": "beta document", "metadata": {"status": "draft"}},
            ],
            options={"refresh": "wait_for"},
        ).written == 2
        hits = search.search_text("alpha", filters={"status": "ready"}, options={"highlight": True})
        assert [hit.id for hit in hits] == ["alpha"]
        assert hits[0].highlights
        assert runtime.doctor()["status"] == "ok"
        assert search.delete_documents(ids=["alpha"], options={"refresh": "wait_for"}).deleted == 1
        assert search.search_text("alpha") == []
        contracts = pytest.importorskip("muscles.data.contracts")
        contract = getattr(contracts, "assert_search_index_contract", None)
        if contract is not None:
            contract(lambda: search)
    finally:
        try:
            if client is None:
                try:
                    client = runtime.require_resource("search.open", DataCapability.NATIVE_CLIENT).native_client()
                except Exception:
                    client = None
            if client is not None:
                client.indices.delete(index=index, ignore=[404])
        finally:
            runtime.close()
