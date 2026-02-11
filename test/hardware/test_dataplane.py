#!/usr/bin/env python3
"""
Hardware Dataplane Tests for P4 Load Balancer on Tofino

Requirements:
    1. Switch running (make switch ARCH=tf1 or ARCH=tf2)
    2. Controller NOT running (this test takes ownership of the P4 program)
    3. SDE environment sourced (source ~/setup-open-p4studio.bash)

Usage:
    make test-hardware ARCH=tf1
    cd test && uv run pytest test_hardware_dataplane.py -v -k "TestTableAccess"
"""

import json
import os
import sys
import time

import pytest

try:
    import bfrt_grpc.client as gc
except ImportError:
    pytest.fail(
        "FATAL: bfrt_grpc module not found.\n"
        "The SDE environment must be sourced before running hardware tests.\n"
        "Run: source ~/setup-open-p4studio.bash"
    )


@pytest.fixture(scope="module")
def switch_connection(grpc_addr, program_name):
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

    yield {
        "interface": interface,
        "target": target,
        "bfrt_info": bfrt_info,
        "program_name": program_name,
    }

    # Release the gRPC connection so the controller (or another test suite)
    # can bind to the program afterward.
    try:
        interface.tear_down_stream()
    except Exception:
        pass


@pytest.fixture(scope="module")
def port_table(switch_connection):
    """Get the $PORT fixed table for port configuration tests."""
    bfrt_info = switch_connection["bfrt_info"]
    try:
        return bfrt_info.table_get("$PORT")
    except Exception:
        # $PORT may not be in the P4-bound bfrt_info; try global
        interface = switch_connection["interface"]
        global_info = interface.bfrt_info_get()
        return global_info.table_get("$PORT")


@pytest.fixture(scope="module")
def port_setup_config(config_path):
    """Load port_setup from controller config, if present."""
    with open(config_path, "r") as f:
        configs = json.load(f)
    master = next(c for c in configs if c.get("master", False))
    return master.get("port_setup", [])


@pytest.fixture(scope="module")
def table_helper(switch_connection):
    return TableHelper(switch_connection)


class TableHelper:

    LB_IP = "10.0.0.10"
    CLIENT_IP = "10.0.0.0"
    SERVER_IPS = ["10.0.0.1", "10.0.0.2"]
    CLIENT_PORT = 1
    SERVER_PORTS = [2, 3]
    SERVICE_PORT = 12345

    TABLES = [
        "pipe.SwitchIngress.forward",
        "pipe.SwitchIngress.arp_forward",
        "pipe.SwitchIngress.node_selector",
        "pipe.SwitchIngress.action_selector",
        "pipe.SwitchIngress.action_selector_ap",
        "pipe.SwitchIngress.client_snat",
    ]

    def __init__(self, conn):
        self.target = conn["target"]
        self.bfrt_info = conn["bfrt_info"]

    def get_table(self, table_name):
        return self.bfrt_info.table_get(table_name)

    def insert_entry(self, table_name, key_fields, action_name=None, data_fields=None):
        table = self.get_table(table_name)
        key_list = [table.make_key(key_fields)]
        if data_fields:
            data_list = [table.make_data(data_fields, action_name)]
        else:
            data_list = [table.make_data([], action_name)]
        table.entry_add(self.target, key_list, data_list)

    def delete_entry(self, table_name, key_fields):
        table = self.get_table(table_name)
        key_list = [table.make_key(key_fields)]
        table.entry_del(self.target, key_list)

    def clear_table(self, table_name):
        table = self.get_table(table_name)
        table.entry_del(self.target)

    def get_entries(self, table_name):
        table = self.get_table(table_name)
        resp = table.entry_get(self.target, flags={"from_hw": False})
        return list(resp)

    def clear_all_tables(self):
        for table_name in self.TABLES:
            try:
                self.clear_table(table_name)
            except Exception:
                pass


class TestConnection:

    def test_connection_established(self, switch_connection):
        assert switch_connection["interface"] is not None

    def test_program_bound(self, switch_connection):
        assert switch_connection["bfrt_info"] is not None
        assert switch_connection["target"] is not None


class TestTableAccess:

    @pytest.mark.parametrize("table_name", TableHelper.TABLES)
    def test_table_accessible(self, table_helper, table_name):
        table = table_helper.get_table(table_name)
        assert table is not None, f"Table '{table_name}' not found"


class TestTableWriteRead:

    def test_forward_table_write(self, table_helper):
        test_ip = "192.168.1.1"
        test_port = 5

        table_helper.insert_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", test_port)],
        )

    def test_forward_table_read(self, table_helper):
        entries = table_helper.get_entries("pipe.SwitchIngress.forward")
        assert len(entries) > 0, "No entries found after write"

    def test_forward_table_delete(self, table_helper):
        test_ip = "192.168.1.1"

        table_helper.delete_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(test_ip))],
        )


