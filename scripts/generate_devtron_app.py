#!/usr/bin/env python3
"""
Generate devtron-cli (tron) config.yaml + base/override values.yaml files for a
new service, following the exact conventions already used by the migrated apps
in this repo (admin-gateway-go, admin-service-go-consumer, calculate-rules-consumer-go,
go-loan-service-kafka-consumer-dlq, merchant-gateway-go).

This script does NOT call the Devtron API. It only writes files to disk. Review
the generated files, then run `tron --config <file> create-app` yourself.

Usage:
    python3 scripts/generate_devtron_app.py --answers path/to/answers.yaml
    python3 scripts/generate_devtron_app.py --answers path/to/answers.yaml --out-dir . --force

    # multiple apps in one run — pass several files, each is generated
    # independently (one bad file doesn't stop the rest; exit code is
    # non-zero if any failed):
    python3 scripts/generate_devtron_app.py --answers app-a.yaml app-b.yaml app-c.yaml

See scripts/examples/answers-consumer.example.yaml and
    scripts/examples/answers-web.example.yaml for the input schema.
"""
import argparse
import sys
from pathlib import Path

import yaml

# ------------------------------------------------------------------------------
# Fixed conventions (do not ask the user about these — they don't vary per app)
# ------------------------------------------------------------------------------

ACCOUNT_ID = {
    "dev": "451914973813",
    "qa": "451914973813",
    "prod": "183716846541",
}

DEFAULT_REGISTRY = {
    "dev": "ring-dev",
    "qa": "ring-dev",
    "prod": "onemi-prod-ecr",
}

CHART_VERSION = "5.2.0"
CHART_TYPE = "Rollout Deployment"
REGION = "ap-south-1"

DEFAULT_GIT_ACCOUNT = {
    "dev": "Bitbucket-Sulaim2",
    "qa": "Bitbucket-Sulaim2",
    "prod": "Bitbucket",
}

ENV_LABEL = {"dev": "eks-dev", "qa": "eks-qa", "prod": "eks-prod"}


def env_name(env, team_code):
    return f"{env}-{team_code}"


def per_env_value(value, env, default):
    """Accept either a flat scalar (applies to every env the same) or a
    per-env dict like {dev: x, qa: y, prod: z}. Missing/None falls back to
    default. Used for autoscaling.max/cpu_target/mem_target, which often
    need to differ per env (e.g. a lower ceiling in qa than prod) but don't
    have to."""
    if isinstance(value, dict):
        return value.get(env, default)
    if value is None:
        return default
    return value


def irsa_role_arn(env, base_service_name):
    # base_service_name is entered WITH its "-go" suffix (e.g. "admin-service-go",
    # "merchant-gateway-go") to match the Secrets Manager key convention below —
    # but the IRSA role name itself always drops "-go"
    # (onemi-app-dev-admin-service-irsa-role, not ...-admin-service-go-irsa-role).
    role_suffix = base_service_name[:-3] if base_service_name.endswith("-go") else base_service_name
    return f"arn:aws:iam::{ACCOUNT_ID[env]}:role/onemi-app-{env}-{role_suffix}-irsa-role"


def secret_manager_key(env, base_service_name):
    return f"onemi/{env}/{base_service_name}/app-credentials"


def devtron_secret_name(app_name):
    return f"aws-asm-{app_name}"


def sa_name(app_name, service_account_cfg):
    if not service_account_cfg.get("create", True):
        existing = service_account_cfg.get("name")
        if not existing:
            raise ValueError(
                "service_account.create is false but service_account.name is not set — "
                "you must supply the existing ServiceAccount name to reuse."
            )
        return existing
    return f"{app_name}-sa"


# ------------------------------------------------------------------------------
# Reusable values.yaml building blocks
# ------------------------------------------------------------------------------

def common_labels(env, project_name, app_name, business_unit, brand, workload_type, criticality, language, include_service=True):
    labels = {
        "business-unit": business_unit,
        "brand": brand,
        "env": ENV_LABEL[env],
        "workload-type": workload_type,
        "criticality": criticality,
        "language": language,
        "team": project_name,
    }
    if include_service:
        labels["service"] = app_name
    return labels


def lifecycle_common():
    return {
        "lifecycle": {
            "enabled": True,
            "preStop": {"exec": {"command": ["/bin/sh", "-c", "sleep 15"]}},
        }
    }


