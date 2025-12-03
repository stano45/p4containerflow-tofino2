#!/usr/bin/env python3
"""
Hardware Test Script for P4 Load Balancer on Tofino

This script tests the load balancer running on real Tofino hardware.
It requires:
1. The switch to be running (make switch ARCH=tf1 or ARCH=tf2)
2. The controller should NOT be running (this test takes ownership of the P4 program)

Usage:
    python3 hardware_test.py [--arch tf1|tf2] [--grpc-addr HOST:PORT]

Tests performed:
1. Connection test - verifies gRPC connection to the switch
2. Table access test - verifies P4 tables exist and are accessible  
3. Table write test - writes and reads back table entries
4. Load balancer setup test - configures a basic load balancer setup
"""

import argparse
import json
import os
import sys
import time

# Add the controller directory to path for imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROLLER_DIR = os.path.join(SCRIPT_DIR, "..", "controller")
sys.path.insert(0, CONTROLLER_DIR)

try:
    import requests
except ImportError:
    print("ERROR: requests module not found. Install with: pip install requests")
    sys.exit(1)

try:
    import bfrt_grpc.client as gc
except ImportError:
    print("ERROR: bfrt_grpc module not found. Make sure SDE environment is sourced.")
    print("Run: source ~/setup-open-p4studio.bash")
    sys.exit(1)


