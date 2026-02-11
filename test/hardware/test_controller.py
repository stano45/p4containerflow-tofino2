#!/usr/bin/env python3
"""
Hardware Controller API Tests for P4 Load Balancer

Requirements:
    - Switch running (make switch ARCH=tf1 or ARCH=tf2)
    - Controller running (make controller)

Usage:
    cd test && uv run pytest test_hardware_controller.py -v
    cd test && uv run pytest test_hardware_controller.py -v -k "migrate"
    cd test && uv run pytest test_hardware_controller.py -v --controller-url http://127.0.0.1:5000
"""

import time

import pytest


class TestControllerHealth:

    def test_controller_reachable(self, api_client):
        resp = api_client.get("")
        assert resp is not None, f"Cannot connect to controller at {api_client.base_url}"

    def test_migrate_endpoint_exists(self, api_client):
        resp = api_client.post("migrateNode", data={})
        assert resp is not None, "No response from migrateNode endpoint"
        assert resp.status_code == 400, f"Expected 400 for empty body, got {resp.status_code}"

    def test_migrate_returns_json_error(self, api_client):
        resp = api_client.post("migrateNode", data={})
        assert resp is not None
        data = resp.json()
        assert "error" in data, "Error response should contain 'error' field"

    def test_cleanup_endpoint_responds(self, api_client):
        resp = api_client.get("cleanup")
        assert resp is not None, "No response from cleanup endpoint"
        assert resp.status_code in [200, 405], f"Unexpected status: {resp.status_code}"


class TestPortSetupConfig:
    """Verify that port_setup configuration is valid when present.

    These tests validate the port_setup entries in the controller config
    without needing direct gRPC access. The controller configures ports
    via the BF-RT $PORT table on startup when port_setup is specified.
    """

    def test_port_setup_entries_valid(self, port_setup):
        """Each port_setup entry must have dev_port, speed, and fec."""
        if not port_setup:
            pytest.skip("No port_setup in config (model/simulation mode)")

        for i, entry in enumerate(port_setup):
            assert "dev_port" in entry, f"port_setup[{i}] missing 'dev_port'"
            assert isinstance(entry["dev_port"], int), f"port_setup[{i}].dev_port must be int"
            assert entry["dev_port"] > 0, f"port_setup[{i}].dev_port must be positive"

    def test_port_setup_speeds_valid(self, port_setup):
        """Speed values must be valid BF speed strings."""
        if not port_setup:
            pytest.skip("No port_setup in config (model/simulation mode)")

        valid_speeds = {
            "BF_SPEED_1G", "BF_SPEED_10G", "BF_SPEED_25G",
            "BF_SPEED_40G", "BF_SPEED_50G", "BF_SPEED_100G",
        }
        for i, entry in enumerate(port_setup):
            speed = entry.get("speed", "BF_SPEED_25G")
            assert speed in valid_speeds, (
                f"port_setup[{i}].speed '{speed}' not in {valid_speeds}"
            )

    def test_port_setup_fecs_valid(self, port_setup):
        """FEC values must be valid BF FEC type strings."""
        if not port_setup:
            pytest.skip("No port_setup in config (model/simulation mode)")

        valid_fecs = {
            "BF_FEC_TYP_NONE", "BF_FEC_TYP_FIRECODE", "BF_FEC_TYP_REED_SOLOMON",
        }
        for i, entry in enumerate(port_setup):
            fec = entry.get("fec", "BF_FEC_TYP_REED_SOLOMON")
            assert fec in valid_fecs, (
                f"port_setup[{i}].fec '{fec}' not in {valid_fecs}"
            )

    def test_port_setup_no_duplicates(self, port_setup):
        """No duplicate dev_port values."""
        if not port_setup:
            pytest.skip("No port_setup in config (model/simulation mode)")

        dev_ports = [e["dev_port"] for e in port_setup]
        assert len(dev_ports) == len(set(dev_ports)), (
            f"Duplicate dev_port values in port_setup: {dev_ports}"
        )


class TestMigrateNodeValid:

    def test_migrate_to_new_ip(self, api_client, lb_nodes):
        assert lb_nodes, "FATAL: No LB nodes in controller config - check controller_config.json"

        original_ip = lb_nodes[0]["ipv4"]
        new_ip = "10.0.0.99"

        resp = api_client.migrate_node(original_ip, new_ip)
        assert resp is not None, "No response from controller"
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} - {resp.text}"

        data = resp.json()
        assert data.get("status") == "success"

        restore_resp = api_client.migrate_node(new_ip, original_ip)
        assert restore_resp is not None and restore_resp.status_code == 200, "Failed to restore"

    def test_migrate_back_to_original(self, api_client, lb_nodes):
        assert lb_nodes, "FATAL: No LB nodes in controller config - check controller_config.json"

        original_ip = lb_nodes[0]["ipv4"]
        temp_ip = "10.0.0.97"

        resp1 = api_client.migrate_node(original_ip, temp_ip)
        assert resp1 is not None and resp1.status_code == 200

        resp2 = api_client.migrate_node(temp_ip, original_ip)
        assert resp2 is not None, "No response when migrating back"
        assert resp2.status_code == 200, f"Migration back failed: {resp2.text}"

    def test_multiple_sequential_migrations(self, api_client, lb_nodes):
        assert len(lb_nodes) >= 2, f"FATAL: Need at least 2 LB nodes in config, got {len(lb_nodes)}"

        ip1, ip2 = lb_nodes[0]["ipv4"], lb_nodes[1]["ipv4"]
        temp1, temp2 = "10.0.0.101", "10.0.0.102"

        resp1 = api_client.migrate_node(ip1, temp1)
        resp2 = api_client.migrate_node(ip2, temp2)

        assert resp1 is not None and resp1.status_code == 200
        assert resp2 is not None and resp2.status_code == 200

        api_client.migrate_node(temp1, ip1)
        api_client.migrate_node(temp2, ip2)

    def test_migrate_same_ip(self, api_client, lb_nodes):
        assert lb_nodes, "FATAL: No LB nodes in controller config - check controller_config.json"

        same_ip = lb_nodes[0]["ipv4"]
        resp = api_client.migrate_node(same_ip, same_ip)

        assert resp is not None, "No response for same-IP migration"
        assert resp.status_code == 200, f"Same-IP migration should succeed: {resp.text}"