def volumes_common():
    return (
        [{"name": "tmp-dir", "emptyDir": {}}],
        [{"name": "tmp-dir", "mountPath": "/tmp"}],
    )


def full_spot_placement():
    """Pod placement: full-spot. Used by consumers in every env, and by
    gateways in dev. Schedules only onto spot capacity."""
    return {
        "affinity": {
            "enabled": True,
            "values": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "karpenter.sh/capacity-type",
                                        "operator": "In",
                                        "values": ["spot"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        },
        "tolerations": [
            {
                "key": "karpenter.sh/capacity-type",
                "operator": "Equal",
                "value": "spot",
                "effect": "NoSchedule",
            }
        ],
    }


def fifty_fifty_placement(app_name):
    """Pod placement: '50/50'. Spread pods across distinct nodes via
    podAntiAffinity on hostname; no explicit spot/on-demand constraint."""
    return {
        "affinity": {
            "enabled": True,
            "values": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {"matchLabels": {"app": app_name}},
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            },
        }
    }


def min_domain_placement(app_name, release_name):
    """Pod placement: 'min-domain'. podAntiAffinity by hostname PLUS a
    topologySpreadConstraint requiring at least 2 capacity-type domains
    (spot + on-demand). appId/envId are Devtron-internal and only known
    after the app + environment exist in the UI — fill them in after
    `tron create-app` and re-apply with `update-app`."""
    placement = fifty_fifty_placement(app_name)
    placement["podExtraSpecs"] = {
        "topologySpreadConstraints": [
            {
                "maxSkew": 1,
                "topologyKey": "karpenter.sh/capacity-type",
                "whenUnsatisfiable": "DoNotSchedule",
                "minDomains": 2,
                "labelSelector": {
                    "matchLabels": {
                        "app": app_name,
                        "appId": "<FILL_AFTER_CREATE>",
                        "envId": "<FILL_AFTER_CREATE>",
                        "release": release_name,
                    }
                },
            },
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "DoNotSchedule",
                "labelSelector": {
                    "matchLabels": {
                        "app": app_name,
                        "appId": "<FILL_AFTER_CREATE>",
                        "envId": "<FILL_AFTER_CREATE>",
                        "release": release_name,
                    }
                },
            },
        ]
    }
    return placement


VALID_QA_PROD_STRATEGIES = ("full-spot", "fifty-fifty", "min-domain")


def resolve_placement(app_type, env, qa_prod_strategy, app_name, release_name):
    if app_type == "consumer":
        return full_spot_placement()
    # web
    if env == "dev":
        return full_spot_placement()
    if qa_prod_strategy == "min-domain":
        return min_domain_placement(app_name, release_name)
    if qa_prod_strategy == "fifty-fifty":
        return fifty_fifty_placement(app_name)
    if qa_prod_strategy == "full-spot":
        return full_spot_placement()
    raise ValueError(
        f"Unknown qa_prod_strategy: {qa_prod_strategy!r} — must be one of {VALID_QA_PROD_STRATEGIES}"
    )


def probes_block(app_type, port, path):
    if app_type == "consumer":
        return {"LivenessProbe": {"enabled": False}, "ReadinessProbe": {"enabled": False}}
    return {
        "LivenessProbe": {
            "Path": path,
            "failureThreshold": 3,
            "initialDelaySeconds": 60,
            "periodSeconds": 60,
            "port": port,
            "scheme": "HTTP",
            "successThreshold": 1,
            "tcp": False,
            "timeoutSeconds": 5,
        },
        "ReadinessProbe": {
            "Path": path,
            "failureThreshold": 3,
            "initialDelaySeconds": 10,
            "periodSeconds": 20,
            "port": port,
            "scheme": "HTTP",
            "successThreshold": 1,
            "tcp": False,
            "timeoutSeconds": 5,
        },
    }