class TestArpTableWriteRead:

    def test_arp_forward_table_write(self, table_helper):
        test_ip = "192.168.1.1"
        test_port = 5

        table_helper.insert_entry(
            "pipe.SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(test_ip))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", test_port)],
        )

    def test_arp_forward_table_read(self, table_helper):
        entries = table_helper.get_entries("pipe.SwitchIngress.arp_forward")
        assert len(entries) > 0, "No ARP forward entries found after write"

    def test_arp_forward_table_delete(self, table_helper):
        test_ip = "192.168.1.1"

        table_helper.delete_entry(
            "pipe.SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(test_ip))],
        )


class TestLoadBalancerSetup:

    def test_clear_existing_entries(self, table_helper):
        table_helper.clear_all_tables()

    def test_add_forward_entries(self, table_helper):
        table_helper.insert_entry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(table_helper.CLIENT_IP))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", table_helper.CLIENT_PORT)],
        )

        for i, server_ip in enumerate(table_helper.SERVER_IPS):
            table_helper.insert_entry(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(server_ip))],
                "SwitchIngress.set_egress_port",
                [gc.DataTuple("port", table_helper.SERVER_PORTS[i])],
            )

    def test_add_arp_forward_entries(self, table_helper):
        table_helper.insert_entry(
            "pipe.SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(table_helper.CLIENT_IP))],
            "SwitchIngress.set_egress_port",
            [gc.DataTuple("port", table_helper.CLIENT_PORT)],
        )

        for i, server_ip in enumerate(table_helper.SERVER_IPS):
            table_helper.insert_entry(
                "pipe.SwitchIngress.arp_forward",
                [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(server_ip))],
                "SwitchIngress.set_egress_port",
                [gc.DataTuple("port", table_helper.SERVER_PORTS[i])],
            )

    def test_add_client_snat_entry(self, table_helper):
        table_helper.insert_entry(
            "pipe.SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", table_helper.SERVICE_PORT)],
            "SwitchIngress.set_rewrite_src",
            [gc.DataTuple("new_src", gc.ipv4_to_bytes(table_helper.LB_IP))],
        )

    def test_add_action_profile_entries(self, table_helper):
        for i, server_ip in enumerate(table_helper.SERVER_IPS):
            table_helper.insert_entry(
                "pipe.SwitchIngress.action_selector_ap",
                [gc.KeyTuple("$ACTION_MEMBER_ID", i)],
                "SwitchIngress.set_rewrite_dst",
                [gc.DataTuple("new_dst", gc.ipv4_to_bytes(server_ip))],
            )

    def test_add_selector_group(self, table_helper):
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
        table_helper.insert_entry(
            "pipe.SwitchIngress.node_selector",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(table_helper.LB_IP))],
            None,
            [gc.DataTuple("$SELECTOR_GROUP_ID", 1)],
        )

    def test_verify_setup(self, table_helper):
        entries = table_helper.get_entries("pipe.SwitchIngress.forward")
        assert len(entries) >= 3, f"Expected 3+ forward entries, got {len(entries)}"


class TestPortTableAccess:
    """Test that the $PORT BF-RT table is accessible for port configuration."""

    def test_port_table_exists(self, port_table):
        assert port_table is not None, "$PORT table not found"

    def test_port_table_readable(self, port_table, switch_connection):
        target = switch_connection["target"]
        try:
            resp = port_table.entry_get(target, [])
            entries = list(resp)
            # On fresh switchd, may have only internal ports or none at all
            assert isinstance(entries, list)
        except Exception as e:
            pytest.fail(f"Failed to read $PORT table: {e}")


