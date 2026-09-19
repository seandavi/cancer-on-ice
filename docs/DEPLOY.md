# Deploying the cancerOnIce catalog

Written 2026-09-18 while provisioning, from the commands actually run. biocOnIce
never recorded how its bucket and catalog were created; this is that runbook.

## What exists

| Thing | Value |
| --- | --- |
| R2 bucket | `canceronice` (location `wnam`, same account as `bioconice`) |
| R2 Data Catalog | enabled on the bucket; backend warehouse `<account>_canceronice` |
| icegate catalog key / client `warehouse` | `canceronice` |
| Gateway Worker | `icegate-canceronice` → `https://icegate-canceronice.seandavi.workers.dev` |
| Gateway config | [`icegate.yaml`](../icegate.yaml) in this repo, baked into the Worker at deploy |

Same Cloudflare account as biocOnIce is acceptable **only because** nothing
restricted ever lands here (biocOnIce ADR-0005; SPEC.md § Licence gate).

## Secrets (Google Secret Manager, project `cdsci-infra`)

Naming and labels follow `monode/infrastructure/terraform/README.md`.

| Secret | Holds | Status |
| --- | --- | --- |
| `canceronice-icegate-key-seandavi` | ingest API key (read+write); digest in `icegate.yaml` | created 2026-09-18 |
| `canceronice-icegate-key-seandavi-ro` | personal read-only API key | created 2026-09-18 |
| `canceronice-cf-vending-ro` | bucket-scoped RO backend token (`icegate-canceronice-ro`) | minted 2026-09-18 |
| `canceronice-cf-vending-rw` | bucket-scoped RW backend token (`icegate-canceronice-rw`) | minted 2026-09-18 |
| `cdsci-cloudflare-workers-token`, `cdsci-r2-account-id` | shared deploy credentials | pre-existing |

Worker secrets (set by `scripts/provision-vending-tokens.sh`):
`CF_ACCOUNT_ID`, `R2_CATALOG_PREFIX`, `CF_API_TOKEN_RO`, `CF_API_TOKEN_RW`.
Unlike `icegate-bioconice`, no Admin-level token enters this Worker.

## Runbook

All Cloudflare calls below used `cdsci-cloudflare-api-token` (R2:Edit). The
icegate checkout's pinned wrangler (3.114) predates `r2 bucket catalog` and
`cors put`, so CORS and catalog enablement went through the REST API.

```sh
export CLOUDFLARE_API_TOKEN=$(gcloud secrets versions access latest --secret cdsci-cloudflare-api-token --project cdsci-infra)
export CLOUDFLARE_ACCOUNT_ID=$(gcloud secrets versions access latest --secret cdsci-r2-account-id --project cdsci-infra)
API=https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID
H="Authorization: Bearer $CLOUDFLARE_API_TOKEN"

# 1. bucket (from the icegate checkout, for its wrangler)
npx wrangler r2 bucket create canceronice --location wnam

# 2. CORS at creation — DuckDB-WASM reads data files straight from R2, and
#    icegate's CORS covers metadata only (this is bioconice's open explorer bug)
curl -X PUT -H "$H" -H 'Content-Type: application/json' $API/r2/buckets/canceronice/cors --data \
 '{"rules":[{"allowed":{"origins":["*"],"methods":["GET","HEAD"],"headers":["*"]},"exposeHeaders":["ETag","Content-Length","Content-Range","Accept-Ranges"],"maxAgeSeconds":3600}]}'

# 3. Data Catalog
curl -X POST -H "$H" $API/r2-catalog/canceronice/enable
curl -H "$H" $API/r2-catalog/canceronice      # status: active
```

4. **icegate API keys** — minted per icegate `docs/operators.md` §3 (`icegate_`
   + 32 base62), plaintext to Secret Manager, SHA-256 digest into
   `icegate.yaml`. To rotate: mint, `gcloud secrets versions add`, replace the
   digest, redeploy.
5. **First deploy** — `scripts/deploy-icegate.sh` (refuses uncommitted
   `icegate.yaml`). Must precede step 6: wrangler will not set a secret on a
   Worker that does not exist. Until step 6 the Worker answers 500.
6. **Vending tokens and Worker secrets** —
   `TOKEN_MINTER=<token> scripts/provision-vending-tokens.sh`. Needs a
   Cloudflare token with *Account · API Tokens · Edit*. On 2026-09-18 that
   permission was added to `monode-infra-tofu` (`cdsci-cloudflare-api-token`)
   and the script was run with it; a short-lived dashboard token works too.
7. **Verify** — rerun the verification block of `scripts/deploy-icegate.sh`,
   then reproduce biocOnIce ADR-0011's four-check evidence table against this
   bucket (vended read allowed; write 403; foreign bucket 403; catalog mutation
   via RO principal 403).

## Ingest

```sh
export CANCERONICE_URI=https://icegate-canceronice.seandavi.workers.dev
export CANCERONICE_TOKEN=$(gcloud secrets versions access latest --secret canceronice-icegate-key-seandavi --project cdsci-infra)
uv run canceronice init
```

## Rebuilding provenance.release for the source_version key (#78)

`provenance.release`'s business key changed from `(release, source)` to
`(release, source, source_version)`, and `source_version` had to become a
required column to be a valid Iceberg identifier field. The live table was
created under the old declaration, with `source_version` optional — `_evolve`
(`schemas.py`) detects this and refuses to promote it in place, because
pyiceberg cannot verify no existing row is actually NULL there. In practice
every row written so far does have a `source_version` (`merge.manifest`
always supplies one, defaulting to the retrieval date), so the rebuild below
is a schema-only change, not a backfill:

```python
from canceronice import catalog

cat = catalog()  # CANCERONICE_URI / CANCERONICE_TOKEN set, as in "Ingest" above
table = cat.load_table("provenance.release")
# allow_incompatible_changes=True: pyiceberg refuses optional -> required by
# default because it cannot check existing rows for NULL; every row here
# already has a source_version (see above), so this is safe.
with table.update_schema(allow_incompatible_changes=True) as update:
    update.update_column("source_version", required=True)
    update.set_identifier_fields("release", "source", "source_version")
```

Run once, before deploying code built on this PR. After it, `_evolve` sees
`source_version` already required and reconciles automatically on any future
declaration change.

## Known gaps

1. Steps 1–7 done 2026-09-18; access-control evidence is recorded on issue #15.
2. Bucket was created by hand, while `monode/infrastructure/INDEX.md` says
   OpenTofu owns R2 buckets — import or amend the convention (tracked).
3. No GCP uptime check on `/health` yet (tracked).
4. `icegate-canceronice` not yet listed in `INDEX.md`'s Cloudflare-hosted table (tracked).
5. `logpush: true` is uncommitted in the local icegate checkout; a deploy from
   a fresh clone silently loses access logs (inherited from biocOnIce).
