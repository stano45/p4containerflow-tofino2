#!/usr/bin/env python3
"""
Hardware Dataplane Tests for P4 Load Balancer on Tofino

This module tests the load balancer dataplane running on real Tofino hardware.
It requires:
1. The switch to be running (make switch ARCH=tf1 or ARCH=tf2)
2. The controller should NOT be running (this test takes ownership of the P4 program)
3. SDE environment sourced (source ~/setup-open-p4studio.bash)

Usage:
    make test-hardware ARCH=tf1  # or tf2
    
    # Or run specific tests:
    cd test && uv run pytest test_hardware_dataplane.py -v -k "TestTableAccess"
"""

import os
import sys

import pytest

# Try to import bfrt_grpc - will fail if SDE not sourced
try:
    import bfrt_grpc.client as gc
    BFRT_AVAILABLE = True
except ImportError:
    BFRT_AVAILABLE = False
    gc = None


# =============================================================================
# Skip entire module if bfrt_grpc not available
# =============================================================================

pytestmark = pytest.mark.skipif(
    not BFRT_AVAILABLE,
    reason="bfrt_grpc module not found. Source SDE environment: source ~/setup-open-p4studio.bash"
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(scope="module")
def switch_connection(grpc_addr, program_name):
    """
    Connect to switch and bind to P4 program.
    
    This fixture provides exclusive access to the switch for the test module.
    The connection is shared across all tests in the module.
    """
    if not BFRT_AVAILABLE:
        pytest.skip("bfrt_grpc not available")
    
    # Connect to switch
    try:
        interface = gc.ClientInterface(
            grpc_addr,
            client_id=0,
            device_id=0,
            notifications=None,
            perform_subscribe=True,
        )
    except Exception as e:
        pytest.fail(f"Failed to connect to switch at {grpc_addr}: {e}")
    
    # Bind to P4 program
    try:
        interface.bind_pipeline_config(program_name)
        target = gc.Target(device_id=0, pipe_id=0xFFFF)
        bfrt_info = interface.bfrt_info_get(program_name)
    except Exception as e:
        if "already owns" in str(e) or "ALREADY_EXISTS" in str(e):
            pytest.fail(
                "Another client (controller?) already owns this program. "
                "Please stop the controller before running hardware tests."
            )
        else:
            pytest.fail(f"Failed to bind to program '{program_name}': {e}")
    
    # Yield connection info as a dict
    yield {
        "interface": interface,
        "target": target,
        "bfrt_info": bfrt_info,
        "program_name": program_name,
    }
    
    # Cleanup on teardown (optional - tables cleared in test_cleanup)


@pytest.fixture(scope="module")
def table_helper(switch_connection):
    """Helper class for table operations."""
    return TableHelper(switch_connection)


class TableHelper:
    """Helper class for P4 table operations."""
    
    # Test configuration
    LB_IP = "10.0.0.10"
    CLIENT_IP = "10.0.0.0"
    SERVER_IPS = ["10.0.0.1", "10.0.0.2"]
    CLIENT_PORT = 1
    SERVER_PORTS = [2, 3]
    SERVICE_PORT = 12345
    
    # Table names
    TABLES = [
        "pipe.SwitchIngress.forward",
        "pipe.SwitchIngress.node_selector",
        "pipe.SwitchIngress.action_selector",
        "pipe.SwitchIngress.action_selector_ap",
        "pipe.SwitchIngress.client_snat",
    ]
    
    def __init__(self, conn):
        self.target = conn["target"]
        self.bfrt_info = conn["bfrt_info"]
    
    def get_table(self, table_name):
        """Get a table object by name."""
        return self.bfrt_info.table_get(table_name)
    
    def insert_entry(self, table_name, key_fields, action_name=None, data_fields=None):
        """Insert a table entry."""
        table = self.get_table(table_name)
        key_list = [table.make_key(key_fields)]
        if data_fields:
            data_list = [table.make_data(data_fields, action_name)]
        else:
            data_list = [table.make_data([], action_name)]
        table.entry_add(self.target, key_list, data_list)
    
    def delete_entry(self, table_name, key_fields):
        """Delete a table entry."""
        table = self.get_table(table_name)
        key_list = [table.make_key(key_fields)]
        table.entry_del(self.target, key_list)
    
    def clear_table(self, table_name):
        """Clear all entries from a table."""
        table = self.get_table(table_name)
        table.entry_del(self.target)
    
    def get_entries(self, table_name):
        """Get all entries from a table."""
        table = self.get_table(table_name)
        resp = table.entry_get(self.target, flags={"from_hw": False})
        return list(resp)
    
    def clear_all_tables(self):
        """Clear all test tables, ignoring errors for empty tables."""
        for table_name in self.TABLES:
            try:
                self.clear_table(table_name)
            except Exception:
                pass  # Table might already be empty


# =============================================================================
# Test: Connection
# =============================================================================

class TestConnection:
    """Tests for gRPC connection to switch."""
    
    def test_connection_established(self, switch_connection):
        """Should successfully connect to switch via gRPC."""
        assert switch_connection["interface"] is not None
    
    def test_program_bound(self, switch_connection):
        """Should successfully bind to P4 program."""
        assert switch_connection["bfrt_info"] is not None
        assert switch_connection["target"] is not None


# =============================================================================
# Test: Table Access
# =============================================================================

class TestTableAccess:
    """Tests for P4 table accessibility."""
    
    @pytest.mark.parametrize("table_name", TableHelper.TABLES)
    def test_table_accessible(self, table_helper, table_name):
        """Each P4 table should be accessible."""
        table = table_helper.get_table(table_name)
        assert table is not None, f"Table '{table_name}' not found"


# =============================================================================
# Test: Table Write/Read
# =============================================================================

class TestTableWriteRead:
    """Tests for table write and read operations."""
    
    def test_forward_table_write(self, table_helper):
        """Should write entry to forward table."""
        test_ip = "192.168.1.1"
        test_port = 5
        
        table_helper.insert_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", test_port)],
        )
        # If no exception, write succeeded
    
    def test_forward_table_read(self, table_helper):
        """Should read entries from forward table."""
        entries = table_helper.get_entries("pipe.SwitchIngress.forward")
        assert len(entries) > 0, "No entries found after write"
    
    def test_forward_table_delete(self, table_helper):
        """Should delete entry from forward table."""
        test_ip = "192.168.1.1"
        
        table_helper.delete_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
        )
        # If no exception, delete succeeded


