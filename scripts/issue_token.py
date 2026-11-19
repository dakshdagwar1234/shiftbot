"""Why this exists:
CLI utility to generate signed JWT tokens for demonstration and manual testing.
Parses company, employee, and role arguments and outputs the signed token.
"""
import argparse
import os
import sys
from dotenv import load_dotenv

# Load local environment variables from .env file if present
load_dotenv()

# Ensure repository root is on Python path so app imports resolve
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.auth import issue_token


def main():
    parser = argparse.ArgumentParser(description="Issue JWT for shift-change agent testing.")
    parser.add_argument("--company", required=True, help="Company ID (e.g. comp_a)")
    parser.add_argument("--employee", required=True, help="Employee ID (e.g. emp_101)")
    parser.add_argument(
        "--role",
        choices=["employee", "manager"],
        default="employee",
        help="Caller role: employee or manager (default: employee)",
    )
    args = parser.parse_args()

    token = issue_token(company_id=args.company, employee_id=args.employee, role=args.role)
    print(token)


if __name__ == "__main__":
    main()
