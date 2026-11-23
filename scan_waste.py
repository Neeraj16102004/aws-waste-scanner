#!/usr/bin/env python3
"""
================================================================================
aws-waste-scanner (Free Community Edition)
https://github.com/yourusername/aws-waste-scanner

Quick, safe, strictly read-only scanner that detects:
1. Unattached EBS volumes (state == 'available')
2. Unassociated Elastic IPs (AssociationId missing)

Part of "The Production AWS Cost-Optimization Kit"
Full Edition: [YOUR_GUMROAD_LINK]
================================================================================
"""

import argparse
import datetime
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
        ProfileNotFound,
    )
except ImportError:
    print(
        "\n[ERROR] boto3 is not installed.\n"
        "Run: pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    sys.exit(1)

# ==============================================================================
# PRICING BENCHMARKS (US East Baseline)
# ==============================================================================
EBS_PRICING_PER_GB: Dict[str, float] = {
    "gp2": 0.10,       # $0.10/GB-month
    "gp3": 0.08,       # $0.08/GB-month
    "io1": 0.125,      # $0.125/GB-month
    "io2": 0.125,      # $0.125/GB-month
    "st1": 0.045,      # $0.045/GB-month
    "sc1": 0.015,      # $0.015/GB-month
    "standard": 0.050, # $0.050/GB-month (magnetic)
}
DEFAULT_EBS_RATE: float = 0.10

# Idle Elastic IP: $0.005/hr * 720 hrs = $3.60/month
IDLE_EIP_MONTHLY_COST: float = 0.005 * 720.0  # $3.60


# ==============================================================================
# TERMINAL FORMATTING
# ==============================================================================
class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def colorize(text: str, color_code: str, enable_color: bool = True) -> str:
    return f"{color_code}{text}{Colors.RESET}" if enable_color else text


def format_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def get_tag_value(tags: Optional[List[Dict[str, str]]], key_name: str) -> str:
    if not tags:
        return "-"
    target = key_name.strip().lower()
    for t in tags:
        if (t.get("Key") or t.get("key") or "").strip().lower() == target:
            return t.get("Value") or t.get("value") or "-"
    return "-"


# ==============================================================================
# SCAN LOGIC (STRICTLY READ-ONLY)
# ==============================================================================
def scan_unattached_ebs(ec2_client, region: str) -> List[Dict[str, Any]]:
    """Finds all EBS volumes with state == 'available'."""
    unattached: List[Dict[str, Any]] = []
    try:
        paginator = ec2_client.get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
            for vol in page.get("Volumes", []):
                vol_id = vol.get("VolumeId", "unknown")
                vol_type = vol.get("VolumeType", "gp2")
                size_gb = float(vol.get("Size", 0))
                rate = EBS_PRICING_PER_GB.get(vol_type, DEFAULT_EBS_RATE)
                monthly_cost = size_gb * rate
                name_tag = get_tag_value(vol.get("Tags"), "Name")

                unattached.append({
                    "region": region,
                    "volume_id": vol_id,
                    "volume_type": vol_type,
                    "size_gb": size_gb,
                    "name": name_tag,
                    "monthly_cost": monthly_cost,
                })
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "ClientError")
        print(f"  [{region}] {Colors.YELLOW}Warning (EBS describe_volumes): {error_code}{Colors.RESET}", file=sys.stderr)
    return unattached


def scan_unassociated_eips(ec2_client, region: str) -> List[Dict[str, Any]]:
    """Finds Elastic IPs where AssociationId is missing."""
    idle_eips: List[Dict[str, Any]] = []
    try:
        response = ec2_client.describe_addresses()
        for addr in response.get("Addresses", []):
            association_id = addr.get("AssociationId")
            instance_id = addr.get("InstanceId")

            if not association_id and not instance_id:
                public_ip = addr.get("PublicIp", "unknown")
                allocation_id = addr.get("AllocationId", "standard-ec2")
                name_tag = get_tag_value(addr.get("Tags"), "Name")

                idle_eips.append({
                    "region": region,
                    "allocation_id": allocation_id,
                    "public_ip": public_ip,
                    "name": name_tag,
                    "monthly_cost": IDLE_EIP_MONTHLY_COST,
                })
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "ClientError")
        print(f"  [{region}] {Colors.YELLOW}Warning (EIP describe_addresses): {error_code}{Colors.RESET}", file=sys.stderr)
    return idle_eips


