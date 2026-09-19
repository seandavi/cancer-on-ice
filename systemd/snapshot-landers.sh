#!/usr/bin/env bash
# Weekly snapshot landers (#107): FDA MQSA and HRSA sites/HPSA overwrite themselves
# upstream and have no archive, so every missed week is history nobody can recover.
# Run by canceronice-snapshots.service; convention: monode/infrastructure/SCHEDULING.md.
set -uo pipefail

export CANCERONICE_URI=https://icegate-canceronice.seandavi.workers.dev
# Fetched at run time, never written to disk.
CANCERONICE_TOKEN=$(gcloud secrets versions access latest \
  --secret=canceronice-icegate-key-seandavi --project=cdsci-infra) || exit 1
export CANCERONICE_TOKEN

# Dated label so weekly snapshots inside one monthly release keep their order:
# '2026.09' < '2026.09.19' < '2026.09.26' < '2026.10' as strings, which is how
# merge.py compares releases. ponytail: day-of-month stands in for real facility
# dates until #19 decides the validity columns.
release=$(date +%Y.%m.%d)

rc=0
for lander in mqsa hrsa-sites; do
  # One failing must not cost the other its week.
  "$HOME/.local/bin/uv" run canceronice "$lander" --release "$release" || rc=1
done
exit $rc
