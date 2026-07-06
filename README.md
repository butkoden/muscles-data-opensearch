# muscles-data-opensearch

OpenSearch adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports, resource runtime and diagnostics, while this package owns the
OpenSearch-backed `SearchIndexPort` implementation.

## Related packages

- Core runtime and port contracts:
  [`muscles-data`](https://github.com/butkoden/muscles-data)
- Elasticsearch search adapter:
  [`muscles-data-elasticsearch`](https://github.com/butkoden/muscles-data-elasticsearch)
- Redis key-value/lock/stream adapter:
  [`muscles-data-redis`](https://github.com/butkoden/muscles-data-redis)
- Qdrant vector adapter:
  [`muscles-data-qdrant`](https://github.com/butkoden/muscles-data-qdrant)
- MongoDB document-store adapter:
  [`muscles-data-mongodb`](https://github.com/butkoden/muscles-data-mongodb)
- S3 object-store adapter:
  [`muscles-data-s3`](https://github.com/butkoden/muscles-data-s3)
- SQLAlchemy direct SQL resource adapter:
  [`muscles-data-sqlalchemy`](https://github.com/butkoden/muscles-data-sqlalchemy)
- Executable example:
  [`example_data_opensearch_1`](https://github.com/butkoden/muscular-example/tree/master/example_data_opensearch_1)

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
