#!/usr/bin/env python3
"""
Hardware Controller API Tests for P4 Load Balancer

This module tests the controller's HTTP API. It does NOT connect directly 
to the switch - it only tests the controller via its REST API endpoints.

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


# =============================================================================
# Test: Controller Health/Reachability
# =============================================================================

class TestControllerHealth:
    """Tests for controller health and reachability."""
    
    def test_controller_reachable(self, api_client):
        """Controller should be reachable via HTTP."""
        resp = api_client.get("")
        assert resp is not None, f"Cannot connect to controller at {api_client.base_url}"
        # Any response proves connectivity (even 404)
    
    def test_migrate_endpoint_exists(self, api_client):
        """migrateNode endpoint should exist and return 400 for empty body."""
        resp = api_client.post("migrateNode", data={})
        assert resp is not None, "No response from migrateNode endpoint"
        assert resp.status_code == 400, f"Expected 400 for empty body, got {resp.status_code}"
    
    def test_migrate_returns_json_error(self, api_client):
        """migrateNode should return JSON error response."""
        resp = api_client.post("migrateNode", data={})
        assert resp is not None
        data = resp.json()
        assert "error" in data, "Error response should contain 'error' field"
    
    def test_cleanup_endpoint_responds(self, api_client):
        """cleanup endpoint should respond to GET (to check it exists without clearing state)."""
        # Use GET which returns 405 but proves endpoint exists
        resp = api_client.get("cleanup")
        assert resp is not None, "No response from cleanup endpoint"
        assert resp.status_code in [200, 405], f"Unexpected status: {resp.status_code}"


# =============================================================================
# Test: migrateNode - Valid Requests
# =============================================================================

class TestMigrateNodeValid:
    """Tests for valid node migration requests."""
    
    def test_migrate_to_new_ip(self, api_client, lb_nodes):
        """Should successfully migrate LB node to new IP."""
        if not lb_nodes:
            pytest.skip("No LB nodes in config")
        
        original_ip = lb_nodes[0]["ipv4"]
        new_ip = "10.0.0.99"
        
        resp = api_client.migrate_node(original_ip, new_ip)
        assert resp is not None, "No response from controller"
        assert resp.status_code == 200, f"Migration failed: {resp.status_code} - {resp.text}"
        
        data = resp.json()
        assert data.get("status") == "success"
        
        # Migrate back to restore state
        restore_resp = api_client.migrate_node(new_ip, original_ip)
        assert restore_resp is not None and restore_resp.status_code == 200, "Failed to restore"
    
    def test_migrate_back_to_original(self, api_client, lb_nodes):
        """Should successfully migrate node back to original IP."""
        if not lb_nodes:
            pytest.skip("No LB nodes in config")
        
        original_ip = lb_nodes[0]["ipv4"]
        temp_ip = "10.0.0.97"
        
        # Migrate to temp
        resp1 = api_client.migrate_node(original_ip, temp_ip)
        assert resp1 is not None and resp1.status_code == 200
        
        # Migrate back
        resp2 = api_client.migrate_node(temp_ip, original_ip)
        assert resp2 is not None, "No response when migrating back"
        assert resp2.status_code == 200, f"Migration back failed: {resp2.text}"
    
    def test_multiple_sequential_migrations(self, api_client, lb_nodes):
        """Should handle multiple sequential migrations."""
        if len(lb_nodes) < 2:
            pytest.skip("Need at least 2 LB nodes")
        
        ip1, ip2 = lb_nodes[0]["ipv4"], lb_nodes[1]["ipv4"]
        temp1, temp2 = "10.0.0.101", "10.0.0.102"
        
        # Migrate both nodes
        resp1 = api_client.migrate_node(ip1, temp1)
        resp2 = api_client.migrate_node(ip2, temp2)
        
        assert resp1 is not None and resp1.status_code == 200
        assert resp2 is not None and resp2.status_code == 200
        
        # Restore both
        api_client.migrate_node(temp1, ip1)
        api_client.migrate_node(temp2, ip2)
    
    def test_migrate_same_ip(self, api_client, lb_nodes):
        """Migrating to same IP should succeed as no-op."""
        if not lb_nodes:
            pytest.skip("No LB nodes in config")
        
        same_ip = lb_nodes[0]["ipv4"]
        resp = api_client.migrate_node(same_ip, same_ip)
        
        assert resp is not None, "No response for same-IP migration"
        assert resp.status_code == 200, f"Same-IP migration should succeed: {resp.text}"


# =============================================================================
# Test: migrateNode - Invalid Requests
# =============================================================================

class TestMigrateNodeInvalid:
    """Tests for invalid node migration requests."""
    
    def test_missing_old_ipv4(self, api_client):
        """Should return 400 when old_ipv4 is missing."""
        resp = api_client.post("migrateNode", data={"new_ipv4": "10.0.0.99"})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    
    def test_missing_new_ipv4(self, api_client):
        """Should return 400 when new_ipv4 is missing."""
        resp = api_client.post("migrateNode", data={"old_ipv4": "10.0.0.1"})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    
    def test_empty_body(self, api_client):
        """Should return 400 for empty request body."""
        resp = api_client.post("migrateNode", data={})
        assert resp is not None, "No response"
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    
    def test_non_lb_node_migration(self, api_client, non_lb_nodes):
        """Should return 500 when trying to migrate non-LB node."""
        if not non_lb_nodes:
            pytest.skip("No non-LB nodes in config")
        
        non_lb_ip = non_lb_nodes[0]["ipv4"]
        resp = api_client.migrate_node(non_lb_ip, "10.0.0.99")
        
        assert resp is not None, "No response"
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"
        
        data = resp.json()
        assert "error" in data
        assert "not LB node" in data["error"]
    
    def test_unknown_ip_migration(self, api_client):
        """Should return 500 for completely unknown IP."""
        resp = api_client.migrate_node("192.168.255.255", "10.0.0.99")
        assert resp is not None, "No response"
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"
    
    def test_malformed_json(self, api_client):
        """Should return 400 or 500 for malformed JSON."""
        resp = api_client.post("migrateNode", raw_data="{invalid json")
        assert resp is not None, "No response"
        assert resp.status_code in [400, 500], f"Expected 400/500, got {resp.status_code}"
    
    def test_get_method_not_allowed(self, api_client):
        """GET on migrateNode should return 405 Method Not Allowed."""
        resp = api_client.get("migrateNode")
        assert resp is not None, "No response"
        assert resp.status_code in [404, 405], f"Expected 404/405, got {resp.status_code}"
    
    def test_null_parameters(self, api_client):
        """Should return 400 for null parameter values."""
        resp = api_client.post("migrateNode", data={"old_ipv4": None, "new_ipv4": None})
        assert resp is not None, "No response"
        assert resp.status_code in [400, 500], f"Expected 400/500, got {resp.status_code}"


# =============================================================================
# Test: Invalid Endpoints
# =============================================================================

class TestInvalidEndpoints:
    """Tests for non-existent endpoints."""
    
    def test_nonexistent_endpoint(self, api_client):
        """Non-existent endpoint should return 404."""
        resp = api_client.post("nonexistent")
        assert resp is not None, "No response"
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
    
    def test_root_endpoint(self, api_client):
        """Root endpoint should return some response."""
        resp = api_client.get("")
        assert resp is not None, "No response from root endpoint"
    
    def test_random_nested_path(self, api_client):
        """Random nested path should return 404."""
        resp = api_client.get("api/v1/random/path")
        assert resp is not None, "No response"
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"


# =============================================================================
# Test: Response Times
# =============================================================================

class TestResponseTimes:
    """Tests for API response times."""
    
    MAX_RESPONSE_TIME = 2.0  # seconds
    
    def test_migrate_response_time(self, api_client, lb_nodes):
        """migrateNode should respond within acceptable time."""
        if not lb_nodes:
            pytest.skip("No LB nodes in config")
        
        original_ip = lb_nodes[0]["ipv4"]
        temp_ip = "10.0.0.96"
        
        start = time.time()
        resp = api_client.migrate_node(original_ip, temp_ip)
        elapsed = time.time() - start
        
        if resp and resp.status_code == 200:
            assert elapsed < self.MAX_RESPONSE_TIME, f"Response took {elapsed:.3f}s"
            # Restore
            api_client.migrate_node(temp_ip, original_ip)
        else:
            pytest.skip("Migration failed, cannot test response time")
    
    def test_error_response_time(self, api_client):
        """Error responses should be fast."""
        start = time.time()
        resp = api_client.post("migrateNode", data={})
        elapsed = time.time() - start
        
        assert resp is not None, "No response"
        assert elapsed < self.MAX_RESPONSE_TIME, f"Error response took {elapsed:.3f}s"


# =============================================================================
# Test: Cleanup Endpoint (runs last - clears state)
# =============================================================================

@pytest.mark.cleanup
class TestCleanup:
    """Tests for cleanup endpoint. 
    
    WARNING: These tests clear controller state. Run last or separately:
        pytest test_hardware_controller.py -v -k "not cleanup"  # skip cleanup
        pytest test_hardware_controller.py -v -k "cleanup"      # only cleanup
    """
    
    def test_cleanup_returns_200(self, api_client):
        """cleanup should return 200."""
        resp = api_client.cleanup()
        assert resp is not None, "No response"
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    
    def test_cleanup_returns_success(self, api_client):
        """cleanup should return success status in JSON."""
        resp = api_client.cleanup()
        assert resp is not None
        
        data = resp.json()
        assert data.get("status") == "success"
        assert "message" in data
    
    def test_cleanup_idempotent(self, api_client):
        """Multiple cleanup calls should all succeed (idempotent)."""
        for i in range(3):
            resp = api_client.cleanup()
            assert resp is not None, f"No response on cleanup call {i+1}"
            assert resp.status_code == 200, f"Cleanup {i+1} failed: {resp.status_code}"
    
    def test_get_cleanup_not_allowed(self, api_client):
        """GET on cleanup should return 405 Method Not Allowed."""
        resp = api_client.get("cleanup")
        assert resp is not None, "No response"
        assert resp.status_code in [404, 405], f"Expected 404/405, got {resp.status_code}"


# =============================================================================
# Entry point for direct execution
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