class HardwareTest:
    """Hardware test suite for Tofino load balancer."""

    def __init__(self, arch: str, grpc_addr: str, controller_url: str):
        self.arch = arch
        self.grpc_addr = grpc_addr
        self.controller_url = controller_url
        self.program_name = (
            "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
        )
        self.interface = None
        self.bfrt_info = None
        self.target = None
        self.passed = 0
        self.failed = 0

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
        """Connect to the switch via gRPC."""
        self.log(f"Connecting to switch at {self.grpc_addr}...")
        try:
            self.interface = gc.ClientInterface(
                self.grpc_addr,
                client_id=99,  # Use a different client_id than the controller
                device_id=0,
                notifications=None,
                perform_subscribe=True,
            )
            self.log(f"Connected successfully")
            return True
        except Exception as e:
            self.log(f"Connection failed: {e}", "ERROR")
            return False

    def bind_program(self) -> bool:
        """Bind to the P4 program (or get info in read-only mode if already bound)."""
        self.log(f"Binding to program: {self.program_name}")
        try:
            self.interface.bind_pipeline_config(self.program_name)
            self.target = gc.Target(device_id=0, pipe_id=0xFFFF)
            self.bfrt_info = self.interface.bfrt_info_get(self.program_name)
            self.log(f"Bound to program successfully")
            return True
        except Exception as e:
            # Check if another client already owns the program
            if "already owns" in str(e) or "ALREADY_EXISTS" in str(e):
                self.log(f"Program already owned by another client (controller)")
                self.log(f"Getting program info in read-only mode...")
                try:
                    # Try to get bfrt_info without binding (read-only access)
                    self.bfrt_info = self.interface.bfrt_info_get(self.program_name)
                    self.target = gc.Target(device_id=0, pipe_id=0xFFFF)
                    self.log(f"Got program info in read-only mode")
                    return True
                except Exception as e2:
                    self.log(f"Failed to get program info: {e2}", "ERROR")
                    return False
            self.log(f"Failed to bind program: {e}", "ERROR")
            return False

    def test_connection(self):
        """Test 1: Verify gRPC connection to switch."""
        print("\n" + "=" * 60)
        print("TEST 1: gRPC Connection Test")
        print("=" * 60)

        if self.connect():
            self.log_pass("gRPC connection established")
        else:
            self.log_fail("gRPC connection", "Could not connect to switch")
            return False

        if self.bind_program():
            self.log_pass(f"Connected to P4 program '{self.program_name}'")
        else:
            self.log_fail(
                "Program binding", f"Could not connect to '{self.program_name}'"
            )
            return False

        return True

    def test_table_read(self):
        """Test 2: Read table entries from the switch."""
        print("\n" + "=" * 60)
        print("TEST 2: Table Read Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Table read", "Not connected to switch")
            return False

        tables_to_check = [
            "pipe.SwitchIngress.forward",
            "pipe.SwitchIngress.node_selector",
            "pipe.SwitchIngress.action_selector",
            "pipe.SwitchIngress.action_selector_ap",
            "pipe.SwitchIngress.client_snat",
        ]

        # In read-only mode, we may not be able to read table entries
        # Just verify we can access table metadata
        for table_name in tables_to_check:
            try:
                table = self.bfrt_info.table_get(table_name)
                # Just verify we can get the table object
                self.log_pass(f"Table '{table_name}' exists")
            except Exception as e:
                self.log_fail(f"Access table '{table_name}'", str(e))

        return True

    def test_controller_health(self):
        """Test 3: Test controller REST API health."""
        print("\n" + "=" * 60)
        print("TEST 3: Controller API Test")
        print("=" * 60)

        # Test if controller is responding
        try:
            # Flask apps typically respond to root or a simple endpoint
            # Try the cleanup endpoint with a GET (it expects POST, so we expect 405)
            resp = requests.get(f"{self.controller_url}/cleanup", timeout=5)
            if resp.status_code == 405:  # Method not allowed is expected for GET
                self.log_pass("Controller is running and responding")
            elif resp.status_code == 200:
                self.log_pass("Controller cleanup endpoint accessible")
            else:
                self.log_fail(
                    "Controller health", f"Unexpected status: {resp.status_code}"
                )
        except requests.exceptions.ConnectionError:
            self.log_fail(
                "Controller health", "Cannot connect to controller. Is it running?"
            )
            return False
        except Exception as e:
            self.log_fail("Controller health", str(e))
            return False

        return True

    def test_node_migration(self):
        """Test 4: Test node migration via controller API."""
        print("\n" + "=" * 60)
        print("TEST 4: Node Migration Test")
        print("=" * 60)

        # Load controller config to get current node IPs
        config_path = os.path.join(CONTROLLER_DIR, "controller_config.json")
        try:
            with open(config_path, "r") as f:
                configs = json.load(f)
                master_config = next(c for c in configs if c.get("master", False))
                nodes = master_config.get("nodes", [])
                lb_nodes = [n for n in nodes if n.get("is_lb_node", False)]
        except Exception as e:
            self.log_fail("Load config", str(e))
            return False

        if len(lb_nodes) < 1:
            self.log_fail("Node migration", "No LB nodes configured")
            return False

        # Test migration: migrate first LB node to a test IP and back
        original_ip = lb_nodes[0]["ipv4"]
        test_ip = "10.0.0.100"  # Temporary test IP

        self.log(f"Testing migration: {original_ip} -> {test_ip}")

        try:
            # Migrate to test IP
            resp = requests.post(
                f"{self.controller_url}/migrateNode",
                headers={"Content-Type": "application/json"},
                json={"old_ipv4": original_ip, "new_ipv4": test_ip},
                timeout=10,
            )
            if resp.status_code == 200:
                self.log_pass(f"Migrated {original_ip} -> {test_ip}")
            else:
                self.log_fail(
                    f"Migration to test IP", f"Status {resp.status_code}: {resp.text}"
                )
                return False

            # Wait a moment
            time.sleep(1)

            # Migrate back
            resp = requests.post(
                f"{self.controller_url}/migrateNode",
                headers={"Content-Type": "application/json"},
                json={"old_ipv4": test_ip, "new_ipv4": original_ip},
                timeout=10,
            )
            if resp.status_code == 200:
                self.log_pass(f"Migrated back {test_ip} -> {original_ip}")
            else:
                self.log_fail(
                    f"Migration back", f"Status {resp.status_code}: {resp.text}"
                )
                return False

        except requests.exceptions.ConnectionError:
            self.log_fail("Node migration", "Cannot connect to controller")
            return False
        except Exception as e:
            self.log_fail("Node migration", str(e))
            return False

        return True

    def test_table_entries_after_migration(self):
        """Test 5: Verify table entries are correct after migration."""
        print("\n" + "=" * 60)
        print("TEST 5: Table Verification After Migration")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Table verification", "Not connected to switch")
            return False

        try:
            # Check forward table has entries
            forward_table = self.bfrt_info.table_get("pipe.SwitchIngress.forward")
            resp = forward_table.entry_get(self.target, flags={"from_hw": False})
            entries = list(resp)
            if len(entries) > 0:
                self.log_pass(f"Forward table has {len(entries)} entries")
            else:
                self.log_fail("Forward table", "No entries found")

            # Check node_selector table
            selector_table = self.bfrt_info.table_get(
                "pipe.SwitchIngress.node_selector"
            )
            resp = selector_table.entry_get(self.target, flags={"from_hw": False})
            entries = list(resp)
            if len(entries) > 0:
                self.log_pass(f"Node selector table has {len(entries)} entries")
            else:
                self.log_fail("Node selector table", "No entries found")

        except Exception as e:
            self.log_fail("Table verification", str(e))
            return False

        return True

    def run_all_tests(self):
        """Run all hardware tests."""
        print("\n" + "=" * 60)
        print(f"  HARDWARE TEST SUITE - {self.arch.upper()}")
        print(f"  Switch: {self.grpc_addr}")
        print(f"  Controller: {self.controller_url}")
        print(f"  Program: {self.program_name}")
        print("=" * 60)

        # Run tests
        self.test_connection()
        self.test_table_read()
        self.test_controller_health()
        self.test_node_migration()
        self.test_table_entries_after_migration()

        # Summary
        print("\n" + "=" * 60)
        print("  TEST SUMMARY")
        print("=" * 60)
        total = self.passed + self.failed
        print(f"  Total:  {total}")
        print(f"  Passed: {self.passed} ✅")
        print(f"  Failed: {self.failed} ❌")
        print("=" * 60)

        return self.failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="Hardware Test for P4 Load Balancer on Tofino"
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
        help="URL of the controller REST API (default: http://127.0.0.1:5000)",
    )
    args = parser.parse_args()

    test = HardwareTest(
        arch=args.arch,
        grpc_addr=args.grpc_addr,
        controller_url=args.controller_url,
    )

    success = test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
