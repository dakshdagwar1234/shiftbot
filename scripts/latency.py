"""Why this exists:
Measures end-to-end HTTP latency and LLM call counts for POST /chat using the real LLM.
Executes 40 sequential requests with rate-limiting pauses and calculates p50, p95, and max percentiles.
"""
import math
import os
import sys
import time
import uuid
import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from app.auth import issue_token

MESSAGES_POOL = [
    # Clear change requests
    "move my drop to 9 PM",
    "change my shift to 20:00",
    "move my cab to 21:00 today",
    "can I get a drop at 22:00?",
    "shift my drop to 8 PM",
    "book my cab for 21:00",
    "reschedule my drop to 20:00",
    "please move my drop to 21:00",
    # Ambiguous requests
    "can I go a bit late?",
    "I need to change my cab timing",
    "can I leave earlier today?",
    "I want to change my drop time",
    "is it possible to reschedule my cab?",
    "can I shift my cab booking?",
    "I might need a later cab",
    "can you adjust my slot?",
    # Follow-up times
    "9 PM",
    "20:00",
    "8 PM",
    "21:00",
    "10 PM",
    "22:00",
    "make it 8 PM instead",
    "at 9 PM please",
    # Status queries
    "what is my request status?",
    "check my cab booking status",
    "has my shift change been applied?",
    "status",
    "show my current cab status",
    "do I have any pending requests?",
    "is my shift change confirmed?",
    "what is the status of my trip?",
    # Other / general queries
    "hello, what can you do?",
    "what are your working hours?",
    "can you help me?",
    "how does this shift change bot work?",
    "who is my manager?",
    "thanks for the help",
    "good morning",
    "what time is the earliest drop?",
]


def calculate_percentile(data: list[float], percentile: float) -> float:
    """Calculates nearest-rank percentile from a sorted float array."""
    if not data:
        return 0.0
    k = (len(data) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    d0 = data[int(f)] * (c - k)
    d1 = data[int(c)] * (k - f)
    return d0 + d1


def run_latency_benchmark() -> None:
    base_url = os.environ.get("BASE_URL", "http://localhost:8000")
    token = issue_token("comp_alpha", "emp_001", "employee")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    latencies_ms: list[float] = []
    llm_calls_list: list[int] = []
    error_count = 0
    rate_limit_count = 0

    print(f"\n=======================================================")
    print(f"   POST /chat END-TO-END LATENCY BENCHMARK (40 REQUESTS)")
    print(f"=======================================================")
    print(f"Target Base URL: {base_url}")
    print(f"Free-tier safety pause: 2.0s between requests\n")

    with httpx.Client(base_url=base_url, timeout=30.0) as http_client:
        for idx, msg in enumerate(MESSAGES_POOL[:40], 1):
            conv_id = f"lat_{uuid.uuid4().hex[:8]}"
            payload = {"conversation_id": conv_id, "message": msg}

            t0 = time.perf_counter()
            try:
                resp = http_client.post("/chat", json=payload, headers=headers)
                duration_ms = (time.perf_counter() - t0) * 1000.0

                if resp.status_code == 200:
                    latencies_ms.append(duration_ms)
                    header_calls = resp.headers.get("X-LLM-Calls", "1")
                    try:
                        llm_calls_list.append(int(header_calls))
                    except ValueError:
                        llm_calls_list.append(1)

                    print(f"[{idx:02d}/40] {duration_ms:6.1f}ms | Calls: {header_calls} | '{msg[:35]}'")
                elif resp.status_code == 429:
                    rate_limit_count += 1
                    print(f"[{idx:02d}/40] HTTP 429 Rate Limited | '{msg[:35]}'")
                else:
                    error_count += 1
                    print(f"[{idx:02d}/40] HTTP {resp.status_code} Error | '{msg[:35]}'")
            except Exception as exc:
                error_count += 1
                print(f"[{idx:02d}/40] Exception: {exc} | '{msg[:35]}'")

            # Free tier pause
            time.sleep(2.0)

    # Compute statistics
    latencies_ms.sort()
    n = len(latencies_ms)
    p50 = calculate_percentile(latencies_ms, 50.0)
    p95 = calculate_percentile(latencies_ms, 95.0)
    max_lat = latencies_ms[-1] if latencies_ms else 0.0

    min_calls = min(llm_calls_list) if llm_calls_list else 0
    max_calls = max(llm_calls_list) if llm_calls_list else 0

    print("\n" + "=" * 50)
    print("           LATENCY BENCHMARK RESULTS")
    print("=" * 50)
    print(f"Total Requests (n):          {n}")
    print(f"Median Latency (p50):        {p50:.1f} ms")
    print(f"95th Percentile (p95):       {p95:.1f} ms")
    print(f"Maximum Latency (max):       {max_lat:.1f} ms")
    print(f"Errors (non-429):            {error_count}")
    print(f"Rate Limits (HTTP 429):      {rate_limit_count}")
    print(f"X-LLM-Calls (min / max):     {min_calls} / {max_calls}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    run_latency_benchmark()
