#!/usr/bin/env python3
"""CDK entrypoint for the invoice-agent worker stack."""

import os

import aws_cdk as cdk

from invoice_agent_infra.worker_stack import InvoiceAgentWorkerStack

app = cdk.App()

# Region defaults to Frankfurt (EU/GDPR, closest to the German dev team). Override with
# `-c region=...` or CDK_DEFAULT_REGION. Account is supplied by the CDK CLI at deploy time.
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION") or "eu-central-1"
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

InvoiceAgentWorkerStack(
    app,
    "InvoiceAgentWorker",
    env=cdk.Environment(account=account, region=region),
    description="Scheduled Fargate worker: polls Google Drive and runs the invoice compliance agent",
)

app.synth()
