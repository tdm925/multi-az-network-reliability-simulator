#!/usr/bin/env python3
"""
simulate_failure.py — Multi-AZ Network Reliability Simulator

Breaks connectivity from the ALB to the ECS tasks by revoking the
ServiceSecurityGroup ingress rule, waits while you observe:

    ALB marks targets unhealthy
      -> CloudWatch Alarm fires
        -> SNS notifies you (email) and triggers the remediation Lambda
          -> Lambda forces an ECS service redeployment

...then restores the ingress rule automatically.

Requires: boto3, AWS CLI configured (aws configure), and the
nrs-network / nrs-compute / nrs-monitoring stacks already deployed.
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

DEFAULT_PROJECT = "nrs"
DEFAULT_PORT = 80
DEFAULT_OUTAGE_SECONDS = 90


def get_export_value(cfn_client, export_name: str) -> str:
    """Look up a CloudFormation stack export by name."""
    paginator = cfn_client.get_paginator("list_exports")
    for page in paginator.paginate():
        for export in page["Exports"]:
            if export["Name"] == export_name:
                return export["Value"]
    raise RuntimeError(
        f"Export '{export_name}' not found. "
        f"Check that the relevant stack is deployed and ProjectName matches."
    )


def find_ingress_rule(ec2_client, security_group_id: str, port: int):
    """Return the ingress rule dict on this SG that allows the given port, or None."""
    response = ec2_client.describe_security_groups(GroupIds=[security_group_id])
    sg = response["SecurityGroups"][0]
    for perm in sg["IpPermissions"]:
        if perm.get("FromPort") == port and perm.get("ToPort") == port:
            return perm
    return None


def revoke_rule(ec2_client, security_group_id: str, rule: dict):
    ec2_client.revoke_security_group_ingress(
        GroupId=security_group_id,
        IpPermissions=[rule],
    )


def authorize_rule(ec2_client, security_group_id: str, rule: dict):
    ec2_client.authorize_security_group_ingress(
        GroupId=security_group_id,
        IpPermissions=[rule],
    )


def print_target_health(elbv2_client, target_group_arn: str):
    resp = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn)
    for t in resp["TargetHealthDescriptions"]:
        target = t["Target"]["Id"]
        state = t["TargetHealth"]["State"]
        reason = t["TargetHealth"].get("Reason", "")
        print(f"    {target}: {state} {('(' + reason + ')') if reason else ''}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                         help="ProjectName used in the CFN stacks (default: nrs)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help="Container port to break (default: 80)")
    parser.add_argument("--outage-seconds", type=int, default=DEFAULT_OUTAGE_SECONDS,
                         help="How long to keep the outage before auto-restoring (default: 90)")
    parser.add_argument("--region", default=None,
                         help="AWS region override (default: from AWS CLI config)")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    ec2 = session.client("ec2")
    elbv2 = session.client("elbv2")

    print(f"== Multi-AZ Network Reliability Simulator — Failure Injection ==\n")

    print("Resolving infrastructure from CloudFormation exports...")
    sg_id = get_export_value(cfn, f"{args.project}-service-sg-id")
    tg_arn = get_export_value(cfn, f"{args.project}-tg-arn")
    print(f"  ServiceSecurityGroup: {sg_id}")
    print(f"  TargetGroup:          {tg_arn}\n")

    rule = find_ingress_rule(ec2, sg_id, args.port)
    if rule is None:
        print(f"ERROR: no ingress rule found on {sg_id} for port {args.port}. "
              f"Nothing to break.")
        sys.exit(1)

    print(f"Current target health (before injection):")
    print_target_health(elbv2, tg_arn)

    input("\nPress Enter to BREAK connectivity (revoke ingress rule)...")

    try:
        revoke_rule(ec2, sg_id, rule)
        print(f"\n[{time.strftime('%H:%M:%S')}] Ingress rule revoked. "
              f"ALB traffic to ECS tasks on port {args.port} is now blocked.\n")

        print(f"Watching for {args.outage_seconds}s — check:")
        print("  - AWS Console > EC2 > Target Groups > this TG > Targets tab")
        print("  - AWS Console > CloudWatch > Alarms > nrs-unhealthy-hosts")
        print("  - Your email for the SNS alert")
        print("  - CloudWatch Logs > /aws/lambda/nrs-remediation (Lambda invocation)\n")

        interval = 15
        elapsed = 0
        while elapsed < args.outage_seconds:
            time.sleep(interval)
            elapsed += interval
            print(f"[{elapsed}s] Target health:")
            print_target_health(elbv2, tg_arn)

    finally:
        print(f"\n[{time.strftime('%H:%M:%S')}] Restoring ingress rule...")
        try:
            authorize_rule(ec2, sg_id, rule)
            print("Ingress rule restored. Traffic should recover shortly.")
        except ClientError as e:
            if "InvalidPermission.Duplicate" in str(e):
                print("Rule already present (Lambda or manual action may have restored it).")
            else:
                raise

    print("\nDone. Give the ALB health checks ~30-60s to mark targets healthy again.")


if __name__ == "__main__":
    main()
