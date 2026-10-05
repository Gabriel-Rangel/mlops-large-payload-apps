#!/usr/bin/env bash
# Remove o exemplo AWS (pede confirmação). Os objetos do Unity Catalog saem com
# `databricks bundle destroy` e `databricks external-locations/storage-credentials delete`.
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1}
PREFIX=${PREFIX:-lp-apps}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
read -r -p "Apagar a stack $PREFIX, os buckets, a role do Unity Catalog e o secret na conta $ACCOUNT? [y/N] " ok
[[ "$ok" == "y" ]] || exit 1

aws s3 rm "s3://$PREFIX-payloads-$ACCOUNT" --recursive --quiet || true
aws cloudformation delete-stack --stack-name "$PREFIX"
aws cloudformation wait stack-delete-complete --stack-name "$PREFIX"
aws s3 rb "s3://$PREFIX-cfn-artifacts-$ACCOUNT" --force || true
aws iam delete-role-policy --role-name "$PREFIX-uc-payloads-role" --policy-name unity-catalog-bucket-access || true
aws iam delete-role --role-name "$PREFIX-uc-payloads-role" || true
aws secretsmanager delete-secret --secret-id mlops-large-payload-apps/databricks-client --force-delete-without-recovery || true
