"""Cria o service principal do Credit Engine e guarda o OAuth secret (idempotente).

  credit-engine-client   quem chama a App. Recebe o entitlement `workspace-access` (sem ele, a App
                         redireciona o token para a tela de login) e `CAN_USE` na App (pelo bundle).

O secret vai para o secret scope do Databricks (chaves `client-id` / `client-secret`, usadas pelo
job de validação) e, opcionalmente, para o AWS Secrets Manager (usado pela Lambda de teste).
Nenhum valor de secret é impresso. Rodar de novo mantém o secret existente, exceto com --rotate.

Uso:
  python scripts/setup_identities.py --profile <PERFIL_DATABRICKS> \
      [--aws-profile <PERFIL_AWS> --aws-secret-name mlops-large-payload-apps/databricks-client]
"""

from __future__ import annotations

import argparse
import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import Patch, PatchOp, PatchSchema

SP_NAME = "credit-engine-client"


def ensure_service_principal(w: WorkspaceClient, name: str):
    found = list(w.service_principals.list(filter=f'displayName eq "{name}"'))
    sp = found[0] if found else w.service_principals.create(display_name=name, active=True)
    if not found:
        print(f"Service principal criado: {name}")
    if "workspace-access" not in {e.value for e in (sp.entitlements or [])}:
        w.service_principals.patch(
            sp.id,
            operations=[Patch(op=PatchOp.ADD, path="entitlements", value=[{"value": "workspace-access"}])],
            schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )
        print(f"  entitlement workspace-access adicionado a {name}")
    return sp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="perfil da Databricks CLI")
    parser.add_argument("--scope", default="mlops-large-payload-apps", help="secret scope do Databricks")
    parser.add_argument("--aws-profile", help="perfil da AWS CLI (omita para não gravar no Secrets Manager)")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--aws-secret-name", default="mlops-large-payload-apps/databricks-client")
    parser.add_argument("--rotate", action="store_true", help="gera um novo OAuth secret mesmo se já existir")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    if args.scope not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(scope=args.scope)
        print(f"Secret scope criado: {args.scope}")
    stored = {s.key for s in w.secrets.list_secrets(scope=args.scope)}

    sm = None
    aws_missing = False
    if args.aws_profile:
        import boto3

        sm = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region).client("secretsmanager")
        try:
            sm.describe_secret(SecretId=args.aws_secret_name)
        except sm.exceptions.ResourceNotFoundException:
            aws_missing = True

    sp = ensure_service_principal(w, SP_NAME)
    print(f"{SP_NAME}: application_id={sp.application_id}")
    if not args.rotate and not aws_missing and {"client-id", "client-secret"} <= stored:
        print(f"  OAuth secret já guardado em {args.scope} (use --rotate para trocar)")
        return

    # Um service principal pode ter vários secrets; os anteriores continuam válidos até expirarem.
    secret = w.service_principal_secrets_proxy.create(service_principal_id=sp.id)
    w.secrets.put_secret(scope=args.scope, key="client-id", string_value=sp.application_id)
    w.secrets.put_secret(scope=args.scope, key="client-secret", string_value=secret.secret)
    print(f"  OAuth secret guardado em {args.scope}/client-secret (expira em {secret.expire_time})")

    if sm is not None:
        value = json.dumps({"client_id": sp.application_id, "client_secret": secret.secret, "host": w.config.host})
        if aws_missing:
            sm.create_secret(Name=args.aws_secret_name, SecretString=value,
                             Description=f"OAuth M2M do service principal Databricks {SP_NAME}")
            print(f"  AWS secret criado: {args.aws_secret_name}")
        else:
            sm.put_secret_value(SecretId=args.aws_secret_name, SecretString=value)
            print(f"  AWS secret atualizado: {args.aws_secret_name}")


if __name__ == "__main__":
    main()