def autoscaling_block(app_type, engine, min_r, max_r, cpu_target, mem_target):
    """Consumers always use KEDA with min==max (fixed replica count driven by
    lag/cpu, not elastic). Gateways can use plain HPA ('hpa') or KEDA ('keda') —
    match whatever the rest of that app's siblings already use."""
    if app_type == "consumer" or engine == "keda":
        return {
            "autoscaling": {"enabled": False},
            "kedaAutoscaling": {
                "enabled": True,
                "minReplicaCount": min_r,
                "maxReplicaCount": max_r,
                "triggers": [
                    {"type": "cpu", "metricType": "Utilization", "metadata": {"value": str(cpu_target)}},
                    {"type": "memory", "metricType": "Utilization", "metadata": {"value": str(mem_target)}},
                ],
            },
        }
    # plain HPA style (matches admin-gateway-go-prod)
    return {
        "autoscaling": {
            "enabled": True,
            "MinReplicas": min_r,
            "MaxReplicas": max_r,
            "TargetCPUUtilizationPercentage": cpu_target,
            "TargetMemoryUtilizationPercentage": mem_target,
            "containerResource": {
                "enabled": False,
                "TargetCPUUtilizationPercentage": cpu_target,
                "TargetMemoryUtilizationPercentage": mem_target,
            },
            "annotations": {},
            "behavior": {},
            "extraMetrics": [],
            "labels": {},
        }
    }


def ingress_block(env, ingress_cfg, app_name):
    scheme = ingress_cfg["scheme"]
    # hosts.{env} may be a single host string or a list of host strings —
    # every host in the list gets the same pathType/paths.
    hosts_cfg = ingress_cfg["hosts"][env]
    host_names = [hosts_cfg] if isinstance(hosts_cfg, str) else hosts_cfg
    return {
        "enabled": True,
        "className": "alb",
        "annotations": {
            "kubernetes.io/ingress.class": "alb",
            "alb.ingress.kubernetes.io/scheme": scheme,
            "alb.ingress.kubernetes.io/target-type": "ip",
            "alb.ingress.kubernetes.io/group.name": ingress_cfg["alb_group_name"][env],
            "alb.ingress.kubernetes.io/group.order": str(ingress_cfg.get("alb_group_order", "10")),
            "alb.ingress.kubernetes.io/healthcheck-path": ingress_cfg["healthcheck_path"],
            "alb.ingress.kubernetes.io/healthcheck-protocol": "HTTP",
            "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP": 80}, {"HTTPS": 443}]',
            "alb.ingress.kubernetes.io/certificate-arn": ingress_cfg["certificate_arn"][env],
            "alb.ingress.kubernetes.io/ssl-policy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
            "alb.ingress.kubernetes.io/ssl-redirect": "443",
        },
        "hosts": [
            {"host": h, "pathType": "ImplementationSpecific", "paths": ["/*"]}
            for h in host_names
        ],
        "labels": {},
        "tls": [],
    }


# ------------------------------------------------------------------------------
# base-values.yaml / override-values.yaml assembly
# ------------------------------------------------------------------------------

