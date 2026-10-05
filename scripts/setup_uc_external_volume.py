"""Registra um bucket S3 no Unity Catalog para servir de Volume EXTERNAL (idempotente).

Passos:
  1. IAM role (trust do Unity Catalog + self-assume) com acesso ao bucket
  2. Storage credential apontando para a role
  3. Trust policy final com o principal do Unity Catalog e o external ID da credential
  4. External location em s3://<bucket>/

Depois disso o bundle cria o Volume EXTERNAL (resources/uc.yml) num prefixo do bucket, e
  s3://<bucket>/<prefixo>/dt=.../<id>/request.json  ==  /Volumes/<catálogo>/<schema>/<volume>/dt=.../<id>/request.json

Uso:
  python scripts/setup_uc_external_volume.py --profile <PERFIL_DATABRICKS> --aws-profile <PERFIL_AWS> \
      --bucket <BUCKET> --name <NOME_UC> --role-name <NOME_DA_ROLE>

Observação: a AWS pode levar ~10 min para aceitar a role recém-criada; o script espera.
"""

from __future__ import annotations

import argparse
import json
import time

import boto3
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import AwsIamRoleRequest


def trust_policy(principals: list[str], external_id: str | None) -> str:
    statement: dict = {"Effect": "Allow", "Principal": {"AWS": principals}, "Action": "sts:AssumeRole"}
    if external_id:
        statement["Condition"] = {"StringEquals": {"sts:ExternalId": external_id}}
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="perfil da Databricks CLI")
    parser.add_argument("--aws-profile", required=True, help="perfil da AWS CLI")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--bucket", required=True, help="bucket S3 (já existente)")
    parser.add_argument("--name", required=True, help="nome da storage credential e da external location")
    parser.add_argument("--role-name", required=True, help="nome da IAM role criada para o Unity Catalog")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    account = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    role_arn = f"arn:aws:iam::{account}:role/{args.role_name}"
    w = WorkspaceClient(profile=args.profile)

    # 1. Role com trust provisória (a própria conta); a trust final depende da credential (passo 3).
    try:
        iam.get_role(RoleName=args.role_name)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(
            RoleName=args.role_name,
            AssumeRolePolicyDocument=trust_policy([f"arn:aws:iam::{account}:root"], None),
            Description=f"Unity Catalog: acesso ao bucket {args.bucket}",
        )
        print(f"IAM role criada: {role_arn}")
    iam.put_role_policy(
        RoleName=args.role_name,
        PolicyName="unity-catalog-bucket-access",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
                            "s3:GetBucketLocation", "s3:ListBucketMultipartUploads",
                            "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload",
                        ],
                        "Resource": [f"arn:aws:s3:::{args.bucket}", f"arn:aws:s3:::{args.bucket}/*"],
                    },
                    # O Unity Catalog exige que a role consiga assumir a si mesma.
                    {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": role_arn},
                ],
            }
        ),
    )

    # 2. Storage credential: devolve o principal do Unity Catalog e o external ID a confiar.
    try:
        cred = w.storage_credentials.get(args.name)
    except NotFound:
        cred = w.storage_credentials.create(name=args.name, aws_iam_role=AwsIamRoleRequest(role_arn=role_arn))
        print(f"Storage credential criada: {args.name}")
    uc_principal = cred.aws_iam_role.unity_catalog_iam_arn
    external_id = cred.aws_iam_role.external_id

    # 3. Trust final. A role recém-criada só é aceita como principal depois de propagar no IAM.
    for attempt in range(30):
        try:
            iam.update_assume_role_policy(
                RoleName=args.role_name, PolicyDocument=trust_policy([uc_principal, role_arn], external_id)
            )
            break
        except iam.exceptions.MalformedPolicyDocumentException:
            print(f"  aguardando propagação da role no IAM ({attempt + 1})")
            time.sleep(10)
    else:
        raise SystemExit("Não foi possível gravar a trust policy da role")
    print("Trust policy: Unity Catalog + self-assume, com external ID")

    # 4. External location. O Unity Catalog pode levar ~10 min para conseguir assumir a role nova.
    url = f"s3://{args.bucket}/"
    for attempt in range(40):
        try:
            loc = w.external_locations.get(args.name)
            print(f"External location: {loc.name} → {loc.url}")
            return
        except NotFound:
            pass
        try:
            loc = w.external_locations.create(name=args.name, url=url, credential_name=args.name)
            print(f"External location criada: {loc.name} → {loc.url}")
            return
        except Exception as exc:  # noqa: BLE001 — normalmente propagação do IAM
            print(f"  tentativa {attempt + 1}: {type(exc).__name__}: {str(exc)[:140]}")
            time.sleep(15)
    raise SystemExit("External location não criada; confira a trust policy e a política S3 da role")


if __name__ == "__main__":
    main()