class TestPortConfiguration:
    """Test adding, reading, and removing ports via the $PORT table.

    Uses a test port (D_P 140 by default, matching Wedge100BF cage 2/0).
    Skipped if no port_setup is defined in the controller config.
    """

    # Use a known-good port for testing; can be overridden via config
    TEST_DEV_PORT = 140
    TEST_SPEED = "BF_SPEED_25G"
    TEST_FEC = "BF_FEC_TYP_REED_SOLOMON"

    def test_add_port(self, port_table, switch_connection, port_setup_config):
        if not port_setup_config:
            pytest.skip("No port_setup in config; port config tests skipped")

        target = switch_connection["target"]
        dev_port = port_setup_config[0]["dev_port"]
        speed = port_setup_config[0].get("speed", self.TEST_SPEED)
        fec = port_setup_config[0].get("fec", self.TEST_FEC)

        try:
            port_table.entry_add(
                target,
                [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                [port_table.make_data([
                    gc.DataTuple("$SPEED", str_val=speed),
                    gc.DataTuple("$FEC", str_val=fec),
                    gc.DataTuple("$PORT_ENABLE", bool_val=True),
                ])],
            )
        except Exception:
            # Port may already exist; try modify
            port_table.entry_mod(
                target,
                [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                [port_table.make_data([
                    gc.DataTuple("$SPEED", str_val=speed),
                    gc.DataTuple("$FEC", str_val=fec),
                    gc.DataTuple("$PORT_ENABLE", bool_val=True),
                ])],
            )

    def test_read_port(self, port_table, switch_connection, port_setup_config):
        if not port_setup_config:
            pytest.skip("No port_setup in config; port config tests skipped")

        target = switch_connection["target"]
        dev_port = port_setup_config[0]["dev_port"]

        resp = port_table.entry_get(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
        )
        entries = list(resp)
        assert len(entries) > 0, f"Port D_P={dev_port} not found after add"

        data, key = entries[0]
        data_dict = data.to_dict()
        assert data_dict["$PORT_ENABLE"] is True, "Port should be enabled"

    def test_add_all_config_ports(self, port_table, switch_connection, port_setup_config):
        """Add all ports from port_setup config and verify they come up."""
        if not port_setup_config:
            pytest.skip("No port_setup in config; port config tests skipped")

        target = switch_connection["target"]

        for entry in port_setup_config:
            dev_port = entry["dev_port"]
            speed = entry.get("speed", self.TEST_SPEED)
            fec = entry.get("fec", self.TEST_FEC)

            try:
                port_table.entry_add(
                    target,
                    [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                    [port_table.make_data([
                        gc.DataTuple("$SPEED", str_val=speed),
                        gc.DataTuple("$FEC", str_val=fec),
                        gc.DataTuple("$PORT_ENABLE", bool_val=True),
                    ])],
                )
            except Exception:
                port_table.entry_mod(
                    target,
                    [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                    [port_table.make_data([
                        gc.DataTuple("$SPEED", str_val=speed),
                        gc.DataTuple("$FEC", str_val=fec),
                        gc.DataTuple("$PORT_ENABLE", bool_val=True),
                    ])],
                )

        # Wait briefly for ports to initialize
        time.sleep(2)

        # Verify all ports are present
        for entry in port_setup_config:
            dev_port = entry["dev_port"]
            resp = port_table.entry_get(
                target,
                [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
            )
            entries = list(resp)
            assert len(entries) > 0, f"Port D_P={dev_port} not found"

    def test_disable_port(self, port_table, switch_connection, port_setup_config):
        if not port_setup_config:
            pytest.skip("No port_setup in config; port config tests skipped")

        target = switch_connection["target"]
        dev_port = port_setup_config[0]["dev_port"]

        port_table.entry_mod(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
            [port_table.make_data([
                gc.DataTuple("$PORT_ENABLE", bool_val=False),
            ])],
        )

        resp = port_table.entry_get(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
        )
        entries = list(resp)
        data, _ = entries[0]
        data_dict = data.to_dict()
        assert data_dict["$PORT_ENABLE"] is False, "Port should be disabled"

        # Re-enable for subsequent tests
        port_table.entry_mod(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
            [port_table.make_data([
                gc.DataTuple("$PORT_ENABLE", bool_val=True),
            ])],
        )

    def test_delete_port(self, port_table, switch_connection, port_setup_config):
        if not port_setup_config:
            pytest.skip("No port_setup in config; port config tests skipped")

        target = switch_connection["target"]
        dev_port = port_setup_config[0]["dev_port"]

        port_table.entry_del(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
        )

        # Re-add for subsequent tests
        speed = port_setup_config[0].get("speed", self.TEST_SPEED)
        fec = port_setup_config[0].get("fec", self.TEST_FEC)
        port_table.entry_add(
            target,
            [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
            [port_table.make_data([
                gc.DataTuple("$SPEED", str_val=speed),
                gc.DataTuple("$FEC", str_val=fec),
                gc.DataTuple("$PORT_ENABLE", bool_val=True),
            ])],
        )


@pytest.mark.cleanup
class TestCleanup:

    @pytest.mark.parametrize("table_name", [
        "pipe.SwitchIngress.node_selector",
        "pipe.SwitchIngress.action_selector",
        "pipe.SwitchIngress.action_selector_ap",
        "pipe.SwitchIngress.client_snat",
        "pipe.SwitchIngress.forward",
        "pipe.SwitchIngress.arp_forward",
    ])
    def test_clear_table(self, table_helper, table_name):
        try:
            table_helper.clear_table(table_name)
        except Exception as e:
            if "OBJECT_NOT_FOUND" not in str(e) and "not found" not in str(e).lower():
                raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
