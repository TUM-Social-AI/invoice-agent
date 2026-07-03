"""
CDK stack for the invoice-agent scheduled worker.

Shape (see docs discussion): EventBridge fires a Fargate task on a cadence; the task
runs `python -m src.orchestration.worker` (one poll), reads/writes dedup state in
DynamoDB, pulls its API key + Drive credentials from Secrets Manager, logs to
CloudWatch, and exits. It runs in a minimal NAT-free VPC (public subnet, egress-only)
to avoid a fixed NAT cost, and scales to zero between polls.

Everything that varies is a CDK context value (see cdk.json / `-c key=value`):
  existingVpcId   import an existing VPC instead of creating one (else a minimal VPC is made)
  scheduleRate    EventBridge schedule expression        (default "rate(10 minutes)")
  llmProvider     LLM_PROVIDER env for the task           (default "gemini")
  llmKeyEnvName   env var the provider reads its key from (default "GOOGLE_API_KEY")
  imageTag        ECR image tag to run                    (default "latest")
  dedupTableName  DynamoDB table name                     (default "invoice-agent-dedup")
  cpu / memoryMib Fargate task size                       (default 2048 / 8192)
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class InvoiceAgentWorkerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        existing_vpc_id = ctx("existingVpcId")
        schedule_expr = ctx("scheduleRate") or "rate(10 minutes)"
        llm_provider = ctx("llmProvider") or "gemini"
        key_env_name = ctx("llmKeyEnvName") or "GOOGLE_API_KEY"
        image_tag = ctx("imageTag") or "latest"
        dedup_table_name = ctx("dedupTableName") or "invoice-agent-dedup"
        cpu = int(ctx("cpu") or 2048)
        memory_mib = int(ctx("memoryMib") or 8192)

        # --- Network -------------------------------------------------------------
        # Import an existing VPC if one is supplied, else create a minimal, NAT-free one.
        # Swapping between the two is just this branch + a `-c existingVpcId=...` on deploy.
        if existing_vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=existing_vpc_id)
        else:
            vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=0,  # no NAT: the worker only needs outbound, via public-subnet public IPs
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                    )
                ],
            )

        # Egress-only firewall: no inbound rules (nothing connects in), all outbound allowed
        # (to reach Google Drive + the LLM API, DynamoDB, Secrets Manager, ECR, CloudWatch).
        worker_sg = ec2.SecurityGroup(
            self,
            "WorkerSg",
            vpc=vpc,
            description="invoice-agent worker — egress only",
            allow_all_outbound=True,
        )

        # --- Dedup state ---------------------------------------------------------
        # Durable, concurrency-safe claim store. RETAIN so a stack teardown never wipes
        # which documents were already processed. Name matches config.yaml's default.
        dedup_table = dynamodb.Table(
            self,
            "DedupTable",
            table_name=dedup_table_name,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Secrets -------------------------------------------------------------
        # Created empty; populate the real values out-of-band (never in code):
        #   aws secretsmanager put-secret-value --secret-id invoice-agent/llm-api-key --secret-string '...'
        llm_secret = secretsmanager.Secret(
            self,
            "LlmApiKey",
            secret_name="invoice-agent/llm-api-key",
            description="LLM API key (Gemini or OpenAI) for the invoice worker",
        )
        drive_secret = secretsmanager.Secret(
            self,
            "DriveServiceAccount",
            secret_name="invoice-agent/drive-service-account",
            description="Google Drive service-account JSON",
        )

        # --- Image registry ------------------------------------------------------
        # Push the (3-5 GB) worker image here; the task pulls it on start. Kept out of
        # CDK asset bundling so `synth` needs no Docker and CI owns the build/push.
        repo = ecr.Repository(
            self,
            "Repo",
            repository_name="invoice-agent",
            image_scan_on_push=True,
        )
        repo.add_lifecycle_rule(max_image_count=10)  # cap storage cost of a heavy image

        # --- Logs ----------------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name="/invoice-agent/worker",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Compute: Fargate task -----------------------------------------------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, cluster_name="invoice-agent")

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            cpu=cpu,
            memory_limit_mib=memory_mib,
            ephemeral_storage_gib=30,  # scratch for downloaded PDFs + OCR work
        )

        task_def.add_container(
            "worker",
            image=ecs.ContainerImage.from_ecr_repository(repo, tag=image_tag),
            command=["python", "-m", "src.orchestration.worker"],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=log_group),
            environment={
                "LLM_PROVIDER": llm_provider,
                "CONFIG_PATH": "config/config.yaml",
                # Dedup table name + region come from config.yaml today (aws_region: null ->
                # the task's region). Keep config.yaml's dedupTableName aligned with this stack.
            },
            secrets={
                # Injected as env vars at task start via the execution role.
                key_env_name: ecs.Secret.from_secrets_manager(llm_secret),
                "GOOGLE_SERVICE_ACCOUNT_JSON": ecs.Secret.from_secrets_manager(drive_secret),
            },
        )

        # App (task role) may read/write the dedup table. The execution role's ECR pull,
        # secret read, and log write are granted automatically by the constructs above.
        dedup_table.grant_read_write_data(task_def.task_role)

        # --- Trigger: scheduled task ---------------------------------------------
        # assign_public_ip is required for a public-subnet Fargate task to egress without a NAT.
        events.Rule(
            self,
            "Schedule",
            schedule=events.Schedule.expression(schedule_expr),
            targets=[
                targets.EcsTask(
                    cluster=cluster,
                    task_definition=task_def,
                    task_count=1,
                    subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                    security_groups=[worker_sg],
                    assign_public_ip=True,
                )
            ],
        )

        # --- Outputs -------------------------------------------------------------
        CfnOutput(self, "EcrRepositoryUri", value=repo.repository_uri)
        CfnOutput(self, "DedupTableName", value=dedup_table.table_name)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "LlmSecretArn", value=llm_secret.secret_arn)
        CfnOutput(self, "DriveSecretArn", value=drive_secret.secret_arn)
