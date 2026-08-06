#!/usr/bin/env python3
"""
Migrate an AWS Secrets Manager secret from the legacy .env-blob format used by
the ECS services to the JSON format ESO_AWSSecretsManager expects in Devtron,
under a new secret name.

Flow (matches the onboard-devtron-app skill's secret-key convention —
onemi/{env}/{base_service_name}/app-credentials):

  1. Fetch the OLD secret's raw value and print it.
  2. Parse it as .env (KEY=VALUE lines) and convert to JSON, print that.
  3. Ask for confirmation.
  4. Create (or, if it already exists, optionally update) the NEW secret with
     that JSON as its value.

This calls the AWS CLI directly (`aws secretsmanager ...`), the same way
ecs.sh does elsewhere in this repo — no extra Python dependencies. It runs
against whatever AWS credentials/profile are already active in your shell.

Usage:
    python3 scripts/migrate_secret.py \\
        --old-secret onemi/dev/admin-service/legacy-credentials \\
        --new-secret onemi/dev/admin-service-go/app-credentials \\
        --region ap-south-1

    # non-interactive (CI/scripted use only — skips the confirmation prompt):
    python3 scripts/migrate_secret.py --old-secret ... --new-secret ... --yes
"""
import argparse
import json
import subprocess
import sys

DEFAULT_REGION = "ap-south-1"


def run_aws(args, region, profile):
    cmd = ["aws"] + args + ["--region", region]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def fetch_secret_value(secret_id, region, profile):
    result = run_aws(
        ["secretsmanager", "get-secret-value", "--secret-id", secret_id, "--query", "SecretString", "--output", "text"],
        region,
        profile,
    )
    if result.returncode != 0:
        print(f"ERROR: failed to fetch secret {secret_id!r}:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.rstrip("\n")


def secret_exists(secret_id, region, profile):
    result = run_aws(["secretsmanager", "describe-secret", "--secret-id", secret_id], region, profile)
    return result.returncode == 0


def parse_env_blob(raw):
    """Parse KEY=VALUE lines (the .env format the ECS secrets were stored
    in) into an ordered dict. Blank lines and #-comments are skipped; values
    may contain '=' so only the first '=' on a line is treated as the
    separator; surrounding quotes on the value are stripped."""
    parsed = {}
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            print(f"WARNING: skipping line {lineno} (no '=' found): {stripped!r}", file=sys.stderr)
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        parsed[key] = value
    return parsed


def confirm(prompt):
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def create_or_update_secret(secret_id, json_string, region, profile):
    if secret_exists(secret_id, region, profile):
        print(f"\nSecret {secret_id!r} already exists.")
        if not confirm(f"Update it in place with the JSON above (put-secret-value)?"):
            print("Aborted — existing secret left unchanged.")
            return False
        result = run_aws(
            ["secretsmanager", "put-secret-value", "--secret-id", secret_id, "--secret-string", json_string],
            region,
            profile,
        )
    else:
        result = run_aws(
            ["secretsmanager", "create-secret", "--name", secret_id, "--secret-string", json_string],
            region,
            profile,
        )
    if result.returncode != 0:
        print(f"ERROR: failed to write secret {secret_id!r}:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(result.stdout)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-secret", required=True, help="Existing secret name/ARN, stored in .env (KEY=VALUE) format")
    parser.add_argument("--new-secret", required=True, help="New secret name to create/update, e.g. onemi/{env}/{base_service_name}/app-credentials")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None, help="AWS CLI profile to use, if not the default")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (non-interactive use only)")
    args = parser.parse_args()

    print(f"Fetching old secret {args.old_secret!r} (region {args.region})...")
    raw = fetch_secret_value(args.old_secret, args.region, args.profile)

    print("\n" + "=" * 70)
    print(f"OLD SECRET ({args.old_secret}) — raw .env-format value:")
    print("=" * 70)
    print(raw)

    parsed = parse_env_blob(raw)
    json_string = json.dumps(parsed, indent=2)

    print("\n" + "=" * 70)
    print(f"NEW SECRET ({args.new_secret}) — converted to JSON:")
    print("=" * 70)
    print(json_string)
    print("=" * 70)

    if not args.yes:
        if not confirm(f"\nCreate/update {args.new_secret!r} in Secrets Manager with the JSON above?"):
            print("Aborted — nothing was written.")
            return 1

    create_or_update_secret(args.new_secret, json_string, args.region, args.profile)
    print(f"\nDone — {args.new_secret!r} now holds the converted JSON.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