def build_values(env, answers, is_base):
    """Build one values.yaml dict (used both for the base file and per-env
    override file — same shape, different per-env numbers)."""
    app_name = answers["app_name"]
    app_type = answers["app_type"]
    team_code = answers["team_code"]
    project_name = answers["project_name"]
    business_unit = answers.get("business_unit", "onemi")
    brand = answers.get("brand", "Ring")
    workload_type = answers["workload_type"]
    criticality = answers["criticality"]
    language = answers.get("language", "GO")
    base_service_name = answers["base_service_name"]
    res = answers["resources"][env]
    port = answers.get("ingress", {}).get("container_port", 8080)
    path = answers.get("ingress", {}).get("healthcheck_path", "/health")
    release_name = f"{app_name}-{env_name(env, team_code)}"

    volumes, volume_mounts = volumes_common()

    values = {}
    values["GracePeriod"] = 45 if app_type == "web" else 60
    values.update(probes_block(app_type, port, path))
    values["MaxSurge"] = 2
    values["MaxUnavailable"] = 0
    values["MinReadySeconds"] = 60

    values.update(
        resolve_placement(
            app_type,
            env,
            answers.get("pod_placement", {}).get("qa_prod_strategy", "min-domain"),
            app_name,
            release_name,
        )
    )

    autoscaling_cfg = answers.get("autoscaling", {})
    values.update(
        autoscaling_block(
            app_type,
            autoscaling_cfg.get("engine", "keda"),
            res.get("replicas", 1),
            per_env_value(autoscaling_cfg.get("max"), env, res.get("replicas", 1)),
            per_env_value(autoscaling_cfg.get("cpu_target"), env, 80),
            per_env_value(autoscaling_cfg.get("mem_target"), env, 80),
        )
    )

    values["containerSpec"] = lifecycle_common()
    values["containerSecurityContext"] = {"readOnlyRootFilesystem": True}
    values["containers"] = []

    if app_type == "web":
        values["ContainerPort"] = [
            {
                "envoyPort": port,
                "idleTimeout": "1800s",
                "name": "app",
                "port": port,
                "protocol": "TCP",
                "resizePolicy": [],
                "servicePort": 80,
                "supportStreaming": False,
                "useHTTP2": False,
            }
        ]
        values["EnvVariables"] = []
        values["pauseForSecondsBeforeSwitchActive"] = 30
        values["progressDeadlineSeconds"] = 180
        values["progressDeadlineAbort"] = True
        values["podDisruptionBudget"] = {"minAvailable": "1"}
        values["ingress"] = ingress_block(env, answers["ingress"], app_name)
    else:
        values["deploymentType"] = "ROLLING"
        values["deployment"] = {"strategy": {"rolling": {"maxSurge": 2, "maxUnavailable": 0}}}
        values["pauseForSecondsBeforeSwitchActive"] = 30
        values["podDisruptionBudget"] = {"minAvailable": "0"}
        values["ingress"] = {"enabled": False}

    values["rolloutLabels"] = common_labels(
        env, project_name, app_name, business_unit, brand, workload_type, criticality, language,
        include_service=(app_type == "web"),
    )
    values["image"] = {"pullPolicy": "IfNotPresent"}
    values["podAnnotations"] = {"downscaler/exclude": "true"}
    values["podLabels"] = common_labels(
        env, project_name, app_name, business_unit, brand, workload_type, criticality, language,
        include_service=True,
    )
    values["replicaCount"] = res.get("replicas", 1)
    values["resources"] = {
        "limits": {"cpu": res["cpu"], "memory": res["memory"]},
        "requests": {"cpu": res["cpu"], "memory": res["memory"]},
    }
    values["volumes"] = volumes
    values["volumeMounts"] = volume_mounts
    values["restartPolicy"] = "Always"
    values["service"] = {"type": "ClusterIP"}
    if app_type == "web":
        values["service"]["annotations"] = {}
        values["service"]["loadBalancerSourceRanges"] = []

    sa_create = answers.get("service_account", {}).get("create", True)
    values["serviceAccount"] = {
        "annotations": {"eks.amazonaws.com/role-arn": irsa_role_arn(env, base_service_name)},
        "create": sa_create,
        "name": sa_name(app_name, answers.get("service_account", {})),
    }

    if app_type == "consumer":
        values["server"] = {"deployment": {"image": "", "image_tag": ""}}

    # args/command are NOT tied to app_type — most apps (consumer or web) run
    # the Docker image's own ENTRYPOINT/CMD unmodified and omit these entirely
    # (e.g. admin-gateway-go-prod has neither, despite being a web app).
    # Only emit them if this app's Dockerfile/entrypoint actually needs an
    # override (e.g. merchant-gateway-go wraps its binary and needs
    # args: ["/application/run"] + command: ["/bin/sh", "-c"]).
    entrypoint = answers.get("container_entrypoint") or {}
    if "args" in entrypoint:
        values["args"] = {"enabled": True, "value": entrypoint["args"]}
    if "command" in entrypoint:
        values["command"] = {
            "enabled": True,
            "value": entrypoint["command"],
            "workingDir": entrypoint.get("workingDir", {}),
        }

    return values


def eso_secret_block(env, app_name, base_service_name, service_account_ref=None):
    aws = {
        "service": "SecretsManager",
        "region": REGION,
    }
    if service_account_ref:
        aws["auth"] = {"jwt": {"serviceAccountRef": {"name": service_account_ref}}}
    else:
        aws["role"] = irsa_role_arn(env, base_service_name)

    block = {
        "name": devtron_secret_name(app_name),
        "type": "environment",
        "external": True,
        "externalType": "ESO_AWSSecretsManager",
        "esoSecretData": {
            "secretStore": {"aws": aws},
            "refreshInterval": "2m",
            "esoDataFrom": [{"extract": {"key": secret_manager_key(env, base_service_name)}}],
        },
    }
    if service_account_ref:
        block["mergeStrategy"] = "replace"
    return block


def build_configurations_block(registry, repository_name, git_cfg, build_cfg):
    block = {
        "container_registry_name": registry,
        "repository_name": repository_name,
        "build_type": "DockerfileExists",
        "dockerfile_path": git_cfg["dockerfile_path"],
    }
    # Optional Docker build args (docker build --build-arg KEY=value per
    # entry) — only emitted if the answers file sets build.args.
    if build_cfg.get("args"):
        block["args"] = build_cfg["args"]
    return block


