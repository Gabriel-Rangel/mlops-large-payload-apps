#!/usr/bin/env bash
# Implanta o exemplo AWS (CloudFormation): bucket de payloads, notificação S3 → SNS → inbox e a
# Lambda que simula o Credit Engine. Não precisa de SAM; só da AWS CLI.
#
#   aws/deploy.sh
#
# Variáveis: AWS_PROFILE, AWS_REGION (padrão us-east-1), PREFIX (padrão lp-apps).
set -euo pipefail

export AWS_REGION=${AWS_REGION:-us-east-1}
PREFIX=${PREFIX:-lp-apps}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/build/lambda"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="$PREFIX-cfn-artifacts-$ACCOUNT"

# Bucket privado para o código empacotado das Lambdas.
if ! aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ARTIFACTS" >/dev/null
  aws s3api put-public-access-block --bucket "$ARTIFACTS" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

# Pacote da Lambda: só biblioteca padrão + boto3 (já presente no runtime), sem layer.
rm -rf "$BUILD/credit_engine_sim" && mkdir -p "$BUILD/credit_engine_sim"
cp "$ROOT/aws/credit_engine_sim.py" "$ROOT/aws/dbx_http.py" "$ROOT/src/common/payloads.py" "$BUILD/credit_engine_sim/"

aws cloudformation package --template-file "$ROOT/aws/template.yaml" --s3-bucket "$ARTIFACTS" \
  --s3-prefix "$PREFIX" --output-template-file "$BUILD/template.packaged.yaml" >/dev/null
aws cloudformation deploy --template-file "$BUILD/template.packaged.yaml" --stack-name "$PREFIX" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "Prefix=$PREFIX" --tags project=mlops-large-payload-apps
aws cloudformation describe-stacks --stack-name "$PREFIX" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