# ==============================================================================
# AWS AUTHENTICATION & REGION RESOLUTION
# ==============================================================================
def get_session(profile_name: Optional[str] = None) -> boto3.Session:
    try:
        return boto3.Session(profile_name=profile_name) if profile_name else boto3.Session()
    except ProfileNotFound as e:
        print(f"{Colors.RED}[FATAL ERROR] AWS Profile '{profile_name}' not found: {e}{Colors.RESET}", file=sys.stderr)
        sys.exit(1)


def verify_caller(session: boto3.Session) -> Tuple[str, str]:
    try:
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        return ident.get("Account", "Unknown"), ident.get("Arn", "Unknown")
    except (NoCredentialsError, PartialCredentialsError) as e:
        print(
            f"\n{Colors.RED}[FATAL ERROR] AWS credentials not found or incomplete.\n"
            f"Run 'aws configure' or set AWS_ACCESS_KEY_ID & AWS_SECRET_ACCESS_KEY.\n"
            f"Details: {e}{Colors.RESET}\n",
            file=sys.stderr,
        )
        sys.exit(1)
    except ClientError as e:
        print(f"\n{Colors.RED}[FATAL ERROR] STS verification failed: {e}{Colors.RESET}\n", file=sys.stderr)
        sys.exit(1)


def get_regions(session: boto3.Session, override: Optional[str] = None) -> List[str]:
    if override:
        return [r.strip() for r in override.split(",") if r.strip()]
    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        resp = ec2.describe_regions(AllRegions=False)
        return sorted([r["RegionName"] for r in resp.get("Regions", [])])
    except ClientError:
        return [session.region_name or "us-east-1"]