class TestMigrateNodeInvalid:

    def test_missing_old_ipv4(self, api_client):
        resp = api_client.post("migrateNode", data={"new_ipv4": "10.0.0.99"})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

    def test_missing_new_ipv4(self, api_client):
        resp = api_client.post("migrateNode", data={"old_ipv4": "10.0.0.1"})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

    def test_empty_body(self, api_client):
        resp = api_client.post("migrateNode", data={})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

    def test_non_lb_node_migration(self, api_client, non_lb_nodes):
        assert non_lb_nodes, "FATAL: No non-LB nodes in controller config - check controller_config.json"

        non_lb_ip = non_lb_nodes[0]["ipv4"]
        resp = api_client.migrate_node(non_lb_ip, "10.0.0.99")

        assert resp is not None, "No response"
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"

        data = resp.json()
        assert "error" in data
        assert "not LB node" in data["error"]

    def test_unknown_ip_migration(self, api_client):
        resp = api_client.migrate_node("192.168.255.255", "10.0.0.99")
        assert resp is not None, "No response"
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"

    def test_malformed_json(self, api_client):
        resp = api_client.post("migrateNode", raw_data="{invalid json")
        assert resp is not None, "No response"
        assert resp.status_code in [400, 500], f"Expected 400/500, got {resp.status_code}"

    def test_get_method_not_allowed(self, api_client):
        resp = api_client.get("migrateNode")
        assert resp is not None, "No response"
        assert resp.status_code in [404, 405], f"Expected 404/405, got {resp.status_code}"

    def test_null_parameters(self, api_client):
        resp = api_client.post("migrateNode", data={"old_ipv4": None, "new_ipv4": None})
        assert resp is not None, "No response"
        assert resp.status_code in [400, 500], f"Expected 400/500, got {resp.status_code}"


class TestInvalidEndpoints:

    def test_nonexistent_endpoint(self, api_client):
        resp = api_client.post("nonexistent")
        assert resp is not None, "No response"
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"

    def test_root_endpoint(self, api_client):
        resp = api_client.get("")
        assert resp is not None, "No response from root endpoint"

    def test_random_nested_path(self, api_client):
        resp = api_client.get("api/v1/random/path")
        assert resp is not None, "No response"
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


class TestResponseTimes:

    MAX_RESPONSE_TIME = 2.0

    def test_migrate_response_time(self, api_client, lb_nodes):
        assert lb_nodes, "FATAL: No LB nodes in controller config - check controller_config.json"

        original_ip = lb_nodes[0]["ipv4"]
        temp_ip = "10.0.0.96"

        start = time.time()
        resp = api_client.migrate_node(original_ip, temp_ip)
        elapsed = time.time() - start

        assert resp is not None, "No response from migration endpoint"
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} - {resp.text}"
        assert elapsed < self.MAX_RESPONSE_TIME, f"Response took {elapsed:.3f}s (max {self.MAX_RESPONSE_TIME}s)"

        api_client.migrate_node(temp_ip, original_ip)

    def test_error_response_time(self, api_client):
        start = time.time()
        resp = api_client.post("migrateNode", data={})
        elapsed = time.time() - start

        assert resp is not None, "No response"
        assert elapsed < self.MAX_RESPONSE_TIME, f"Error response took {elapsed:.3f}s"


@pytest.mark.cleanup
class TestCleanup:
    """
    WARNING: These tests clear controller state. Run last or separately:
        pytest test_hardware_controller.py -v -k "not cleanup"
        pytest test_hardware_controller.py -v -k "cleanup"
    """

    def test_cleanup_returns_200(self, api_client):
        resp = api_client.cleanup()
        assert resp is not None, "No response"
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_cleanup_returns_success(self, api_client):
        resp = api_client.cleanup()
        assert resp is not None

        data = resp.json()
        assert data.get("status") == "success"
        assert "message" in data

    def test_cleanup_idempotent(self, api_client):
        for i in range(3):
            resp = api_client.cleanup()
            assert resp is not None, f"No response on cleanup call {i+1}"
            assert resp.status_code == 200, f"Cleanup {i+1} failed: {resp.status_code}"

    def test_get_cleanup_not_allowed(self, api_client):
        resp = api_client.get("cleanup")
        assert resp is not None, "No response"
        assert resp.status_code in [404, 405], f"Expected 404/405, got {resp.status_code}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
