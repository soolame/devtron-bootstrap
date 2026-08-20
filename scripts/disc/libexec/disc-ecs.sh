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
TASK_CPU=$(echo "$TD_JSON" | jq -r '.taskDefinition.cpu // "Dynamic (set per-container)"')
TASK_MEM=$(echo "$TD_JSON" | jq -r '.taskDefinition.memory // "Dynamic (set per-container)"')

echo "task_role_arn (for IRSA): $TASK_ROLE"
echo "execution_role_arn: $EXEC_ROLE"
echo "task_cpu: $TASK_CPU"
echo "task_memory: $TASK_MEM"
echo ""

ASG_TARGET=$(aws application-autoscaling describe-scalable-targets \
    --service-namespace ecs \
    --resource-id "service/$CLUSTER/$SERVICE" \
    --region "$REGION" \
    --output json 2>/dev/null || echo "{}")

MIN_CAP=$(echo "$ASG_TARGET" | jq -r '.ScalableTargets[0].MinCapacity // empty')
MAX_CAP=$(echo "$ASG_TARGET" | jq -r '.ScalableTargets[0].MaxCapacity // empty')

if [[ -n "$MIN_CAP" ]]; then
    echo "autoscaling_min_replicas: $MIN_CAP"
    echo "autoscaling_max_replicas: $MAX_CAP"

    SCALING_POLICIES=$(aws application-autoscaling describe-scaling-policies \
        --service-namespace ecs \
        --resource-id "service/$CLUSTER/$SERVICE" \
        --region "$REGION" \
        --output json 2>/dev/null || echo "{}")

    POLICIES=$(echo "$SCALING_POLICIES" | jq -c '.ScalingPolicies[]?')

    if [[ -z "$POLICIES" ]]; then
        echo "scaling_policy: none"
    else
        echo "$POLICIES" | while IFS= read -r POLICY; do

            POLICY_NAME=$(echo "$POLICY" | jq -r '.PolicyName')
            POLICY_TYPE=$(echo "$POLICY" | jq -r '.PolicyType')

            echo "scaling_policy: $POLICY_NAME ($POLICY_TYPE)"

            if [[ "$POLICY_TYPE" == "TargetTrackingScaling" ]]; then
                METRIC=$(echo "$POLICY" | jq -r '.TargetTrackingScalingPolicyConfiguration.PredefinedMetricSpecification.PredefinedMetricType // "CustomizedMetric"')
                TARGET=$(echo "$POLICY" | jq -r '.TargetTrackingScalingPolicyConfiguration.TargetValue // "N/A"')
                SCALE_IN_CD=$(echo "$POLICY" | jq -r '.TargetTrackingScalingPolicyConfiguration.ScaleInCooldown // empty')
                SCALE_OUT_CD=$(echo "$POLICY" | jq -r '.TargetTrackingScalingPolicyConfiguration.ScaleOutCooldown // empty')

                echo "  metric: $METRIC"
                echo "  target_value: $TARGET"
                echo "  scale_in_cooldown: ${SCALE_IN_CD:-N/A}${SCALE_IN_CD:+s}"
                echo "  scale_out_cooldown: ${SCALE_OUT_CD:-N/A}${SCALE_OUT_CD:+s}"

            elif [[ "$POLICY_TYPE" == "StepScaling" ]]; then
                ADJ_TYPE=$(echo "$POLICY" | jq -r '.StepScalingPolicyConfiguration.AdjustmentType // "N/A"')
                COOLDOWN=$(echo "$POLICY" | jq -r '.StepScalingPolicyConfiguration.Cooldown // empty')
                STEPS=$(echo "$POLICY" | jq -r '.StepScalingPolicyConfiguration.StepAdjustments[]? | "  step: scaling_adjustment=\(.ScalingAdjustment) lower_bound=\(.MetricIntervalLowerBound // "-inf") upper_bound=\(.MetricIntervalUpperBound // "+inf")"')

                echo "  adjustment_type: $ADJ_TYPE"
                echo "  cooldown: ${COOLDOWN:-N/A}${COOLDOWN:+s}"
                [[ -n "$STEPS" ]] && echo "$STEPS"
            fi

        done
    fi
else
    echo "autoscaling: not configured"
fi
echo ""

CP_STRATEGY=$(echo "$SVC_JSON" | jq -c '.services[0].capacityProviderStrategy[]?')
LAUNCH_TYPE=$(echo "$SVC_JSON" | jq -r '.services[0].launchType // empty')