# =============================================================================
# Test: Load Balancer Setup
# =============================================================================

class TestLoadBalancerSetup:
    """Tests for complete load balancer configuration."""
    
    def test_clear_existing_entries(self, table_helper):
        """Should clear any existing table entries."""
        table_helper.clear_all_tables()
        # If no exception, clear succeeded
    
    def test_add_forward_entries(self, table_helper):
        """Should add forward table entries for client and servers."""
        # Client entry
        table_helper.insert_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(table_helper.CLIENT_IP))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", table_helper.CLIENT_PORT)],
        )
        
        # Server entries
        for i, server_ip in enumerate(table_helper.SERVER_IPS):
            table_helper.insert_entry(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(server_ip))],
                "SwitchIngress.set_egress_port",
                [gc.DataTuple("port", table_helper.SERVER_PORTS[i])],
            )
    
    def test_add_client_snat_entry(self, table_helper):
        """Should add client SNAT entry."""
        table_helper.insert_entry(
            "pipe.SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", table_helper.SERVICE_PORT)],
            "SwitchIngress.set_rewrite_src",
            [gc.DataTuple("new_src", gc.ipv4_to_bytes(table_helper.LB_IP))],
        )
    
    def test_add_action_profile_entries(self, table_helper):
        """Should add action profile entries for servers."""
        for i, server_ip in enumerate(table_helper.SERVER_IPS):
            table_helper.insert_entry(
                "pipe.SwitchIngress.action_selector_ap",
                [gc.KeyTuple("$ACTION_MEMBER_ID", i)],
                "SwitchIngress.set_rewrite_dst",
                [gc.DataTuple("new_dst", gc.ipv4_to_bytes(server_ip))],
            )
    
    def test_add_selector_group(self, table_helper):
        """Should add selector group with server members."""
        selector_table = table_helper.get_table("pipe.SwitchIngress.action_selector")
        key = [selector_table.make_key([gc.KeyTuple("$SELECTOR_GROUP_ID", 1)])]
        data = [
            selector_table.make_data(
                [
                    gc.DataTuple("$MAX_GROUP_SIZE", 4),
                    gc.DataTuple("$ACTION_MEMBER_ID", int_arr_val=[0, 1]),
                    gc.DataTuple("$ACTION_MEMBER_STATUS", bool_arr_val=[True, True]),
                ]
            )
        ]
        selector_table.entry_add(table_helper.target, key, data)
    
    def test_add_node_selector_entry(self, table_helper):
        """Should add node selector entry for load balancer VIP."""
        table_helper.insert_entry(
            "pipe.SwitchIngress.node_selector",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(table_helper.LB_IP))],
            None,
            [gc.DataTuple("$SELECTOR_GROUP_ID", 1)],
        )
    
    def test_verify_setup(self, table_helper):
        """Should have correct number of forward entries."""
        entries = table_helper.get_entries("pipe.SwitchIngress.forward")
        assert len(entries) >= 3, f"Expected 3+ forward entries, got {len(entries)}"


# =============================================================================
# Test: Cleanup
# =============================================================================

@pytest.mark.cleanup
class TestCleanup:
    """Tests for table cleanup. Should run last."""
    
    @pytest.mark.parametrize("table_name", [
        "pipe.SwitchIngress.node_selector",
        "pipe.SwitchIngress.action_selector",
        "pipe.SwitchIngress.action_selector_ap",
        "pipe.SwitchIngress.client_snat",
        "pipe.SwitchIngress.forward",
    ])
    def test_clear_table(self, table_helper, table_name):
        """Should clear table entries."""
        try:
            table_helper.clear_table(table_name)
        except Exception as e:
            # Some tables might already be empty - that's OK
            if "OBJECT_NOT_FOUND" not in str(e) and "not found" not in str(e).lower():
                raise


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
