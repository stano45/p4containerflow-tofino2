#!/usr/bin/env python3
"""
Hardware Controller Test Script for P4 Load Balancer on Tofino

This script tests the controller's HTTP API running on real Tofino hardware.
It does NOT connect directly to the switch - it only tests the controller
via its REST API endpoints.

Requirements:
1. The switch must be running (make switch ARCH=tf1 or ARCH=tf2)
2. The controller must be running (make controller)

Usage:
    python3 test/test_hardware_controller.py
    python3 test/test_hardware_controller.py --controller-url http://127.0.0.1:5000
    python3 test/test_hardware_controller.py --config /path/to/controller_config.json

Tests performed:
1. Controller health/reachability tests
2. migrateNode endpoint - valid requests
3. migrateNode endpoint - invalid requests and edge cases
4. cleanup endpoint - functionality and idempotency
5. Error handling and edge cases
"""

import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import requests
except ImportError:
    print("ERROR: requests module not found.")
    print("Install with: pip install requests")
    sys.exit(1)


class HardwareControllerTest:
    """HTTP API test suite for the P4 load balancer controller."""

    def __init__(self, controller_url: str, config_path: str):
        self.controller_url = controller_url.rstrip("/")
        self.config_path = config_path
        self.passed = 0
        self.failed = 0
        self.skipped = 0

        # Load controller configuration
        self.config = None
        self.lb_nodes = []
        self.non_lb_nodes = []
        self.load_config()

    def load_config(self):
        """Load controller configuration file to understand node setup."""
        try:
            with open(self.config_path, "r") as f:
                configs = json.load(f)
                # Find the master switch config
                self.config = next(c for c in configs if c.get("master", False))
                
                # Separate LB nodes from non-LB nodes
                for node in self.config.get("nodes", []):
                    if node.get("is_lb_node", False):
                        self.lb_nodes.append(node)
                    else:
                        self.non_lb_nodes.append(node)
                
                self.log(f"Loaded config from {self.config_path}")
                self.log(f"  LB nodes: {[n['ipv4'] for n in self.lb_nodes]}")
                self.log(f"  Non-LB nodes: {[n['ipv4'] for n in self.non_lb_nodes]}")
        except FileNotFoundError:
            self.log(f"Config file not found: {self.config_path}", "ERROR")
            sys.exit(1)
        except Exception as e:
            self.log(f"Failed to load config: {e}", "ERROR")
            sys.exit(1)

    def log(self, msg: str, level: str = "INFO"):
        """Print a log message."""
        print(f"[{level}] {msg}")

    def log_pass(self, test_name: str):
        """Log a passed test."""
        self.passed += 1
        print(f"  ✅ PASS: {test_name}")

    def log_fail(self, test_name: str, error: str):
        """Log a failed test."""
        self.failed += 1
        print(f"  ❌ FAIL: {test_name}")
        print(f"         Error: {error}")

    def log_skip(self, test_name: str, reason: str):
        """Log a skipped test."""
        self.skipped += 1
        print(f"  ⏭️  SKIP: {test_name}")
        print(f"         Reason: {reason}")

    def call_api(
        self,
        endpoint: str,
        method: str = "POST",
        data: dict = None,
        timeout: float = 5.0,
        raw_data: str = None,
    ):
        """
        Call a controller API endpoint.
        
        Args:
            endpoint: API endpoint (without leading slash)
            method: HTTP method (GET, POST, etc.)
            data: JSON data to send (will be serialized)
            timeout: Request timeout in seconds
            raw_data: Raw string data to send (for malformed JSON tests)
        
        Returns:
            requests.Response or None if request failed
        """
        url = f"{self.controller_url}/{endpoint}"
        headers = {"Content-Type": "application/json"}
        
        try:
            if method == "POST":
                if raw_data is not None:
                    resp = requests.post(url, data=raw_data, headers=headers, timeout=timeout)
                elif data is not None:
                    resp = requests.post(url, json=data, timeout=timeout)
                else:
                    resp = requests.post(url, timeout=timeout)
            elif method == "GET":
                resp = requests.get(url, timeout=timeout)
            elif method == "PUT":
                resp = requests.put(url, json=data, timeout=timeout)
            elif method == "DELETE":
                resp = requests.delete(url, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")
            return resp
        except requests.exceptions.ConnectionError:
            return None
        except requests.exceptions.Timeout:
            return None
        except requests.exceptions.RequestException as e:
            self.log(f"HTTP request failed: {e}", "WARN")
            return None

    # =========================================================================
    # Test Group 1: Controller Health/Reachability
    # =========================================================================

    def test_controller_reachable(self):
        """Test that the controller is reachable."""
        print("\n" + "=" * 60)
        print("TEST GROUP 1: Controller Health/Reachability")
        print("=" * 60)

        # Test 1.1: Basic connectivity via cleanup endpoint (always available)
        resp = self.call_api("cleanup", method="POST")
        if resp is None:
            self.log_fail(
                "Controller reachable",
                f"Cannot connect to controller at {self.controller_url}"
            )
            return False
        
        if resp.status_code == 200:
            self.log_pass("Controller is reachable and responding")
        else:
            self.log_fail(
                "Controller reachable",
                f"Unexpected status code: {resp.status_code}"
            )
            return False

        # Test 1.2: Check response is valid JSON
        try:
            resp_json = resp.json()
            if "status" in resp_json or "error" in resp_json:
                self.log_pass("Controller returns valid JSON responses")
            else:
                self.log_fail(
                    "JSON response format",
                    f"Unexpected response format: {resp_json}"
                )
        except json.JSONDecodeError:
            self.log_fail("JSON response format", "Response is not valid JSON")

        return True

    # =========================================================================
    # Test Group 2: migrateNode Endpoint - Valid Requests
    # =========================================================================

    def test_migrate_node_valid(self):
        """Test valid node migration requests."""
        print("\n" + "=" * 60)
        print("TEST GROUP 2: migrateNode Endpoint - Valid Requests")
        print("=" * 60)

        if len(self.lb_nodes) < 1:
            self.log_skip("Node migration tests", "No LB nodes in config")
            return

        # We need to restart the controller state, so call cleanup first
        # and note that this clears tables
        self.log("Note: Tests will modify controller state")

        original_ip = self.lb_nodes[0]["ipv4"]
        test_ip = "10.0.0.99"

        # Test 2.1: Valid migration to new IP
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": original_ip, "new_ipv4": test_ip}
        )
        
        if resp is None:
            self.log_fail("Migrate node (valid)", "No response from controller")
            return

        if resp.status_code == 200:
            self.log_pass(f"Migrate node: {original_ip} -> {test_ip}")
            try:
                resp_json = resp.json()
                if resp_json.get("status") == "success":
                    self.log_pass("Migration response contains success status")
                else:
                    self.log_fail(
                        "Migration response format",
                        f"Expected status=success, got: {resp_json}"
                    )
            except json.JSONDecodeError:
                self.log_fail("Migration response format", "Response is not valid JSON")
        else:
            self.log_fail(
                f"Migrate node: {original_ip} -> {test_ip}",
                f"Status {resp.status_code}: {resp.text}"
            )
            return

        # Test 2.2: Migrate back to original IP
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": test_ip, "new_ipv4": original_ip}
        )
        
        if resp and resp.status_code == 200:
            self.log_pass(f"Migrate back: {test_ip} -> {original_ip}")
        else:
            status = resp.status_code if resp else "No response"
            self.log_fail(
                f"Migrate back: {test_ip} -> {original_ip}",
                f"Status: {status}"
            )

        # Test 2.3: Multiple sequential migrations
        if len(self.lb_nodes) >= 2:
            ip1 = self.lb_nodes[0]["ipv4"]
            ip2 = self.lb_nodes[1]["ipv4"]
            temp_ip1 = "10.0.0.101"
            temp_ip2 = "10.0.0.102"

            # Migrate first node
            resp1 = self.call_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": ip1, "new_ipv4": temp_ip1}
            )
            
            # Migrate second node
            resp2 = self.call_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": ip2, "new_ipv4": temp_ip2}
            )

            if resp1 and resp1.status_code == 200 and resp2 and resp2.status_code == 200:
                self.log_pass("Multiple sequential migrations")
            else:
                self.log_fail(
                    "Multiple sequential migrations",
                    f"resp1={resp1.status_code if resp1 else 'None'}, "
                    f"resp2={resp2.status_code if resp2 else 'None'}"
                )

            # Restore original state
            self.call_api("migrateNode", data={"old_ipv4": temp_ip1, "new_ipv4": ip1})
            self.call_api("migrateNode", data={"old_ipv4": temp_ip2, "new_ipv4": ip2})

    # =========================================================================
    # Test Group 3: migrateNode Endpoint - Invalid Requests
    # =========================================================================

    def test_migrate_node_invalid(self):
        """Test invalid node migration requests and edge cases."""
        print("\n" + "=" * 60)
        print("TEST GROUP 3: migrateNode Endpoint - Invalid Requests")
        print("=" * 60)

        # Test 3.1: Missing old_ipv4 parameter
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"new_ipv4": "10.0.0.99"}
        )
        
        if resp and resp.status_code == 400:
            self.log_pass("Missing old_ipv4 returns 400")
        elif resp:
            self.log_fail(
                "Missing old_ipv4",
                f"Expected 400, got {resp.status_code}"
            )
        else:
            self.log_fail("Missing old_ipv4", "No response")

        # Test 3.2: Missing new_ipv4 parameter
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": "10.0.0.1"}
        )
        
        if resp and resp.status_code == 400:
            self.log_pass("Missing new_ipv4 returns 400")
        elif resp:
            self.log_fail(
                "Missing new_ipv4",
                f"Expected 400, got {resp.status_code}"
            )
        else:
            self.log_fail("Missing new_ipv4", "No response")

        # Test 3.3: Empty request body
        resp = self.call_api("migrateNode", method="POST", data={})
        
        if resp and resp.status_code == 400:
            self.log_pass("Empty request body returns 400")
        elif resp:
            self.log_fail(
                "Empty request body",
                f"Expected 400, got {resp.status_code}"
            )
        else:
            self.log_fail("Empty request body", "No response")

        # Test 3.4: Non-existent old IP (not an LB node)
        non_lb_ip = self.non_lb_nodes[0]["ipv4"] if self.non_lb_nodes else "10.0.0.0"
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": non_lb_ip, "new_ipv4": "10.0.0.99"}
        )
        
        if resp and resp.status_code == 500:
            self.log_pass(f"Non-LB node migration ({non_lb_ip}) returns 500")
            try:
                resp_json = resp.json()
                if "error" in resp_json:
                    self.log_pass("Error response contains error message")
            except json.JSONDecodeError:
                pass
        elif resp:
            self.log_fail(
                f"Non-LB node migration ({non_lb_ip})",
                f"Expected 500, got {resp.status_code}"
            )
        else:
            self.log_fail("Non-LB node migration", "No response")

        # Test 3.5: Completely unknown IP
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": "192.168.255.255", "new_ipv4": "10.0.0.99"}
        )
        
        if resp and resp.status_code == 500:
            self.log_pass("Unknown IP migration returns 500")
        elif resp:
            self.log_fail(
                "Unknown IP migration",
                f"Expected 500, got {resp.status_code}"
            )
        else:
            self.log_fail("Unknown IP migration", "No response")

        # Test 3.6: Invalid JSON (malformed)
        resp = self.call_api(
            "migrateNode",
            method="POST",
            raw_data="{invalid json"
        )
        
        if resp and resp.status_code in [400, 500]:
            self.log_pass(f"Malformed JSON returns {resp.status_code}")
        elif resp:
            self.log_fail(
                "Malformed JSON",
                f"Expected 400/500, got {resp.status_code}"
            )
        else:
            self.log_fail("Malformed JSON", "No response")

        # Test 3.7: Wrong HTTP method (GET instead of POST)
        resp = self.call_api("migrateNode", method="GET")
        
        if resp and resp.status_code == 405:
            self.log_pass("GET on migrateNode returns 405 Method Not Allowed")
        elif resp:
            # Some frameworks return 404 for method not allowed
            if resp.status_code in [404, 405]:
                self.log_pass(f"GET on migrateNode returns {resp.status_code}")
            else:
                self.log_fail(
                    "GET on migrateNode",
                    f"Expected 405, got {resp.status_code}"
                )
        else:
            self.log_fail("GET on migrateNode", "No response")

        # Test 3.8: Null values in parameters
        resp = self.call_api(
            "migrateNode",
            method="POST",
            data={"old_ipv4": None, "new_ipv4": None}
        )
        
        if resp and resp.status_code == 400:
            self.log_pass("Null parameter values return 400")
        elif resp:
            # 500 is also acceptable if it fails during processing
            if resp.status_code == 500:
                self.log_pass(f"Null parameter values return {resp.status_code}")
            else:
                self.log_fail(
                    "Null parameter values",
                    f"Expected 400/500, got {resp.status_code}"
                )
        else:
            self.log_fail("Null parameter values", "No response")

        # Test 3.9: Same IP for old and new
        if len(self.lb_nodes) >= 1:
            same_ip = self.lb_nodes[0]["ipv4"]
            resp = self.call_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": same_ip, "new_ipv4": same_ip}
            )
            
            # This could succeed (no-op) or fail - either is acceptable
            if resp:
                self.log_pass(f"Same IP migration returns {resp.status_code}")
            else:
                self.log_fail("Same IP migration", "No response")

    # =========================================================================
    # Test Group 4: cleanup Endpoint
    # =========================================================================

    def test_cleanup_endpoint(self):
        """Test the cleanup endpoint functionality."""
        print("\n" + "=" * 60)
        print("TEST GROUP 4: cleanup Endpoint")
        print("=" * 60)

        # Test 4.1: Basic cleanup call
        resp = self.call_api("cleanup", method="POST")
        
        if resp is None:
            self.log_fail("Cleanup endpoint", "No response from controller")
            return

        if resp.status_code == 200:
            self.log_pass("Cleanup endpoint returns 200")
            try:
                resp_json = resp.json()
                if resp_json.get("status") == "success":
                    self.log_pass("Cleanup response contains success status")
                if "message" in resp_json:
                    self.log_pass("Cleanup response contains message")
            except json.JSONDecodeError:
                self.log_fail("Cleanup response format", "Response is not valid JSON")
        else:
            self.log_fail(
                "Cleanup endpoint",
                f"Status {resp.status_code}: {resp.text}"
            )

        # Test 4.2: Multiple cleanup calls (idempotency)
        resp1 = self.call_api("cleanup", method="POST")
        resp2 = self.call_api("cleanup", method="POST")
        resp3 = self.call_api("cleanup", method="POST")

        if all(r and r.status_code == 200 for r in [resp1, resp2, resp3]):
            self.log_pass("Multiple cleanup calls are idempotent (all return 200)")
        else:
            statuses = [r.status_code if r else "None" for r in [resp1, resp2, resp3]]
            self.log_fail(
                "Cleanup idempotency",
                f"Expected all 200, got: {statuses}"
            )

        # Test 4.3: Wrong HTTP method (GET instead of POST)
        resp = self.call_api("cleanup", method="GET")
        
        if resp and resp.status_code in [404, 405]:
            self.log_pass(f"GET on cleanup returns {resp.status_code}")
        elif resp:
            self.log_fail(
                "GET on cleanup",
                f"Expected 404/405, got {resp.status_code}"
            )
        else:
            self.log_fail("GET on cleanup", "No response")

    # =========================================================================
    # Test Group 5: Invalid Endpoints
    # =========================================================================

    def test_invalid_endpoints(self):
        """Test behavior for non-existent endpoints."""
        print("\n" + "=" * 60)
        print("TEST GROUP 5: Invalid Endpoints")
        print("=" * 60)

        # Test 5.1: Non-existent endpoint
        resp = self.call_api("nonexistent", method="POST")
        
        if resp and resp.status_code == 404:
            self.log_pass("Non-existent endpoint returns 404")
        elif resp:
            self.log_fail(
                "Non-existent endpoint",
                f"Expected 404, got {resp.status_code}"
            )
        else:
            self.log_fail("Non-existent endpoint", "No response")

        # Test 5.2: Root endpoint
        resp = self.call_api("", method="GET")
        
        if resp:
            self.log_pass(f"Root endpoint returns {resp.status_code}")
        else:
            self.log_fail("Root endpoint", "No response")

        # Test 5.3: Random path
        resp = self.call_api("api/v1/random/path", method="GET")
        
        if resp and resp.status_code == 404:
            self.log_pass("Random path returns 404")
        elif resp:
            self.log_fail(
                "Random path",
                f"Expected 404, got {resp.status_code}"
            )
        else:
            self.log_fail("Random path", "No response")

    # =========================================================================
    # Test Group 6: Response Time / Performance
    # =========================================================================

    def test_response_times(self):
        """Test API response times are reasonable."""
        print("\n" + "=" * 60)
        print("TEST GROUP 6: Response Times")
        print("=" * 60)

        max_response_time = 2.0  # seconds

        # Test 6.1: cleanup response time
        start = time.time()
        resp = self.call_api("cleanup", method="POST")
        elapsed = time.time() - start

        if resp and elapsed < max_response_time:
            self.log_pass(f"Cleanup response time: {elapsed:.3f}s (< {max_response_time}s)")
        elif resp:
            self.log_fail(
                "Cleanup response time",
                f"Took {elapsed:.3f}s (> {max_response_time}s)"
            )
        else:
            self.log_fail("Cleanup response time", "No response")

        # Test 6.2: migrateNode response time (if we have LB nodes)
        if len(self.lb_nodes) >= 1:
            original_ip = self.lb_nodes[0]["ipv4"]
            test_ip = "10.0.0.98"

            start = time.time()
            resp = self.call_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": original_ip, "new_ipv4": test_ip}
            )
            elapsed = time.time() - start

            if resp and resp.status_code == 200 and elapsed < max_response_time:
                self.log_pass(f"migrateNode response time: {elapsed:.3f}s (< {max_response_time}s)")
            elif resp and resp.status_code == 200:
                self.log_fail(
                    "migrateNode response time",
                    f"Took {elapsed:.3f}s (> {max_response_time}s)"
                )
            else:
                self.log_skip("migrateNode response time", "Migration failed")

            # Restore
            self.call_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": test_ip, "new_ipv4": original_ip}
            )

    # =========================================================================
    # Main Test Runner
    # =========================================================================

    def run_all_tests(self):
        """Run all hardware controller tests."""
        print("\n" + "=" * 60)
        print("  HARDWARE CONTROLLER API TEST SUITE")
        print(f"  Controller: {self.controller_url}")
        print(f"  Config: {self.config_path}")
        print("=" * 60)
        print("\nNOTE: Make sure both switch AND controller are running!")
        print("      Switch: make switch ARCH=tf1  (or tf2)")
        print("      Controller: make controller\n")

        # Check controller is reachable first
        if not self.test_controller_reachable():
            print("\n⚠️  Controller not reachable - skipping remaining tests")
            print(f"    Make sure the controller is running at {self.controller_url}")
            return False

        # Run all test groups
        self.test_migrate_node_valid()
        self.test_migrate_node_invalid()
        self.test_cleanup_endpoint()
        self.test_invalid_endpoints()
        self.test_response_times()

        # Summary
        print("\n" + "=" * 60)
        print("  TEST SUMMARY")
        print("=" * 60)
        total = self.passed + self.failed + self.skipped
        print(f"  Total:   {total}")
        print(f"  Passed:  {self.passed} ✅")
        print(f"  Failed:  {self.failed} ❌")
        print(f"  Skipped: {self.skipped} ⏭️")
        print("=" * 60)

        if self.failed == 0:
            print("\n✅ All tests passed!")
        else:
            print("\n❌ Some tests failed. Check the output above.")

        print("\nNOTE: Tests may have modified controller state.")
        print("      Restart the controller to restore original configuration:")
        print("      make controller")

        return self.failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Hardware Controller API Test for P4 Load Balancer"
    )
    parser.add_argument(
        "--controller-url",
        default="http://127.0.0.1:5000",
        help="Controller HTTP API URL (default: http://127.0.0.1:5000)",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(SCRIPT_DIR, "..", "controller", "controller_config.json"),
        help="Path to controller config file",
    )
    args = parser.parse_args()

    # Verify config file exists
    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    test = HardwareControllerTest(
        controller_url=args.controller_url,
        config_path=args.config,
    )

    success = test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
