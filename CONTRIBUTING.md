# Contributing

Read [SPEC.md](SPEC.md) and [AGENTS.md](AGENTS.md) first; they are short.

## Proposing a data source

Open a **Data source** issue. The licence field is a hard gate: quote the
terms that establish third-party redistribution, or the issue stays
`license:unknown` and nothing is ingested. Record-level, DUA-gated or
restricted data is out of scope, permanently.

## Declaring a catchment

Catchment definitions are self-declared by centers (SPEC.md § Catchment). Once
`catchments.yaml` exists, a declaration is a pull request touching your
center's entry; CI validates the geography ids.

## Code

- One issue, one branch (`area/topic`), one pull request (`Closes #N`).
- `uv run pytest` passes offline before you push.
- A new source is one module plus its schema declarations, a CLI branch and an
  offline fixture test. No frameworks.

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
