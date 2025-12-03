#!/usr/bin/env python3
"""
Hardware Test Script for P4 Load Balancer on Tofino

This script tests the load balancer running on real Tofino hardware.
It requires:
1. The switch to be running (make switch ARCH=tf1 or ARCH=tf2)
2. The controller should NOT be running (this test takes ownership of the P4 program)

Usage:
    make test-hardware ARCH=tf1  # or tf2

Tests performed:
1. Connection test - verifies gRPC connection to the switch and binds to P4 program
2. Table access test - verifies P4 tables exist and are accessible
3. Table write/read test - writes entries and reads them back
4. Load balancer setup test - configures a complete load balancer setup
5. Cleanup test - verifies all entries can be deleted
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import bfrt_grpc.client as gc
except ImportError:
    print("ERROR: bfrt_grpc module not found. Make sure SDE environment is sourced.")
    print("Run: source ~/setup-open-p4studio.bash")
    sys.exit(1)


class HardwareTest:
    """Hardware test suite for Tofino load balancer."""

    def __init__(self, arch: str, grpc_addr: str):
        self.arch = arch
        self.grpc_addr = grpc_addr
        self.program_name = (
            "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
        )
        self.interface = None
        self.bfrt_info = None
        self.target = None
        self.passed = 0
        self.failed = 0

        # Test configuration
        self.lb_ip = "10.0.0.10"
        self.client_ip = "10.0.0.0"
        self.server_ips = ["10.0.0.1", "10.0.0.2"]
        self.client_port = 1
        self.server_ports = [2, 3]
        self.service_port = 12345

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
                client_id=0,
                device_id=0,
                notifications=None,
                perform_subscribe=True,
            )
            self.log("Connected successfully")
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
            if "already owns" in str(e) or "ALREADY_EXISTS" in str(e):
                self.log(
                    "ERROR: Another client (controller?) already owns this program",
                    "ERROR",
                )
                self.log(
                    "Please stop the controller before running hardware tests", "ERROR"
                )
            else:
                self.log(f"Failed to bind program: {e}", "ERROR")
            return False

    def insert_table_entry(
        self, table_name, key_fields, action_name=None, data_fields=None
    ):
        """Insert a table entry."""
        table = self.bfrt_info.table_get(table_name)
        key_list = [table.make_key(key_fields)]
        if data_fields:
            data_list = [table.make_data(data_fields, action_name)]
        else:
            data_list = [table.make_data([], action_name)]
        table.entry_add(self.target, key_list, data_list)

    def delete_table_entry(self, table_name, key_fields):
        """Delete a table entry."""
        table = self.bfrt_info.table_get(table_name)
        key_list = [table.make_key(key_fields)]
        table.entry_del(self.target, key_list)

    def clear_table(self, table_name):
        """Clear all entries from a table."""
        table = self.bfrt_info.table_get(table_name)
        table.entry_del(self.target)

    def get_table_entries(self, table_name):
        """Get all entries from a table."""
        table = self.bfrt_info.table_get(table_name)
        resp = table.entry_get(self.target, flags={"from_hw": False})
        return list(resp)

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
            self.log_pass(f"Bound to P4 program '{self.program_name}'")
        else:
            self.log_fail("Program binding", f"Could not bind to '{self.program_name}'")
            return False

        return True

    def test_table_access(self):
        """Test 2: Verify P4 tables exist and are accessible."""
        print("\n" + "=" * 60)
        print("TEST 2: Table Access Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Table access", "Not connected to switch")
            return False

        tables_to_check = [
            "pipe.SwitchIngress.forward",
            "pipe.SwitchIngress.node_selector",
            "pipe.SwitchIngress.action_selector",
            "pipe.SwitchIngress.action_selector_ap",
            "pipe.SwitchIngress.client_snat",
        ]

        all_passed = True
        for table_name in tables_to_check:
            try:
                table = self.bfrt_info.table_get(table_name)
                self.log_pass(f"Table '{table_name}' accessible")
            except Exception as e:
                self.log_fail(f"Access table '{table_name}'", str(e))
                all_passed = False

        return all_passed

    def test_table_write_read(self):
        """Test 3: Write and read back table entries."""
        print("\n" + "=" * 60)
        print("TEST 3: Table Write/Read Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Table write/read", "Not connected to switch")
            return False

        try:
            # Test forward table
            self.log("Testing forward table write/read...")
            test_ip = "192.168.1.1"
            test_port = 5

            self.insert_table_entry(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
                "SwitchIngress.set_egress_port",
                [gc.DataTuple("port", test_port)],
            )
            self.log_pass("Forward table entry written")

            # Read back
            entries = self.get_table_entries("pipe.SwitchIngress.forward")
            if len(entries) > 0:
                self.log_pass(f"Forward table entry read back ({len(entries)} entries)")
            else:
                self.log_fail("Forward table read", "No entries found after write")

            # Clean up
            self.delete_table_entry(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
            )
            self.log_pass("Forward table entry deleted")

        except Exception as e:
            self.log_fail("Table write/read", str(e))
            return False

        return True

    def test_load_balancer_setup(self):
        """Test 4: Configure a complete load balancer setup."""
        print("\n" + "=" * 60)
        print("TEST 4: Load Balancer Setup Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("LB setup", "Not connected to switch")
            return False

        try:
            # Clear any existing entries first
            self.log("Clearing existing entries...")
            for table in [
                "pipe.SwitchIngress.forward",
                "pipe.SwitchIngress.client_snat",
                "pipe.SwitchIngress.node_selector",
                "pipe.SwitchIngress.action_selector",
                "pipe.SwitchIngress.action_selector_ap",
            ]:
                try:
                    self.clear_table(table)
                except:
                    pass  # Table might already be empty

            # 1. Add forward entries
            self.log("Adding forward entries...")
            self.insert_table_entry(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(self.client_ip))],
                "SwitchIngress.set_egress_port",
                [gc.DataTuple("port", self.client_port)],
            )
            for i, server_ip in enumerate(self.server_ips):
                self.insert_table_entry(
                    "pipe.SwitchIngress.forward",
                    [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(server_ip))],
                    "SwitchIngress.set_egress_port",
                    [gc.DataTuple("port", self.server_ports[i])],
                )
            self.log_pass("Forward entries added")

            # 2. Add client SNAT entry
            self.log("Adding client SNAT entry...")
            self.insert_table_entry(
                "pipe.SwitchIngress.client_snat",
                [gc.KeyTuple("hdr.tcp.src_port", self.service_port)],
                "SwitchIngress.set_rewrite_src",
                [gc.DataTuple("new_src", gc.ipv4_to_bytes(self.lb_ip))],
            )
            self.log_pass("Client SNAT entry added")

            # 3. Add action profile entries
            self.log("Adding action profile entries...")
            for i, server_ip in enumerate(self.server_ips):
                self.insert_table_entry(
                    "pipe.SwitchIngress.action_selector_ap",
                    [gc.KeyTuple("$ACTION_MEMBER_ID", i)],
                    "SwitchIngress.set_rewrite_dst",
                    [gc.DataTuple("new_dst", gc.ipv4_to_bytes(server_ip))],
                )
            self.log_pass("Action profile entries added")

            # 4. Add selector group
            self.log("Adding selector group...")
            selector_table = self.bfrt_info.table_get(
                "pipe.SwitchIngress.action_selector"
            )
            key = [selector_table.make_key([gc.KeyTuple("$SELECTOR_GROUP_ID", 1)])]
            data = [
                selector_table.make_data(
                    [
                        gc.DataTuple("$MAX_GROUP_SIZE", 4),
                        gc.DataTuple("$ACTION_MEMBER_ID", int_arr_val=[0, 1]),
                        gc.DataTuple(
                            "$ACTION_MEMBER_STATUS", bool_arr_val=[True, True]
                        ),
                    ]
                )
            ]
            selector_table.entry_add(self.target, key, data)
            self.log_pass("Selector group added")

            # 5. Add node selector entry
            self.log("Adding node selector entry...")
            self.insert_table_entry(
                "pipe.SwitchIngress.node_selector",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(self.lb_ip))],
                None,
                [gc.DataTuple("$SELECTOR_GROUP_ID", 1)],
            )
            self.log_pass("Node selector entry added")

            # Verify setup
            self.log("Verifying setup...")
            forward_entries = self.get_table_entries("pipe.SwitchIngress.forward")
            if len(forward_entries) >= 3:
                self.log_pass(f"Setup verified: {len(forward_entries)} forward entries")
            else:
                self.log_fail(
                    "Setup verification",
                    f"Expected 3+ forward entries, got {len(forward_entries)}",
                )

        except Exception as e:
            self.log_fail("Load balancer setup", str(e))
            return False

        return True

    def test_cleanup(self):
        """Test 5: Clean up all table entries."""
        print("\n" + "=" * 60)
        print("TEST 5: Cleanup Test")
        print("=" * 60)

        if not self.bfrt_info:
            self.log_fail("Cleanup", "Not connected to switch")
            return False

        try:
            # Clear tables in reverse order of dependencies
            tables_to_clear = [
                "pipe.SwitchIngress.node_selector",
                "pipe.SwitchIngress.action_selector",
                "pipe.SwitchIngress.action_selector_ap",
                "pipe.SwitchIngress.client_snat",
                "pipe.SwitchIngress.forward",
            ]

            for table_name in tables_to_clear:
                try:
                    self.clear_table(table_name)
                    self.log_pass(f"Cleared table '{table_name}'")
                except Exception as e:
                    # Some tables might already be empty
                    if "OBJECT_NOT_FOUND" in str(e) or "not found" in str(e).lower():
                        self.log_pass(f"Table '{table_name}' already empty")
                    else:
                        self.log_fail(f"Clear table '{table_name}'", str(e))

        except Exception as e:
            self.log_fail("Cleanup", str(e))
            return False

        return True

    def run_all_tests(self):
        """Run all hardware tests."""
        print("\n" + "=" * 60)
        print(f"  HARDWARE TEST SUITE - {self.arch.upper()}")
        print(f"  Switch: {self.grpc_addr}")
        print(f"  Program: {self.program_name}")
        print("=" * 60)
        print("\nNOTE: Make sure the controller is NOT running!")
        print("      This test needs exclusive access to the P4 program.\n")

        # Run tests in order
        if not self.test_connection():
            print("\n⚠️  Connection failed - skipping remaining tests")
            return False

        self.test_table_access()
        self.test_table_write_read()
        self.test_load_balancer_setup()
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
    args = parser.parse_args()

    test = HardwareTest(
        arch=args.arch,
        grpc_addr=args.grpc_addr,
    )

    success = test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