if [[ -n "$CP_STRATEGY" ]]; then

    echo "$CP_STRATEGY" | while IFS= read -r CP; do

        CP_NAME=$(echo "$CP" | jq -r '.capacityProvider')
        CP_WEIGHT=$(echo "$CP" | jq -r '.weight // 0')
        CP_BASE=$(echo "$CP" | jq -r '.base // 0')

        echo "capacity_provider: $CP_NAME (weight=$CP_WEIGHT, base=$CP_BASE)"

        case "$CP_NAME" in
            FARGATE_SPOT)
                echo "capacity_type: SPOT (Fargate)"
                ;;
            FARGATE)
                echo "capacity_type: ON_DEMAND (Fargate)"
                ;;
            *)
                # custom EC2 capacity provider -> resolve the backing ASG
                CP_INFO=$(aws ecs describe-capacity-providers \
                    --capacity-providers "$CP_NAME" \
                    --region "$REGION" \
                    --output json 2>/dev/null || echo "{}")

                ASG_ARN=$(echo "$CP_INFO" | jq -r '.capacityProviders[0].autoScalingGroupProvider.autoScalingGroupArn // empty')

                if [[ -z "$ASG_ARN" ]]; then
                    echo "capacity_type: unknown (could not resolve ASG for capacity provider $CP_NAME)"
                    continue
                fi

                ASG_NAME=$(echo "$ASG_ARN" | sed -E 's#.*/([^/]+)$#\1#')

                ASG_INFO=$(aws autoscaling describe-auto-scaling-groups \
                    --auto-scaling-group-names "$ASG_NAME" \
                    --region "$REGION" \
                    --output json 2>/dev/null || echo "{}")

                OD_PCT=$(echo "$ASG_INFO" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.InstancesDistribution.OnDemandPercentageAboveBaseCapacity // empty')

                if [[ -n "$OD_PCT" ]]; then
                    if [[ "$OD_PCT" == "100" ]]; then
                        echo "capacity_type: ON_DEMAND (EC2 ASG $ASG_NAME)"
                    elif [[ "$OD_PCT" == "0" ]]; then
                        echo "capacity_type: SPOT (EC2 ASG $ASG_NAME)"
                    else
                        echo "capacity_type: MIXED - ${OD_PCT}% on-demand above base capacity (EC2 ASG $ASG_NAME)"
                    fi
                else
                    LT_ID=$(echo "$ASG_INFO" | jq -r '.AutoScalingGroups[0].LaunchTemplate.LaunchTemplateId // empty')
                    MARKET_TYPE="on-demand"

                    if [[ -n "$LT_ID" ]]; then
                        LT_VER=$(echo "$ASG_INFO" | jq -r '.AutoScalingGroups[0].LaunchTemplate.Version // "$Default"')
                        LT_DATA=$(aws ec2 describe-launch-template-versions \
                            --launch-template-id "$LT_ID" \
                            --versions "$LT_VER" \
                            --region "$REGION" \
                            --output json 2>/dev/null || echo "{}")
                        MARKET_TYPE=$(echo "$LT_DATA" | jq -r '.LaunchTemplateVersions[0].LaunchTemplateData.InstanceMarketOptions.MarketType // "on-demand"')
                    fi

                    if [[ "$MARKET_TYPE" == "spot" ]]; then
                        echo "capacity_type: SPOT (EC2 ASG $ASG_NAME)"
                    else
                        echo "capacity_type: ON_DEMAND (EC2 ASG $ASG_NAME)"
                    fi
                fi
                ;;
        esac

    done

elif [[ -n "$LAUNCH_TYPE" ]]; then
    echo "launch_type: $LAUNCH_TYPE"
    if [[ "$LAUNCH_TYPE" == "FARGATE" ]]; then
        echo "capacity_type: ON_DEMAND (Fargate, no capacity provider strategy)"
    fi
else
    echo "capacity_type: unknown"
fi
echo ""

LB_CONFIGS=$(echo "$SVC_JSON" | jq -c '.services[0].loadBalancers[]?')

if [[ -z "$LB_CONFIGS" ]]; then
    echo "load_balancer: none attached"
    echo ""
else
    echo "$LB_CONFIGS" | while IFS= read -r LB; do

        TG_ARN=$(echo "$LB" | jq -r '.targetGroupArn')
        LB_C_NAME=$(echo "$LB" | jq -r '.containerName')
        LB_C_PORT=$(echo "$LB" | jq -r '.containerPort')

        echo "load_balanced_container: $LB_C_NAME:$LB_C_PORT"
        echo "target_group_arn: $TG_ARN"

        TG_INFO=$(aws elbv2 describe-target-groups \
            --target-group-arns "$TG_ARN" \
            --region "$REGION" \
            --output json 2>/dev/null || echo "{}")

        HEALTH_PATH=$(echo "$TG_INFO" | jq -r '.TargetGroups[0].HealthCheckPath // "N/A"')
        TG_PROTOCOL=$(echo "$TG_INFO" | jq -r '.TargetGroups[0].Protocol // "N/A"')
        TG_PORT=$(echo "$TG_INFO" | jq -r '.TargetGroups[0].Port // "N/A"')

        echo "target_group_protocol_port: $TG_PROTOCOL:$TG_PORT"
        echo "health_check_path: $HEALTH_PATH"

        ALB_ARNS=$(echo "$TG_INFO" | jq -r '.TargetGroups[0].LoadBalancerArns[]?')

        for ALB_ARN in $ALB_ARNS; do

            ALB_INFO=$(aws elbv2 describe-load-balancers \
                --load-balancer-arns "$ALB_ARN" \
                --region "$REGION" \
                --output json 2>/dev/null || echo "{}")

            ALB_NAME=$(echo "$ALB_INFO" | jq -r '.LoadBalancers[0].LoadBalancerName')
            ALB_DNS=$(echo "$ALB_INFO" | jq -r '.LoadBalancers[0].DNSName')
            ALB_SCHEME=$(echo "$ALB_INFO" | jq -r '.LoadBalancers[0].Scheme')

            echo "load_balancer_name: $ALB_NAME"
            echo "load_balancer_scheme: $ALB_SCHEME"
            echo "load_balancer_dns: $ALB_DNS"

            LISTENERS=$(aws elbv2 describe-listeners \
                --load-balancer-arn "$ALB_ARN" \
                --region "$REGION" \
                --output json 2>/dev/null | jq -c '.Listeners[]?')

            echo "$LISTENERS" | while IFS= read -r LISTENER; do

                [[ -z "$LISTENER" ]] && continue

                LISTENER_ARN=$(echo "$LISTENER" | jq -r '.ListenerArn')
                LISTENER_PORT=$(echo "$LISTENER" | jq -r '.Port')
                LISTENER_PROTOCOL=$(echo "$LISTENER" | jq -r '.Protocol')

                RULES=$(aws elbv2 describe-rules \
                    --listener-arn "$LISTENER_ARN" \
                    --region "$REGION" \
                    --output json 2>/dev/null | \
                    jq -c ".Rules[]? | select(.Actions[]?.TargetGroupArn==\"$TG_ARN\")")

                [[ -z "$RULES" ]] && continue

                echo "listener: $LISTENER_PROTOCOL:$LISTENER_PORT"

                echo "$RULES" | while IFS= read -r RULE; do

                    PRIORITY=$(echo "$RULE" | jq -r '.Priority')

                    CONDITIONS=$(echo "$RULE" | jq -r '
                    [
                      .Conditions[]? |
                      if .Field == "host-header" then
                        "Host: " + (.HostHeaderConfig.Values | join(","))
                      elif .Field == "path-pattern" then
                        "Path: " + (.PathPatternConfig.Values | join(","))
                      elif .Field == "http-header" then
                        "Header: " + .HttpHeaderConfig.HttpHeaderName + "=" + (.HttpHeaderConfig.Values | join(","))
                      elif .Field == "request-method" then
                        "Method: " + (.HttpRequestMethodConfig.Values | join(","))
                      else
                        .Field
                      end
                    ] | join(" | ")
                    ')

                    echo "  rule_priority: $PRIORITY -> $CONDITIONS"

                done

            done

        done

        echo ""

    done
fi

CONTAINERS=$(echo "$TD_JSON" | jq -c '.taskDefinition.containerDefinitions[]?')

echo "$CONTAINERS" | while IFS= read -r ROW; do

    C_NAME=$(echo "$ROW" | jq -r '.name')
    IMAGE=$(echo "$ROW" | jq -r '.image')
    IMAGE_WITHOUT_TAG=$(echo "$IMAGE" | cut -d ':' -f1)
    REPO_NAME=$(basename "$IMAGE_WITHOUT_TAG")
    C_CPU=$(echo "$ROW" | jq -r '.cpu // "N/A"')
    C_MEM=$(echo "$ROW" | jq -r '.memoryReservation // .memory // "N/A"')
    ENTRYPOINT=$(echo "$ROW" | jq -r '.entryPoint // [] | join(" ")')
    COMMAND=$(echo "$ROW" | jq -r '.command // [] | join(" ")')

    echo "===== container: $C_NAME ====="
    echo "image: $IMAGE"
    echo "cpu: $C_CPU"
    echo "memory: $C_MEM"
    [[ -n "$ENTRYPOINT" ]] && echo "entrypoint: $ENTRYPOINT"
    if [[ -n "$COMMAND" ]]; then
        echo "command: $COMMAND"
    else
        echo "command: none (uses image default CMD)"
    fi
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
