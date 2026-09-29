# Incident replay format

`replay.json` is product sample data, without grading answers or test assertions.
Stage 7 validation has exercised this replay with a fixed LongCat model. The attempted
investigation did not complete; provider probes and deterministic replay checks have
passed. See the [validation record](../../dev-notes/architecture/incident-agent-validation.md).

The UTF-8 JSON format is defined by `tau_incident.telemetry.ReplayData`:

- `schema_version: 1`, a dataset `name`, and `rows`.
- Each row has a signal `kind` (`metrics`, `logs`, `traces`, `deployments`, or
  `configuration`), `environment`, `entity`, timezone-aware `data_at` and
  `available_at`, a JSON `body`, and optional `units`.
- Optional `coverage` entries declare a signal, scope, availability, sampling and
  explanatory note. Omitting coverage means unknown coverage.
- Optional `catalog` entries contain a version, scope, availability, entities,
  directed edges and configuration metadata.

Queries filter signal, environment, entities, inclusive event time and optional
substring; they expose offset/limit pagination. Only records available at the
injected clock are visible. A page or sampled result does not establish complete
coverage. Empty matches and unsupported signal failures remain distinct.

CLI `--fixture-now` fixes data availability time while execution timestamps use
the real application clock. Without it, availability uses that application clock.
The dataset filename and SHA-256 are included in evidence provenance. No network
telemetry adapters or production modifications are part of this stage.
