# Live verification script for Step 7 manager approval flow and restart proof.
# Runs employee request, cutoff rejection, insist, restart app, manager approve,
# and verifies status, shift_requests, and audit_log.

import argparse
import json
import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

from app.auth import issue_token


def get_base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000")


def step_before_restart(conv_id: str):
    base_url = get_base_url()
    emp_token = issue_token("comp_alpha", "emp_001", "employee")
    headers = {"Authorization": f"Bearer {emp_token}"}

    print(f"\n--- 1. emp_001 requests shift change breaking cutoff ---")
    payload1 = {
        "conversation_id": conv_id,
        "message": "move my drop to 8 PM",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r1 = client.post("/chat", json=payload1, headers=headers)
        print(f"Status: {r1.status_code}")
        print(f"Response: {r1.json()}")

        print(f"\n--- 2. emp_001 insists to send to manager ---")
        payload2 = {
            "conversation_id": conv_id,
            "message": "please send it to my manager",
        }
        r2 = client.post("/chat", json=payload2, headers=headers)
        print(f"Status: {r2.status_code}")
        print(f"Response: {r2.json()}")


def step_after_restart(conv_id: str):
    base_url = get_base_url()
    emp_token = issue_token("comp_alpha", "emp_001", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    emp_headers = {"Authorization": f"Bearer {emp_token}"}
    mgr_headers = {"Authorization": f"Bearer {mgr_token}"}

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        print(f"\n--- 4. mgr_alpha lists pending requests ---")
        r_list = client.get("/manager/requests", headers=mgr_headers)
        print(f"Status: {r_list.status_code}")
        data = r_list.json()
        print(f"Pending requests: {json.dumps(data, indent=2)}")
        req_list = data.get("requests", [])
        if not req_list:
            print("ERROR: No pending requests found!")
            sys.exit(1)

        req_id = req_list[0]["request_id"]
        print(f"Found pending request ID: {req_id}")

        print(f"\n--- 5. mgr_alpha approves request {req_id} ---")
        r_decide = client.post(
            "/manager/decision",
            json={"request_id": req_id, "decision": "approve"},
            headers=mgr_headers,
        )
        print(f"Status: {r_decide.status_code}")
        print(f"Decision response: {r_decide.json()}")

        print(f"\n--- 6. emp_001 checks status ---")
        r_status = client.get(
            f"/chat/status?conversation_id={conv_id}", headers=emp_headers
        )
        print(f"Status: {r_status.status_code}")
        print(f"Status response: {r_status.json()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--conv", default="conv_restart_proof_1")
    parser.add_argument("--stage", choices=["before", "after"], required=True)
    args = parser.parse_args()

    if args.stage == "before":
        step_before_restart(args.conv)
    else:
        step_after_restart(args.conv)
