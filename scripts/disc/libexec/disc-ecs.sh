#!/bin/bash
# ==============================================================================
# Extracts just the info needed to fill config.yaml for a Devtron app,
# by reading it off the existing ECS service / CodePipeline / CodeBuild.
#
# Usage:
#   disc ecs <cluster-name> <service-name> [region]
#
# Example:
#   disc ecs qa1-ecs-cluster qa1-location-service ap-south-1
#
# Prints plain text to stdout. No output file.
# ==============================================================================

set -euo pipefail

CLUSTER=${1:-}
SERVICE=${2:-}
REGION=${3:-ap-south-1}

if [[ -z "${CLUSTER:-}" || -z "${SERVICE:-}" ]]; then
    echo "Usage: disc ecs <cluster-name> <service-name> [region]"
    exit 1
fi

echo "app_name: $SERVICE"
echo "cluster: $CLUSTER"
echo "region: $REGION"
echo ""

SVC_JSON=$(aws ecs describe-services \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --region "$REGION" \
    --output json)

TD_ARN=$(echo "$SVC_JSON" | jq -r '.services[0].taskDefinition')

TD_JSON=$(aws ecs describe-task-definition \
    --task-definition "$TD_ARN" \
    --region "$REGION" \
    --output json)

TASK_ROLE=$(echo "$TD_JSON" | jq -r '.taskDefinition.taskRoleArn // "None"')
EXEC_ROLE=$(echo "$TD_JSON" | jq -r '.taskDefinition.executionRoleArn // "None"')

echo "task_role_arn (for IRSA): $TASK_ROLE"
echo "execution_role_arn: $EXEC_ROLE"
echo ""

CONTAINERS=$(echo "$TD_JSON" | jq -c '.taskDefinition.containerDefinitions[]?')

echo "$CONTAINERS" | while IFS= read -r ROW; do

    C_NAME=$(echo "$ROW" | jq -r '.name')
    IMAGE=$(echo "$ROW" | jq -r '.image')
    IMAGE_WITHOUT_TAG=$(echo "$IMAGE" | cut -d ':' -f1)
    REPO_NAME=$(basename "$IMAGE_WITHOUT_TAG")

    echo "===== container: $C_NAME ====="
    echo "image: $IMAGE"
    echo "repository_name (ECR): $REPO_NAME"

    ECR_INFO=$(aws ecr describe-repositories \
        --repository-names "$REPO_NAME" \
        --region "$REGION" \
        --output json 2>/dev/null || echo "{}")

    ECR_URI=$(echo "$ECR_INFO" | jq -r '.repositories[0].repositoryUri // empty')
    REGISTRY_ACCOUNT=$(echo "$ECR_URI" | cut -d '.' -f1)

    if [[ -n "$ECR_URI" ]]; then
        echo "ecr_repository_uri: $ECR_URI"
        echo "registry_account_id (match this to a container_registry_name in Devtron): $REGISTRY_ACCOUNT"
    fi

    # secrets -> secretsmanager key path for esoDataFrom
    SECRETS=$(echo "$ROW" | jq -c '.secrets[]?')
    if [[ -n "$SECRETS" ]]; then
        echo "secrets:"
        echo "$SECRETS" | while IFS= read -r SEC; do
            S_NAME=$(echo "$SEC" | jq -r '.name')
            S_ARN=$(echo "$SEC" | jq -r '.valueFrom')
            echo "  $S_NAME -> $S_ARN"
        done
    fi

    # find matching codepipeline
    MATCHED_PIPELINES=$(aws codepipeline list-pipelines \
        --region "$REGION" \
        --output json | \
        jq -r '.pipelines[]?.name' | \
        grep -i "$REPO_NAME" || true)

    if [[ -z "$MATCHED_PIPELINES" ]]; then
        echo "no matching codepipeline found for repo $REPO_NAME"
        echo ""
        continue
    fi

    for PIPE in $MATCHED_PIPELINES; do

        echo "pipeline: $PIPE"

        PIPELINE_JSON=$(aws codepipeline get-pipeline \
            --name "$PIPE" \
            --region "$REGION" \
            --output json)

        ACTIONS=$(echo "$PIPELINE_JSON" | jq -c '.pipeline.stages[].actions[]')

        echo "$ACTIONS" | while IFS= read -r ACTION; do

            PROVIDER=$(echo "$ACTION" | jq -r '.actionTypeId.provider')
            CATEGORY=$(echo "$ACTION" | jq -r '.actionTypeId.category')

            if [[ "$CATEGORY" == "Source" ]]; then
                FULL_REPO=$(echo "$ACTION" | jq -r '.configuration.FullRepositoryId // empty')
                BRANCH=$(echo "$ACTION" | jq -r '.configuration.BranchName // empty')
                [[ -n "$FULL_REPO" ]] && echo "git_repository: $FULL_REPO"
                [[ -n "$BRANCH" ]] && echo "branch: $BRANCH"
            fi

            if [[ "$PROVIDER" == "CodeBuild" ]]; then
                PROJECT_NAME=$(echo "$ACTION" | jq -r '.configuration.ProjectName')
                echo "codebuild_project: $PROJECT_NAME"

                BUILD_JSON=$(aws codebuild batch-get-projects \
                    --names "$PROJECT_NAME" \
                    --region "$REGION" \
                    --output json)

                BUILDSPEC=$(echo "$BUILD_JSON" | jq -r '.projects[0].source.buildspec // empty')

                if [[ -z "$BUILDSPEC" ]]; then
                    echo "dockerfile_path: not found (project uses default buildspec.yml from repo root, check repo manually)"
                elif [[ "$BUILDSPEC" != version:* ]]; then
                    echo "dockerfile_path: buildspec is a file in the repo at '$BUILDSPEC', open it to find the Dockerfile path"
                else
                    DOCKER_LINES=$(echo "$BUILDSPEC" | grep -iE "docker build|dockerfile" || true)
                    if [[ -n "$DOCKER_LINES" ]]; then
                        echo "dockerfile_path (from buildspec, look for -f flag or Dockerfile name):"
                        echo "$DOCKER_LINES" | sed 's/^/  /'
                    else
                        echo "dockerfile_path: no docker build line found in inline buildspec"
                    fi
                fi
            fi

        done

    done

    echo ""

done
