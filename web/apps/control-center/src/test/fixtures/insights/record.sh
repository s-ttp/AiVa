#!/usr/bin/env bash
# Re-record the Insights fixtures from a local gateway: simulated inference, synthetic traffic (ADR-0015).
#
#   bash scripts/dev-services.sh && make migrate seed      # on an empty database
#   uv run aiva-admin traffic --runs 60 && uv run aiva-admin aggregate
#   AIVA_DEV_SIGN_IN=true make dev                          # in another shell
#   bash web/apps/control-center/src/test/fixtures/insights/record.sh
#
# The window is the seven UTC days ending today (override with FROM and TO). Responses are written as they
# come back, so the fixtures match the current contract; adjust the Insights tests if the figures move.
set -euo pipefail
G=${GATEWAY_URL:-http://localhost:8000}
OUT=$(cd "$(dirname "$0")" && pwd)
TO=${TO:-$(date -u +%F)}
FROM=${FROM:-$(date -u -d "$TO -6 days" +%F)}
W="from=$FROM&to=$TO"

tok() {
  curl -sf -X POST "$G/v1/auth/dev/token" -H 'content-type: application/json' -d "{\"user_id\":\"$1\"}" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}
get() {
  curl -sf -H "Authorization: Bearer $1" "$G$2" | python3 -m json.tool --indent 2 > "$OUT/$3.json"
  echo "$3"
}

FIN=$(tok u-finance)
ADM=$(tok u-tenant-admin)
MGR=$(tok u-care-manager)

get "$FIN" "/v1/reports/overview?$W" overview
# The Story-mode figures (flow, packing, tokens by stage, frontier comparison) on the same report.
get "$FIN" "/v1/reports/overview?$W" valueOverview
get "$FIN" "/v1/reports/filters?$W" filters
get "$FIN" "/v1/reports/consumption?$W&group_by=department&limit=25" consumptionDepartment
get "$FIN" "/v1/reports/consumption?$W&group_by=application&limit=100" consumptionApplication
get "$FIN" "/v1/reports/consumption?$W&group_by=user&limit=100" consumptionUser
get "$FIN" "/v1/reports/classification?$W&group_by=task_category" classificationTask
get "$FIN" "/v1/reports/optimization?$W&group_by=model" optimizationModel
get "$FIN" "/v1/reports/optimization?$W&group_by=strategy" optimizationStrategy
get "$FIN" "/v1/reports/optimization?$W&group_by=application" optimizationApplication
get "$FIN" "/v1/reports/governance?$W&group_by=application" governanceApplication
get "$FIN" "/v1/reports/runs?$W&application_id=gct-fraud-investigator&limit=25" runsFraud
TRACE=$(python3 -c "import json; print(json.load(open('$OUT/runsFraud.json'))['runs'][0]['trace_id'])")
get "$FIN" "/v1/reports/runs/$TRACE" runDetailSovereign
get "$MGR" "/v1/reports/consumption?$W&group_by=application&limit=100" consumptionApplicationCareManager
get "$ADM" "/v1/auth/me" profileTenantAdmin
get "$MGR" "/v1/auth/me" profileCareManager
get "$FIN" "/v1/admin/budgets" budgets
get "$FIN" "/v1/admin/budgets/alerts?limit=200" alerts

# One completed export of the consumption report, for the Export Center.
curl -sf -X POST -H "Authorization: Bearer $FIN" -H 'content-type: application/json' "$G/v1/reports/exports" \
  -d "{\"report\":\"consumption\",\"group_by\":\"application\",\"window\":{\"from\":\"$FROM\",\"to\":\"$TO\"},\"filters\":{}}" \
  > /dev/null
for _ in $(seq 1 20); do
  curl -sf -H "Authorization: Bearer $FIN" "$G/v1/reports/exports" |
    python3 -c 'import json, sys; sys.exit(0 if json.load(sys.stdin)["exports"][0]["status"] == "completed" else 1)' &&
    break
  sleep 0.5
done
get "$FIN" "/v1/reports/exports" exports