def build_config_yaml(answers, envs, primary_env):
    app_name = answers["app_name"]
    team_code = answers["team_code"]
    project_name = answers["project_name"]
    base_service_name = answers["base_service_name"]
    git_cfg = answers["git"]
    build_cfg = answers["build"]
    sa = sa_name(app_name, answers.get("service_account", {}))

    is_prod_bundle = envs == ["prod"]
    registry = build_cfg.get("repository_registry_override") or DEFAULT_REGISTRY[primary_env]
    # Observed convention: non-prod bundles use a personal Bitbucket credential,
    # the prod bundle uses the shared org one. Prefer an explicit per-bundle
    # override, then a single blanket account_name, then the convention default.
    if is_prod_bundle:
        git_account = git_cfg.get("account_name_prod") or git_cfg.get("account_name") or DEFAULT_GIT_ACCOUNT[primary_env]
    else:
        git_account = git_cfg.get("account_name_nonprod") or git_cfg.get("account_name") or DEFAULT_GIT_ACCOUNT[primary_env]

    # The ECR repository name is usually shared across bundles, but prod lives
    # in a separate AWS account/registry and occasionally uses a differently
    # named repo (seen on calculate-rules-consumer-go: dev repo
    # "ring-calculate-eligibility-rules-consumer" vs prod repo
    # "calculate-rules-consumer") — prefer an explicit prod override if given.
    if is_prod_bundle:
        repository_name = build_cfg.get("repository_name_prod") or build_cfg["repository_name"]
    else:
        repository_name = build_cfg["repository_name"]

    cfg = {
        "app_name": app_name,
        "project_name": project_name,
        "git_repositories": [
            {
                "url": git_cfg["url"],
                "git_account_name": git_account,
                "checkout_path": "./",
                "fetch_submodules": False,
            }
        ],
        "build_configurations": build_configurations_block(registry, repository_name, git_cfg, build_cfg),
        "base_configurations": {
            "deployment_template": {
                "version": CHART_VERSION,
                "chart_type": CHART_TYPE,
                "show_application_metrics": False,
                "values_path": f"base-{app_name}-values.yaml",
            },
            "secrets": [eso_secret_block(primary_env, app_name, base_service_name)],
        },
        "workflows": [],
    }

    for env in envs:
        ename = env_name(env, team_code)
        branch = git_cfg["branches"][env]
        cfg["workflows"].append(
            {
                "ci_pipeline": {
                    "type": "CI_BUILD",
                    "is_manual": False,
                    "name": f"ci-{ename}",
                    "branches": [
                        {
                            "repo": Path(git_cfg["url"]).name,
                            "branch": branch,
                            "type": "SOURCE_TYPE_BRANCH_FIXED",
                        }
                    ],
                },
                "cd_pipelines": [
                    {
                        "name": f"cd-{ename}",
                        "environment_name": ename,
                        "is_manual": False,
                        "deployment_type": "argo_cd",
                        "deployment_strategies": [
                            {
                                "name": "ROLLING",
                                "strategy": {"maxSurge": "25%", "maxUnavailable": 1},
                                "default": True,
                            }
                        ],
                        "env_configuration": {
                            "deployment_template": {
                                "type": "override",
                                "version": CHART_VERSION,
                                "merge_strategy": "replace",
                                "show_application_metrics": False,
                                "values_path": f"override-{app_name}-{ename}-values.yaml",
                            },
                            "secrets": [eso_secret_block(env, app_name, base_service_name, service_account_ref=sa)],
                        },
                    }
                ],
            }
        )

    return cfg


# ------------------------------------------------------------------------------
# Driver
# ------------------------------------------------------------------------------

