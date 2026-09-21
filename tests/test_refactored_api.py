"""Integration test suite for the refactored modular FastAPI application."""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from api import app

def run_tests():
    client = TestClient(app)
    passed = 0
    failed = 0

    endpoints_to_test = [
        ("GET", "/health", 200),
        ("GET", "/prompts", 200),
        ("GET", "/", 200),
        ("GET", "/graph-admin", 200),
        ("GET", "/docs", 200),
    ]

    print("\n--- Running Integration Smoke Tests ---")
    for method, path, expected_status in endpoints_to_test:
        try:
            if method == "GET":
                res = client.get(path)
            elif method == "POST":
                res = client.post(path, json={})
            
            if res.status_code == expected_status:
                print(f" [PASS] {method} {path} -> {res.status_code}")
                passed += 1
            else:
                print(f" [FAIL] {method} {path} -> Expected {expected_status}, got {res.status_code}: {res.text}")
                failed += 1
        except Exception as e:
            print(f" [ERROR] {method} {path} -> {e}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed.\n")
    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
