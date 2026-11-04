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

Register this adapter once in the application composition root when building
the shared `DataAdapterCatalog`, alongside other configured adapters:

```python
catalog.register(OpenSearchSearchFactory())
```

The `search.public` resource itself is declared in the application's central
`data.resources` configuration shown below. A package resolves the port from
the existing runtime; it does not build another catalog or `DataRuntime`:

```python
from muscles.data.ports import SearchIndexPort

search = app.resource.connect("search.public", SearchIndexPort)
```

Resource config stays in the project:

```yaml
data:
  resources:
    search.public:
      type: opensearch
      url_env: OPENSEARCH_URL
      username: !environ OPENSEARCH_USER
      password: !environ OPENSEARCH_PASSWORD
      index: docs
      timeout: 3
      verify_certs: true
```

The adapter creates the OpenSearch client and a missing index lazily on search,
index/delete, explicit native access or `data.doctor`. New indexes receive a
default mapping for `text`, `title` and `metadata`; `mapping` and `settings`
resource options customize it. Application code should use
`SearchIndexPort`; direct client access is only an advanced escape hatch with
`native_client: true`.

See `muscular-example/example_data_opensearch_1` for an executable example.