def write_yaml(path: Path, data, force: bool):
    if path.exists() and not force:
        print(f"SKIP (exists, use --force to overwrite): {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    print(f"wrote {path}")


def validate(answers):
    required_top = [
        "app_name",
        "app_type",
        "project_name",
        "team_code",
        "base_service_name",
        "workload_type",
        "criticality",
        "git",
        "build",
        "resources",
    ]
    for key in required_top:
        if key not in answers:
            raise ValueError(f"answers file is missing required key: {key}")
    if answers["app_type"] not in ("consumer", "web"):
        raise ValueError("app_type must be 'consumer' or 'web'")
    if answers["app_type"] == "web" and "ingress" not in answers:
        raise ValueError("app_type is 'web' but no 'ingress' section was provided")
    qa_prod_strategy = answers.get("pod_placement", {}).get("qa_prod_strategy", "min-domain")
    if qa_prod_strategy not in VALID_QA_PROD_STRATEGIES:
        raise ValueError(
            f"pod_placement.qa_prod_strategy must be one of {VALID_QA_PROD_STRATEGIES}, got {qa_prod_strategy!r}"
        )
    envs_enabled = answers.get("envs_enabled", {"dev": True, "qa": True, "prod": True})
    for env in ("dev", "qa", "prod"):
        if envs_enabled.get(env):
            if env not in answers["git"].get("branches", {}):
                raise ValueError(f"git.branches.{env} is required (envs_enabled.{env} is true)")
            if env not in answers["resources"]:
                raise ValueError(f"resources.{env} is required (envs_enabled.{env} is true)")
    return envs_enabled


def generate_one(answers_path, out_dir, force):
    """Generate the non-prod/prod bundles for a single answers file. Returns
    the app_name on success (raises on any validation/render error)."""
    with open(answers_path) as f:
        answers = yaml.safe_load(f)

    envs_enabled = validate(answers)
    app_name = answers["app_name"]
    team_code = answers["team_code"]

    nonprod_envs = [e for e in ("dev", "qa") if envs_enabled.get(e)]
    prod_envs = ["prod"] if envs_enabled.get("prod") else []

    # One folder per service, with the two Devtron config bundles nested
    # underneath — {app_name}/non-prod/ and {app_name}/prod/ — rather than
    # two sibling folders ({app_name}/ and {app_name}-prod/).
    service_root = out_dir / app_name
    nonprod_dir = service_root / "non-prod"
    prod_dir = service_root / "prod"

    if nonprod_envs:
        cfg = build_config_yaml(answers, nonprod_envs, primary_env="dev")
        write_yaml(nonprod_dir / "config.yaml", cfg, force)
        write_yaml(nonprod_dir / f"base-{app_name}-values.yaml", build_values(nonprod_envs[0], answers, is_base=True), force)
        for env in nonprod_envs:
            ename = env_name(env, team_code)
            write_yaml(
                nonprod_dir / f"override-{app_name}-{ename}-values.yaml",
                build_values(env, answers, is_base=False),
                force,
            )

    if prod_envs:
        cfg = build_config_yaml(answers, prod_envs, primary_env="prod")
        write_yaml(prod_dir / "config.yaml", cfg, force)
        write_yaml(prod_dir / f"base-{app_name}-values.yaml", build_values("prod", answers, is_base=True), force)
        ename = env_name("prod", team_code)
        write_yaml(
            prod_dir / f"override-{app_name}-{ename}-values.yaml",
            build_values("prod", answers, is_base=False),
            force,
        )

    print("Review the diffs, then run e.g.:")
    if nonprod_envs:
        print(f"  export DEVTRON_URL=... DEVTRON_API_TOKEN=...   # non-prod devtron")
        print(f"  tron --config {app_name}/non-prod/config.yaml create-app")
    if prod_envs:
        print(f"  export DEVTRON_URL=... DEVTRON_API_TOKEN=...   # prod devtron")
        print(f"  tron --config {app_name}/prod/config.yaml create-app")
    if answers.get("pod_placement", {}).get("qa_prod_strategy") == "min-domain":
        print(
            "NOTE: min-domain placement was requested — after create-app, fetch the "
            "appId/envId from the Devtron UI and replace the '<FILL_AFTER_CREATE>' "
            "placeholders in the qa/prod override values files, then run update-app."
        )

    return app_name


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--answers",
        required=True,
        nargs="+",
        help="Path(s) to one or more answers YAML files — pass multiple to generate several apps in one run",
    )
    parser.add_argument("--out-dir", default=".", help="Directory to write app folders into (default: cwd)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    succeeded = []
    failed = []

    for answers_path in args.answers:
        print(f"\n=== {answers_path} ===")
        try:
            app_name = generate_one(Path(answers_path), out_dir, args.force)
            succeeded.append((answers_path, app_name))
        except Exception as exc:
            print(f"ERROR: {answers_path}: {exc}", file=sys.stderr)
            failed.append((answers_path, exc))

    print(f"\nDone. This script only wrote files — nothing was sent to Devtron.")
    print(f"{len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed files:", file=sys.stderr)
        for answers_path, exc in failed:
            print(f"  - {answers_path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
