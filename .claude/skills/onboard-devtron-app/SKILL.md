---
name: onboard-devtron-app
description: Use when onboarding/migrating a new service into Devtron via devtron-cli (tron) in this repo — e.g. "onboard X to devtron", "migrate X from ECS to EKS", "create a devtron app for X". Asks a short standard question set, applies this repo's established naming/placement conventions (namespaces, ECR registries, IRSA roles, secrets, pod placement strategy), and generates config.yaml + base/override values files for non-prod (dev+qa) and prod, matching the layout of every already-migrated app (admin-gateway-go, admin-service-go-consumer, calculate-rules-consumer-go, go-loan-service-kafka-consumer-dlq, merchant-gateway-go).
---

# Onboard a new app to Devtron

## What gets produced

A single folder per service, `{app_name}/`, with the two devtron-cli config
bundles nested underneath it — because dev+qa share one AWS account
(451914973813) and one Devtron instance, while prod is a fully separate AWS
account (183716846541) and (per this team's existing setup) a separate
Devtron instance/token:

```
{app_name}/
  non-prod/   # one config.yaml with TWO cd_pipelines (dev-{team}, qa-{team}),
              # plus base-*.yaml and one override-*.yaml per env
  prod/       # one config.yaml with ONE cd_pipeline (prod-{team}),
              # plus its own base-*.yaml and override-*.yaml
```

The heavy lifting is done by `scripts/generate_devtron_app.py`, which bakes
in every fixed convention (chart version/type, labels, lifecycle hooks,
volumes, security context, secret/role naming formulas, deployment
strategy). You only need to gather the variable inputs below, write them to
an answers YAML file, and run the script.

## Step 1 — Ask these questions

Don't ask about anything covered in "Step 2 — fixed conventions" below. Batch
related questions together instead of asking one at a time.

**Identity**
- Service/app name (kebab-case) → `app_name`.
- Devtron project name, exactly as it appears in Devtron (e.g.
  `platform-engineering`, `lms`, `merchant-sales`) → `project_name`. This is
  never hardcoded or guessed by the generator — always ask and put it in the
  answers file verbatim.
- Short team code used only to build the namespace suffix (`dev-{code}`,
  `qa-{code}`, `prod-{code}`) → `team_code`. Existing examples use `pe`, `lms`,
  `ms`, but this is just a free-form short code, not a fixed enum — ask what
  this team's convention is if unsure.
- App type — there are only **two** valid values:
  - `web` — has an ALB ingress (gateway/API-style service).
  - `consumer` — kafka consumer, DLQ consumer, cron/worker — no ingress.
  This changes pod placement, probes, and several other fields — get it
  right first.

**IRSA role / secret ownership** — ask, never guess:
- What's the underlying **base service name** this app belongs to? Include
  the `-go` suffix (e.g. `admin-service-go`, `merchant-gateway-go`) — this is
  what the Secrets Manager key is keyed on directly, and it's often NOT
  identical to `app_name` — e.g. `admin-service-go-consumer`'s base is
  `admin-service-go`, because it shares a role/secret with the
  admin-service-go gateway. → `base_service_name`.
- Does a sibling app for this `base_service_name` **already exist** in
  Devtron (so an IRSA role + k8s ServiceAccount are already provisioned)?
  - Yes → ask for the exact existing ServiceAccount name → `service_account:
    {create: false, name: <existing>}`. The IRSA role ARN is re-derived from
    the standard pattern (`onemi-app-{env}-{base_service_name_without_-go}-irsa-role`
    — the `-go` is dropped for the role name specifically, see Step 2);
    confirm that ARN with the user if there's any doubt it was named
    differently.
  - No → `service_account: {create: true}` (name defaults to `{app_name}-sa`).

**Git / build**
- Git repo URL, Dockerfile path.
- Branch name **per environment** — there is no fixed convention here;
  existing apps use `development`/`qa`/`main`, others `development`/`qa`/`master`.
  Always ask for all three explicitly.
- ECR `repository_name` — usually one name shared across dev/qa/prod, but
  confirm: prod lives in a separate AWS account/registry and at least one
  existing service (`calculate-rules-consumer-go`) uses a *different* repo
  name in prod than in dev/qa. Ask "is the prod ECR repo name the same?" and
  set `build.repository_name_prod` if not.
- Git credential: defaults to `Bitbucket-Sulaim2` for the non-prod bundle
  and `Bitbucket` for the prod bundle (observed convention). Only ask if the
  user hints this service needs something different.

**Sizing**
- CPU/memory request+limit and replica count per environment. You may
  suggest the lightweight defaults seen elsewhere (60m/120Mi for small
  consumers) as a starting point, but don't assume — ask.
- Autoscaling target (cpu/mem %) and min/max replicas if elastic.
  - Consumers always use KEDA with `minReplicaCount == maxReplicaCount`
    (fixed count, not elastic) — just confirm the replica count, no need to
    ask "keda or hpa" for consumers.
  - `web` apps can use either plain Kubernetes HPA (`autoscaling`, seen on
    admin-gateway-go-prod) or KEDA (`kedaAutoscaling`, seen on
    merchant-gateway-go). Ask, or better: check whether this app has
    siblings already onboarded and match their engine.

**Ingress (`web` app type only)**
- Scheme: `internal` vs `internet-facing`.
- Hostname per environment.
- ACM certificate ARN per environment (these are per-AWS-account — dev/qa
  share one cert pool in account 451914973813, prod has its own in
  183716846541; look at another gateway's override file in this repo for a
  candidate, but always confirm the exact ARN with the user rather than
  reusing someone else's certificate).
- ALB `group.name`: dev/qa conventionally share one bucket per env
  (`onemi-app-{env}-internet` or `-internal`); prod conventionally gets its
  own per-app group (`onemi-app-prod-{app}-internet`). Confirm.

**Container entrypoint override (optional, either app type)**
- Does this app's Docker image need its `ENTRYPOINT`/`CMD` overridden? Most
  apps don't — e.g. `admin-gateway-go-prod` has no `args`/`command` at all,
  despite being a `web` app. Only `merchant-gateway-go` needs it, because it
  wraps its binary (`args: ["/application/run"]`,
  `command: ["/bin/sh", "-c"]`). Don't add `container_entrypoint` to the
  answers file unless the user confirms their image actually needs this —
  it's not tied to app type.

**Pod placement strategy** — apply this table, don't re-derive it each time:

| app_type   | dev        | qa                                    | prod       |
|------------|------------|----------------------------------------|------------|
| `consumer` | full-spot  | full-spot                              | full-spot  |
| `web`      | full-spot  | *(A)* — same choice as prod             | *(A)*      |

*(A)* — ask the user **once**, and apply the same answer to both qa and
prod (`pod_placement.qa_prod_strategy`), because qa exists specifically to
validate prod-like scheduling behavior. Three valid values:
- **"full-spot"** → same as dev: `nodeAffinity` + toleration onto spot only.
  Reasonable for a low-priority/internal web app that doesn't need HA-style
  scheduling even in qa/prod.
- **"fifty-fifty"** → `podAntiAffinity` spreading pods by hostname only (no
  explicit spot/on-demand split enforced).
- **"min-domain"** → `podAntiAffinity` by hostname **plus** a
  `topologySpreadConstraint` requiring at least 2 `karpenter.sh/capacity-type`
  domains (i.e. guarantees a spot+on-demand split). This block needs
  Devtron's internal `appId`/`envId`, which only exist **after** the app and
  environment are created in the Devtron UI. The generator fills in
  `<FILL_AFTER_CREATE>` placeholders — tell the user they must patch those in
  post-creation and re-run `tron ... update-app` (see Step 4).

Definitions, for your own reference:
- **full-spot** = `nodeAffinity` requiring `karpenter.sh/capacity-type In
  [spot]` + a matching toleration.

## Step 2 — Fixed conventions (apply automatically, do not ask)

These are baked into `scripts/generate_devtron_app.py` already — you only
need to know them well enough to sanity-check the output or explain it:

- Chart: version `5.2.0`, `chart_type: "Rollout Deployment"`.
- AWS accounts: dev+qa → `451914973813` (registry `ring-dev`); prod →
  `183716846541` (registry `onemi-prod-ecr`).
- Namespace/environment name: `{env}-{team_code}` (`dev-pe`, `qa-lms`, `prod-ms`, …).
  `project_name` itself (the Devtron project) is never derived from
  `team_code` — it's a required, explicit field in the answers file.
- Devtron secret resource name: `aws-asm-{app_name}`.
- Secrets Manager key: `onemi/{env}/{base_service_name}/app-credentials` —
  `base_service_name` is used verbatim here (it already includes `-go`).
  (A few early-migrated apps have a legacy ad-hoc key like
  `devtron-eks-*-dev` — that's deprecated drift, never reproduce it.)
- IRSA role ARN: `arn:aws:iam::{account_id}:role/onemi-app-{env}-{base_service_name_without_-go}-irsa-role`
  — this is the one place the `-go` suffix gets dropped
  (`onemi-app-dev-admin-service-irsa-role`, never `...-admin-service-go-irsa-role`).
- The **base** `config.yaml` secret block (`base_configurations.secrets`)
  authenticates with a static `secretStore.aws.role` (the IRSA role ARN for
  that bundle's primary env — dev for non-prod, prod for the prod bundle).
  The **per-env override** secret block (inside each `cd_pipelines[].env_configuration.secrets`)
  instead authenticates via `secretStore.aws.auth.jwt.serviceAccountRef.name`
  pointing at the app's k8s ServiceAccount. Don't collapse these two into one
  shape — they're deliberately different.
- Standard labels (`podLabels`/`rolloutLabels`): `env: eks-{env}`,
  `app-group: light-apps`, `Brand: Ring`, `Business-Units: onemi`,
  `Language: GO`, `Team: {project_name}`. (A few existing files drifted to
  `on-emi` / `si-creva` for Business-Units — that's copy/paste noise, not a
  second standard; always emit `onemi` unless the user explicitly says
  otherwise.)
- Common infra bits on every app: `containerSecurityContext.readOnlyRootFilesystem: true`,
  a `tmp-dir` emptyDir volume mounted at `/tmp`, a `preStop: sleep 15`
  lifecycle hook, `podAnnotations: {downscaler/exclude: "true"}`,
  `restartPolicy: Always`, `image.pullPolicy: IfNotPresent`,
  `service.type: ClusterIP`, `progressDeadlineSeconds: 180` (`web` apps).
- File naming: always `config.yaml` (never the `congif.yaml` typo that
  crept into a few of the earlier prod folders).

## Step 3 — Generate

Write the gathered answers into a YAML file following the schema in
`scripts/examples/answers-consumer.example.yaml` or
`scripts/examples/answers-web.example.yaml` (copy whichever matches the
app type, then fill it in — don't invent new top-level keys). Then run:

```bash
python3 scripts/generate_devtron_app.py --answers /path/to/answers.yaml
```

This only writes files locally (`{app_name}/non-prod/` and `{app_name}/prod/`)
— it never calls the Devtron API. Show the user the generated files/diff
before suggesting anything be applied. Re-run with `--force` if regenerating
over existing files.

## Step 4 — Apply (the user runs this, don't run it yourself)

```bash
export DEVTRON_URL=...          # non-prod devtron instance
export DEVTRON_API_TOKEN=...
tron --config {app_name}/non-prod/config.yaml create-app

export DEVTRON_URL=...          # prod devtron instance
export DEVTRON_API_TOKEN=...
tron --config {app_name}/prod/config.yaml create-app
```

`create-app`/`update-app` provision real infrastructure (git material, CI/CD
pipelines, secrets, environments) — this is not something to run
automatically on the user's behalf without them explicitly asking for it in
this step.

If `min-domain` placement was chosen: after `create-app`, the user must open
the Devtron UI, note the `appId` and each environment's `envId`, replace the
`<FILL_AFTER_CREATE>` placeholders in the qa/prod override values files, and
re-run `tron --config ... update-app`.

## Known inconsistencies in this repo (flag them, don't silently replicate)

- `Business-Units` label drifts between `onemi` / `on-emi` / `si-creva`
  across existing files — treat as unintentional drift, default to `onemi`.
- Some prod folders are misspelled `congif.yaml` — always generate
  `config.yaml`.
- One dev secret key (`admin-service-go-kafka-consumer`) uses a legacy
  ad-hoc SecretsManager path instead of the `onemi/{env}/{base}/app-credentials`
  convention — treat it as deprecated, never copy that shape for new apps.