# ==============================================================================
# REPORTING
# ==============================================================================
def render_table(title: str, headers: List[str], rows: List[List[str]], enable_color: bool = True) -> None:
    print(f"\n{colorize('━' * 88, Colors.CYAN, enable_color)}")
    print(f" {colorize(title.upper(), Colors.BOLD, enable_color)} ({len(rows)} detected)")
    print(colorize("━" * 88, Colors.CYAN, enable_color))

    if not rows:
        print(f" {colorize('✔ No waste detected in this category. Clean posture!', Colors.GREEN, enable_color)}")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    header_line = " │ ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(colorize(f" {header_line}", Colors.BOLD, enable_color))
    print("─" * (sum(col_widths) + 3 * (len(headers) - 1) + 2))

    for row in rows:
        line = " │ ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))
        print(f" {line}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="aws-waste-scanner (Free Edition): Rapid read-only audit for unattached EBS volumes & idle Elastic IPs."
    )
    parser.add_argument("--profile", type=str, default=None, help="AWS CLI profile name")
    parser.add_argument("--regions", type=str, default=None, help="Comma-separated regions (e.g. us-east-1,us-west-2)")
    parser.add_argument("--no-color", action="store_true", help="Disable colored terminal output")
    args = parser.parse_args()

    enable_color = not args.no_color and sys.stdout.isatty()

    session = get_session(args.profile)
    account_id, caller_arn = verify_caller(session)
    regions = get_regions(session, args.regions)

    print("\n" + "═" * 88)
    print(colorize("   AWS-WASTE-SCANNER // FREE COMMUNITY EDITION", Colors.BOLD + Colors.CYAN, enable_color))
    print("═" * 88)
    print(f" Account ID      : {colorize(account_id, Colors.BOLD, enable_color)}")
    print(f" Execution IAM   : {caller_arn}")
    print(f" Regions Scanned : {len(regions)} regions ({', '.join(regions[:5])}...)")
    print(f" Timestamp       : {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("─" * 88)

    all_ebs: List[Dict[str, Any]] = []
    all_eips: List[Dict[str, Any]] = []

    for idx, region in enumerate(regions, 1):
        sys.stdout.write(f"\r[{idx:02d}/{len(regions):02d}] Scanning region: {region.ljust(16)} ")
        sys.stdout.flush()
        try:
            ec2 = session.client("ec2", region_name=region)
            all_ebs.extend(scan_unattached_ebs(ec2, region))
            all_eips.extend(scan_unassociated_eips(ec2, region))
        except ClientError:
            pass

    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

    # Render EBS Table
    ebs_rows = [
        [
            r["region"],
            r["volume_id"],
            r["volume_type"],
            f"{r['size_gb']:.0f} GB",
            r["name"][:20],
            format_usd(r["monthly_cost"]),
        ]
        for r in all_ebs
    ]
    render_table(
        "1. Unattached EBS Volumes (state == 'available')",
        ["Region", "Volume ID", "Type", "Size", "Name Tag", "Est. Monthly Waste"],
        ebs_rows,
        enable_color,
    )

    # Render EIP Table
    eip_rows = [
        [
            r["region"],
            r["allocation_id"],
            r["public_ip"],
            r["name"][:20],
            format_usd(r["monthly_cost"]),
        ]
        for r in all_eips
    ]
    render_table(
        "2. Unassociated Elastic IPs ($3.60/month per idle IP)",
        ["Region", "Allocation ID", "Public IP", "Name Tag", "Est. Monthly Waste"],
        eip_rows,
        enable_color,
    )

    # Financial Totals
    total_ebs_waste = sum(r["monthly_cost"] for r in all_ebs)
    total_eip_waste = sum(r["monthly_cost"] for r in all_eips)
    total_waste = total_ebs_waste + total_eip_waste
    annualized_waste = total_waste * 12.0

    print("\n" + "═" * 88)
    print(colorize(" SCAN SUMMARY & RECOVERABLE SAVINGS", Colors.BOLD + Colors.GREEN, enable_color))
    print("═" * 88)
    print(f" Unattached EBS Volumes Found : {len(all_ebs):>4}  ({format_usd(total_ebs_waste)}/month)")
    print(f" Unassociated Elastic IPs     : {len(all_eips):>4}  ({format_usd(total_eip_waste)}/month)")
    print("─" * 88)
    print(f" {colorize('TOTAL IMMEDIATE MONTHLY WASTE', Colors.BOLD, enable_color)} : {colorize(format_usd(total_waste) + '/month', Colors.RED + Colors.BOLD, enable_color)}")
    print(f" {colorize('PROJECTED ANNUALIZED SAVINGS ', Colors.BOLD, enable_color)} : {colorize(format_usd(annualized_waste) + '/year', Colors.GREEN + Colors.BOLD, enable_color)}")
    print("═" * 88)

    # Subtle Lead-Magnet Upsell Tip
    print(f"\n{colorize('💡 FINOPS PRO-TIP:', Colors.BOLD + Colors.YELLOW, enable_color)}")
    print(
        "   This free tool only scans 2 surface-level waste vectors (Unattached EBS & Idle EIPs).\n"
        "   The full **Production AWS Cost-Optimization Kit** also audits:\n"
        "   • Active gp2 volumes eligible for zero-downtime gp3 upgrade (exact 20% savings)\n"
        "   • CloudWatch Log Groups set to 'Never Expire' hoarding unindexed bytes\n"
        "   • Running non-prod EC2 & RDS instances missing an Environment=prod tag\n"
        "   • Complete Terraform guardrails (AWS Budgets, Anomaly Detection & Off-Hours Auto-Stop)\n"
        "   • 25-Page Field Manual + 20-Point Monthly FinOps Checklist (CSV)\n"
    )
    print(f"   👉 Get the Full Kit: {colorize('[YOUR_GUMROAD_LINK]', Colors.CYAN + Colors.BOLD, enable_color)}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORTED] Scan interrupted by user.\n", file=sys.stderr)
        sys.exit(130)
