#!/usr/bin/env bash
# Teste ponta a ponta a partir da AWS: a Lambda simuladora faz o POST na App e espera a decisão
# chegar pelo SNS. Os resultados vão para build/e2e_results.jsonl.
#
#   aws/run_e2e.sh 15 22 29 32
#
# Variáveis: AWS_PROFILE, PREFIX (padrão lp-apps), DATABRICKS_PROFILE, APP_NAME (padrão large-payload-scoring-dev).
set -euo pipefail

export AWS_REGION=${AWS_REGION:-us-east-1}
PREFIX=${PREFIX:-lp-apps}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/build/e2e_results.jsonl"
mkdir -p "$ROOT/build"
APP_URL=$(databricks apps get "${APP_NAME:-large-payload-scoring-dev}" ${DATABRICKS_PROFILE:+-p "$DATABRICKS_PROFILE"} -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')

for size in "${@:-29}"; do
  aws lambda invoke --function-name "$PREFIX-credit-engine-sim" --cli-binary-format raw-in-base64-out \
    --cli-read-timeout 330 --payload "{\"payload_mb\": $size, \"app_url\": \"$APP_URL\"}" "$ROOT/build/e2e_last.json" >/dev/null
  cat "$ROOT/build/e2e_last.json" >> "$OUT" && echo >> "$OUT"
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print({k: r.get(k) for k in ("payload_mb","ok","time_to_202_ms","e2e_ms","prediction","http")})' "$ROOT/build/e2e_last.json"
done
