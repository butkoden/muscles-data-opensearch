# muscles-data-opensearch

OpenSearch adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports, resource runtime and diagnostics, while this package owns the
OpenSearch-backed `SearchIndexPort` implementation.

## Usage

Register the factory in the project composition root:

```python
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.ports import SearchIndexPort
from muscles_data.runtime import DataRuntime
from muscles_data_opensearch import OpenSearchSearchFactory

catalog = DataAdapterCatalog.with_defaults()
catalog.register(OpenSearchSearchFactory())

runtime = DataRuntime(config=config, catalog=catalog)
search = runtime.require_port("search.public", SearchIndexPort)
```

Resource config stays in the project:

```yaml
data:
  resources:
    search.public:
      type: opensearch
      url: ${OPENSEARCH_URL}
      username: ${OPENSEARCH_USER}
      password: ${OPENSEARCH_PASSWORD}
      index: docs
      timeout: 3
      verify_certs: true
```

The adapter creates the OpenSearch client lazily on search, index/delete,
explicit native access or `data.doctor`. Application code should use
`SearchIndexPort`; direct client access is only an advanced escape hatch with
`native_client: true`.

See `muscular-example/example_data_opensearch_1` for an executable example.
