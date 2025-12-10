"""
Pytest configuration and fixtures for P4 Load Balancer tests.

This file provides shared fixtures for:
- Controller API tests (test_hardware_controller.py)
- Hardware dataplane tests (test_hardware_dataplane.py)
"""

import json
import os

import pytest
import requests


# =============================================================================
# Command Line Options
# =============================================================================

def pytest_addoption(parser):
    """Add custom command line options for all tests."""
    # Controller API test options
    parser.addoption(
        "--controller-url",
        action="store",
        default="http://127.0.0.1:5000",
        help="Controller HTTP API URL",
    )
    parser.addoption(
        "--config",
        action="store",
        default=os.path.join(os.path.dirname(__file__), "..", "controller", "controller_config.json"),
        help="Path to controller config file",
    )
    
    # Hardware dataplane test options
    parser.addoption(
        "--arch",
        action="store",
        default=os.environ.get("ARCH", "tf2"),
        choices=["tf1", "tf2"],
        help="Tofino architecture (default: tf2 or from ARCH env var)",
    )
    parser.addoption(
        "--grpc-addr",
        action="store",
        default="127.0.0.1:50052",
        help="gRPC address of the switch (default: 127.0.0.1:50052)",
    )


# =============================================================================
# Controller API Fixtures
# =============================================================================

@pytest.fixture(scope="module")
def controller_url(request):
    """Get controller URL from command line or default."""
    return request.config.getoption("--controller-url")


@pytest.fixture(scope="module")
def config_path(request):
    """Get config path from command line or default."""
    return request.config.getoption("--config")


@pytest.fixture(scope="module")
def controller_config(config_path):
    """Load and parse controller configuration."""
    with open(config_path, "r") as f:
        configs = json.load(f)
    # Find the master switch config
    master_config = next(c for c in configs if c.get("master", False))
    return master_config


@pytest.fixture(scope="module")
def lb_nodes(controller_config):
    """Get list of load balancer nodes from config."""
    nodes = controller_config.get("nodes", [])
    return [n for n in nodes if n.get("is_lb_node", False)]


@pytest.fixture(scope="module")
def non_lb_nodes(controller_config):
    """Get list of non-LB nodes from config."""
    nodes = controller_config.get("nodes", [])
    return [n for n in nodes if not n.get("is_lb_node", False)]


class APIClient:
    """Simple HTTP client for controller API."""
    
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
    
    def post(self, endpoint: str, data: dict = None, raw_data: str = None):
        """Make a POST request."""
        url = f"{self.base_url}/{endpoint}"
        headers = {"Content-Type": "application/json"}
        try:
            if raw_data is not None:
                return requests.post(url, data=raw_data, headers=headers, timeout=self.timeout)
            elif data is not None:
                return requests.post(url, json=data, timeout=self.timeout)
            else:
                return requests.post(url, timeout=self.timeout)
        except requests.exceptions.RequestException:
            return None
    
    def get(self, endpoint: str):
        """Make a GET request."""
        url = f"{self.base_url}/{endpoint}"
        try:
            return requests.get(url, timeout=self.timeout)
        except requests.exceptions.RequestException:
            return None
    
    def migrate_node(self, old_ipv4: str, new_ipv4: str):
        """Call migrateNode endpoint."""
        return self.post("migrateNode", data={"old_ipv4": old_ipv4, "new_ipv4": new_ipv4})
    
    def cleanup(self):
        """Call cleanup endpoint."""
        return self.post("cleanup")


@pytest.fixture(scope="module")
def api_client(controller_url):
    """Create an API client for making requests."""
    return APIClient(controller_url)


# =============================================================================
# Hardware Dataplane Fixtures
# =============================================================================

@pytest.fixture(scope="module")
def arch(request):
    """Get architecture from command line."""
    return request.config.getoption("--arch")


@pytest.fixture(scope="module")
def grpc_addr(request):
    """Get gRPC address from command line."""
    return request.config.getoption("--grpc-addr")


@pytest.fixture(scope="module")
def program_name(arch):
    """Get P4 program name based on architecture."""
    return "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
