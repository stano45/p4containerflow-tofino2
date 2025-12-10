#!/usr/bin/env python3
"""
Hardware Controller Test Script for P4 Load Balancer on Tofino

This script tests the controller running on real Tofino hardware.
Unlike test_hardware_dataplane.py, this test requires the controller to be running
and tests the controller API endpoints and their effects on table state.

It requires:
1. The switch to be running (make switch ARCH=tf1 or ARCH=tf2)
2. The controller to be running (make controller)

Usage:
    python3 test/test_hardware_controller.py --arch tf1  # or tf2

Tests performed:
1. Controller health check - verifies controller HTTP API is accessible
2. Initial configuration test - verifies controller has populated tables correctly
3. Node migration test - tests the migrateNode API endpoint
4. Table state verification - verifies table entries match expected state
5. Cleanup test - verifies cleanup API properly removes all entries
"""

import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import bfrt_grpc.client as gc
    import requests
except ImportError as e:
    print(f"ERROR: Missing required module: {e}")
    print("Make sure SDE environment is sourced and requests is installed.")
    print("Run: source ~/setup-open-p4studio.bash")
    print("     pip install requests")
    sys.exit(1)


class HardwareControllerTest:
    """Hardware controller test suite for Tofino load balancer."""

    def __init__(self, arch: str, grpc_addr: str, controller_url: str, config_path: str):
        self.arch = arch
        self.grpc_addr = grpc_addr
        self.controller_url = controller_url
        self.config_path = config_path
        self.program_name = (
            "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
        )
        self.interface = None
        self.bfrt_info = None
        self.target = None
        self.passed = 0
        self.failed = 0

        # Load controller configuration
        self.config = None
        self.load_config()

    def load_config(self):
        """Load controller configuration file."""
        try:
            with open(self.config_path, "r") as f:
                configs = json.load(f)
                # Find the master switch config
                self.config = next(c for c in configs if c.get("master", False))
                self.log(f"Loaded config from {self.config_path}")
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

    def connect(self) -> bool:
        """Connect to the switch via gRPC (read-only access)."""
        self.log(f"Connecting to switch at {self.grpc_addr}...")
        try:
            # Use a different client_id to avoid conflicts with the controller
            self.interface = gc.ClientInterface(
                self.grpc_addr,
                client_id=99,  # Different from controller's client_id
                device_id=0,
                notifications=None,
                perform_subscribe=False,  # Don't subscribe, just observe
            )
            self.log("Connected successfully (read-only mode)")
            return True
        except Exception as e:
            self.log(f"Connection failed: {e}", "ERROR")
            return False

    def bind_program(self) -> bool:
        """Bind to the P4 program."""
        self.log(f"Binding to program: {self.program_name}")
        try:
            self.interface.bind_pipeline_config(self.program_name)
            self.target = gc.Target(device_id=0, pipe_id=0xFFFF)
            self.bfrt_info = self.interface.bfrt_info_get(self.program_name)
            self.log("Bound to program successfully")
            return True
        except Exception as e:
            self.log(f"Failed to bind program: {e}", "ERROR")
            return False

    def get_table_entries(self, table_name):
        """Get all entries from a table."""
        table = self.bfrt_info.table_get(table_name)
        resp = table.entry_get(self.target, flags={"from_hw": False})
        return list(resp)

    def count_table_entries(self, table_name):
        """Count entries in a table."""
        try:
            entries = self.get_table_entries(table_name)
            return len(entries)
        except Exception as e:
            self.log(f"Failed to count entries in {table_name}: {e}", "WARN")
            return -1

    def call_controller_api(self, endpoint: str, method: str = "POST", data: dict = None):
        """Call a controller API endpoint."""
        url = f"{self.controller_url}/{endpoint}"
        try:
            if method == "POST":
                if data:
                    resp = requests.post(url, json=data, timeout=5)
                else:
                    resp = requests.post(url, timeout=5)
            elif method == "GET":
                resp = requests.get(url, timeout=5)
            else:
                raise ValueError(f"Unsupported method: {method}")
            return resp
        except requests.exceptions.RequestException as e:
            self.log(f"HTTP request failed: {e}", "ERROR")
            return None

    def test_controller_health(self):
        """Test 1: Verify controller HTTP API is accessible."""
        print("\n" + "=" * 60)
        print("TEST 1: Controller Health Check")
        print("=" * 60)

        # Try to call cleanup endpoint with a short timeout to check if controller is up
        resp = self.call_controller_api("cleanup", method="POST")
        
        if resp is None:
            self.log_fail("Controller health check", "Controller not reachable")
            return False
        
        if resp.status_code == 200:
            self.log_pass("Controller HTTP API is accessible")
            return True
        else:
            self.log_fail(
                "Controller health check",
                f"Unexpected status code: {resp.status_code}"
            )
            return False

    def test_initial_configuration(self):
        """Test 2: Verify controller has populated tables correctly."""
        print("\n" + "=" * 60)
        print("TEST 2: Initial Configuration Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Initial configuration", "Not connected to switch")
            return False

        try:
            nodes = self.config.get("nodes", [])
            lb_nodes = [n for n in nodes if n.get("is_lb_node", False)]
            
            # Expected counts
            # forward table: one entry per node (client + LB nodes + any other nodes)
            expected_forward = len(nodes)
            # client_snat: one entry for the service port
            expected_snat = 1
            # action_selector_ap: one entry per LB node
            expected_ap = len(lb_nodes)
            # action_selector: one group
            expected_selector = 1
            # node_selector: one entry for the load balancer VIP
            expected_node_selector = 1

            self.log(f"Checking table entry counts...")
            self.log(f"  Expected nodes: {len(nodes)} (including {len(lb_nodes)} LB nodes)")

            # Check forward table
            forward_count = self.count_table_entries("pipe.SwitchIngress.forward")
            if forward_count == expected_forward:
                self.log_pass(f"Forward table: {forward_count} entries")
            else:
                self.log_fail(
                    "Forward table",
                    f"Expected {expected_forward} entries, got {forward_count}"
                )

            # Check client_snat table
            snat_count = self.count_table_entries("pipe.SwitchIngress.client_snat")
            if snat_count == expected_snat:
                self.log_pass(f"Client SNAT table: {snat_count} entries")
            else:
                self.log_fail(
                    "Client SNAT table",
                    f"Expected {expected_snat} entries, got {snat_count}"
                )

            # Check action_selector_ap table
            ap_count = self.count_table_entries("pipe.SwitchIngress.action_selector_ap")
            if ap_count == expected_ap:
                self.log_pass(f"Action selector AP table: {ap_count} entries")
            else:
                self.log_fail(
                    "Action selector AP table",
                    f"Expected {expected_ap} entries, got {ap_count}"
                )

            # Check action_selector table
            selector_count = self.count_table_entries("pipe.SwitchIngress.action_selector")
            if selector_count == expected_selector:
                self.log_pass(f"Action selector table: {selector_count} entries")
            else:
                self.log_fail(
                    "Action selector table",
                    f"Expected {expected_selector} entries, got {selector_count}"
                )

            # Check node_selector table
            node_selector_count = self.count_table_entries("pipe.SwitchIngress.node_selector")
            if node_selector_count == expected_node_selector:
                self.log_pass(f"Node selector table: {node_selector_count} entries")
            else:
                self.log_fail(
                    "Node selector table",
                    f"Expected {expected_node_selector} entries, got {node_selector_count}"
                )

            return True

        except Exception as e:
            self.log_fail("Initial configuration", str(e))
            return False

    def test_node_migration(self):
        """Test 3: Test the migrateNode API endpoint."""
        print("\n" + "=" * 60)
        print("TEST 3: Node Migration Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Node migration", "Not connected to switch")
            return False

        try:
            nodes = self.config.get("nodes", [])
            lb_nodes = [n for n in nodes if n.get("is_lb_node", False)]
            
            if len(lb_nodes) < 1:
                self.log_fail("Node migration", "No LB nodes found in config")
                return False

            old_ip = lb_nodes[0]["ipv4"]
            new_ip = "10.0.0.99"  # Test IP for migration
            
            self.log(f"Migrating node from {old_ip} to {new_ip}...")
            
            # Call migrateNode API
            resp = self.call_controller_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": old_ip, "new_ipv4": new_ip}
            )

            if resp is None:
                self.log_fail("Node migration API call", "Request failed")
                return False

            if resp.status_code != 200:
                self.log_fail(
                    "Node migration API",
                    f"Status {resp.status_code}: {resp.text}"
                )
                return False

            self.log_pass(f"Migration API returned success")

            # Wait a moment for the change to propagate
            time.sleep(0.5)

            # Verify the forward table has been updated
            forward_entries = self.get_table_entries("pipe.SwitchIngress.forward")
            
            # Look for the new IP in forward table entries
            found_new_ip = False
            found_old_ip = False
            for entry_data in forward_entries:
                entry = entry_data[0]
                # Check if the entry key contains our IPs
                for key_field in entry.key:
                    if hasattr(key_field, 'value') and hasattr(key_field.value, 'ipv4'):
                        ip_bytes = key_field.value.ipv4
                        ip_str = ".".join(str(b) for b in ip_bytes)
                        if ip_str == new_ip:
                            found_new_ip = True
                        if ip_str == old_ip:
                            found_old_ip = True

            if found_new_ip and not found_old_ip:
                self.log_pass(f"Forward table updated: {old_ip} -> {new_ip}")
            elif found_new_ip and found_old_ip:
                self.log_fail(
                    "Forward table update",
                    f"Both old and new IPs found (old IP should be removed)"
                )
            elif not found_new_ip:
                self.log_fail(
                    "Forward table update",
                    f"New IP {new_ip} not found in forward table"
                )

            # Migrate back to original IP
            self.log(f"Migrating back: {new_ip} -> {old_ip}...")
            resp = self.call_controller_api(
                "migrateNode",
                method="POST",
                data={"old_ipv4": new_ip, "new_ipv4": old_ip}
            )

            if resp and resp.status_code == 200:
                self.log_pass("Migration back to original IP successful")
            else:
                self.log_fail(
                    "Migration back",
                    f"Failed to migrate back: {resp.status_code if resp else 'No response'}"
                )

            return True

        except Exception as e:
            self.log_fail("Node migration", str(e))
            return False

    def test_table_state_consistency(self):
        """Test 4: Verify table state consistency."""
        print("\n" + "=" * 60)
        print("TEST 4: Table State Consistency Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Table state consistency", "Not connected to switch")
            return False

        try:
            # Verify all critical tables have entries
            critical_tables = [
                "pipe.SwitchIngress.forward",
                "pipe.SwitchIngress.client_snat",
                "pipe.SwitchIngress.action_selector_ap",
                "pipe.SwitchIngress.action_selector",
                "pipe.SwitchIngress.node_selector",
            ]

            all_ok = True
            for table_name in critical_tables:
                count = self.count_table_entries(table_name)
                if count > 0:
                    self.log_pass(f"Table '{table_name}': {count} entries")
                elif count == 0:
                    self.log_fail(
                        f"Table '{table_name}'",
                        "Table is empty (expected entries)"
                    )
                    all_ok = False
                else:
                    # count == -1, error occurred
                    all_ok = False

            return all_ok

        except Exception as e:
            self.log_fail("Table state consistency", str(e))
            return False

    def test_cleanup(self):
        """Test 5: Test the cleanup API endpoint."""
        print("\n" + "=" * 60)
        print("TEST 5: Cleanup Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Cleanup", "Not connected to switch")
            return False

        try:
            self.log("Calling cleanup API...")
            resp = self.call_controller_api("cleanup", method="POST")

            if resp is None:
                self.log_fail("Cleanup API call", "Request failed")
                return False

            if resp.status_code != 200:
                self.log_fail(
                    "Cleanup API",
                    f"Status {resp.status_code}: {resp.text}"
                )
                return False

            self.log_pass("Cleanup API returned success")

            # Wait for cleanup to complete
            time.sleep(0.5)

            # Verify all tables are empty
            tables_to_check = [
                "pipe.SwitchIngress.forward",
                "pipe.SwitchIngress.client_snat",
                "pipe.SwitchIngress.action_selector_ap",
                "pipe.SwitchIngress.action_selector",
                "pipe.SwitchIngress.node_selector",
            ]

            all_empty = True
            for table_name in tables_to_check:
                count = self.count_table_entries(table_name)
                if count == 0:
                    self.log_pass(f"Table '{table_name}' is empty")
                else:
                    self.log_fail(
                        f"Table '{table_name}'",
                        f"Still has {count} entries after cleanup"
                    )
                    all_empty = False

            # Re-initialize the controller by sending a simple request
            # This will cause the controller to repopulate tables on next startup
            # For now, we just note that manual restart may be needed
            if all_empty:
                self.log("NOTE: Cleanup successful. Restart controller to restore configuration.", "WARN")

            return all_empty

        except Exception as e:
            self.log_fail("Cleanup", str(e))
            return False

    def run_all_tests(self):
        """Run all hardware controller tests."""
        print("\n" + "=" * 60)
        print(f"  HARDWARE CONTROLLER TEST SUITE - {self.arch.upper()}")
        print(f"  Switch: {self.grpc_addr}")
        print(f"  Controller: {self.controller_url}")
        print(f"  Program: {self.program_name}")
        print("=" * 60)
        print("\nNOTE: Make sure both switch AND controller are running!")
        print("      Switch: make switch ARCH=" + self.arch)
        print("      Controller: make controller\n")

        # Connect to switch
        if not self.connect():
            print("\n⚠️  Connection failed - skipping remaining tests")
            return False

        if not self.bind_program():
            print("\n⚠️  Failed to bind to program - skipping remaining tests")
            return False

        # Run tests in order
        self.test_controller_health()
        self.test_initial_configuration()
        self.test_node_migration()
        self.test_table_state_consistency()
        
        # Cleanup test is last because it clears all tables
        print("\n" + "=" * 60)
        print("WARNING: The next test will CLEAR ALL TABLE ENTRIES!")
        print("         You will need to restart the controller afterwards.")
        print("=" * 60)
        input("Press Enter to continue with cleanup test (or Ctrl+C to skip)...")
        self.test_cleanup()

        # Summary
        print("\n" + "=" * 60)
        print("  TEST SUMMARY")
        print("=" * 60)
        total = self.passed + self.failed
        print(f"  Total:  {total}")
        print(f"  Passed: {self.passed} ✅")
        print(f"  Failed: {self.failed} ❌")
        print("=" * 60)

        if self.failed == 0:
            print("\n✅ All tests passed!")
            print("NOTE: Restart the controller to restore table entries:")
            print("      make controller")
        else:
            print("\n❌ Some tests failed. Check the output above.")

        return self.failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Hardware Controller Test for P4 Load Balancer on Tofino"
    )
    parser.add_argument(
        "--arch",
        choices=["tf1", "tf2"],
        default=os.environ.get("ARCH", "tf2"),
        help="Tofino architecture (default: tf2 or from ARCH env var)",
    )
    parser.add_argument(
        "--grpc-addr",
        default="127.0.0.1:50052",
        help="gRPC address of the switch (default: 127.0.0.1:50052)",
    )
    parser.add_argument(
        "--controller-url",
        default="http://127.0.0.1:5000",
        help="Controller HTTP API URL (default: http://127.0.0.1:5000)",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(
            os.path.dirname(__file__), "..", "controller", "controller_config.json"
        ),
        help="Path to controller config file",
    )
    args = parser.parse_args()

    # Verify config file exists
    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    test = HardwareControllerTest(
        arch=args.arch,
        grpc_addr=args.grpc_addr,
        controller_url=args.controller_url,
        config_path=args.config,
    )

    success = test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
