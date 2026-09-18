# cancerOnIce

> *Catchment Lake, built on the cancerOnIce catalog.*

Population cancer data — incidence, mortality, screening, risk factors, social
and environmental context, and the places people get care — published as
Apache Iceberg tables keyed on geography and time, where **every release every
source ever published stays queryable**.

Sibling of [biocOnIce](https://github.com/seandavi/bioc-on-ice), served through
[icegate](https://github.com/seandavi/icegate). Public aggregates only.

- **[SPEC.md](SPEC.md)** — what the system must do (design source of truth)
- **[AGENTS.md](AGENTS.md)** — working rules for agents and humans
- **[docs/adr/](docs/adr/)** — why decisions went the way they did
- **[docs/DEPLOY.md](docs/DEPLOY.md)** — bucket, catalog, gateway and secrets runbook
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to propose a source or a catchment

## Status

Bootstrapping. Work is sequenced by milestone tracking issues — see the
[milestones](../../milestones).

## Develop

```sh
uv run pytest          # offline; uses a local sqlite warehouse
```

## Licence

Code: [MIT](LICENSE). Data: each source keeps its own terms, recorded per
source in `provenance`; only third-party-redistributable public aggregates are
admitted (SPEC.md § Licence gate).
