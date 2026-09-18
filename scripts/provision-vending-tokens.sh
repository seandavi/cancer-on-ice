#!/usr/bin/env bash
# One-time: mint the bucket-scoped RO/RW backend tokens (biocOnIce ADR-0011),
# archive them in Secret Manager, and set the four Worker secrets.
#
# Needs a Cloudflare API token holding "Account API Tokens Write" — no token in
# cdsci-infra has that scope, so create a short-lived one in the dashboard
# (My Profile → API Tokens → "Create additional tokens" template, this account
# only), pass it as TOKEN_MINTER, and revoke it afterwards.
#
# The Worker must already exist (scripts/deploy-icegate.sh): wrangler refuses
# `secret put` on a script that was never deployed.
#
# ponytail: not idempotent on the Cloudflare side — a rerun mints a second pair
# with the same names. Revoke the old pair in the dashboard if you rerun.
#
# Usage: TOKEN_MINTER=<token> scripts/provision-vending-tokens.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
ICEGATE_DIR=${ICEGATE_DIR:-$HERE/../icegate}
BUCKET=canceronice
WORKER=icegate-canceronice
PROJECT=cdsci-infra
: "${TOKEN_MINTER:?set TOKEN_MINTER to a Cloudflare token with Account API Tokens Write}"

gsm() { gcloud secrets versions access latest --secret "$1" --project $PROJECT; }
ACCT=$(gsm cdsci-r2-account-id)

echo "==> minting icegate-$BUCKET-ro / -rw"
OUT=$(CF_ACCOUNT_ID=$ACCT R2_BUCKET=$BUCKET CLOUDFLARE_API_TOKEN=$TOKEN_MINTER bash "$ICEGATE_DIR/scripts/create-backend-tokens.sh")
RO=$(printf '%s\n' "$OUT" | sed -n 's/^CF_API_TOKEN_RO=//p')
RW=$(printf '%s\n' "$OUT" | sed -n 's/^CF_API_TOKEN_RW=//p')
[ -n "$RO" ] && [ -n "$RW" ] || { echo "token minting returned nothing" >&2; exit 1; }

store() { # name value mode scopes
  if gcloud secrets describe "$1" --project $PROJECT >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- --project $PROJECT >/dev/null
  else
    # annotation values use ';' — gcloud's dict parser splits on ','
    printf %s "$2" | gcloud secrets create "$1" --data-file=- --project $PROJECT --replication-policy=automatic \
      --labels=type=api-token,subject=cloudflare,scope=canceronice,managed-by=manual \
      --set-annotations="purpose=icegate-$BUCKET backend $3 vending token (biocOnIce ADR-0011),consumed-by=Worker $WORKER secret,rotated=$(date +%F),cf-token-name=icegate-$BUCKET-$3,cf-token-type=account,scopes=$4" >/dev/null
  fi
  echo "    stored $1"
}
store canceronice-cf-vending-ro "$RO" ro "Workers R2 Data Catalog Read; Workers R2 Storage Bucket Item Read (bucket=$BUCKET)"
store canceronice-cf-vending-rw "$RW" rw "Workers R2 Data Catalog Write; Workers R2 Storage Bucket Item Write (bucket=$BUCKET)"

echo "==> discovering catalog prefix with the RO token (also proves it works)"
PREFIX=$(curl -sf -H "Authorization: Bearer $RO" \
  "https://catalog.cloudflarestorage.com/$ACCT/$BUCKET/v1/config?warehouse=${ACCT}_$BUCKET" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["overrides"]["prefix"])')

echo "==> setting Worker secrets on $WORKER"
export CLOUDFLARE_API_TOKEN=$(gsm cdsci-cloudflare-workers-token) CLOUDFLARE_ACCOUNT_ID=$ACCT
cd "$ICEGATE_DIR"
put() { printf %s "$2" | npx wrangler secret put "$1" --name $WORKER >/dev/null && echo "    $1"; }
put CF_ACCOUNT_ID "$ACCT"
put R2_CATALOG_PREFIX "$PREFIX"
put CF_API_TOKEN_RO "$RO"
put CF_API_TOKEN_RW "$RW"

echo "==> verifying anonymous read through the gateway"
curl -sf "https://$WORKER.seandavi.workers.dev/v1/$BUCKET/namespaces" && echo
echo "Done. Revoke TOKEN_MINTER in the Cloudflare dashboard now."
