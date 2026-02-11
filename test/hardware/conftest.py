"""
Pytest configuration and fixtures for hardware tests.
"""

import json
import os

import pytest
import requests


def pytest_addoption(parser):
    parser.addoption(
        "--controller-url",
        action="store",
        default="http://127.0.0.1:5000",
        help="Controller HTTP API URL",
    )
    parser.addoption(
        "--config",
        action="store",
        default=os.path.join(os.path.dirname(__file__), "..", "..", "controller", "controller_config.json"),
        help="Path to controller config file",
    )
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


@pytest.fixture(scope="session")
def config_path(request):
    return request.config.getoption("--config")


@pytest.fixture(scope="session")
def controller_config(config_path):
    with open(config_path, "r") as f:
        configs = json.load(f)
    master_config = next(c for c in configs if c.get("master", False))
    return master_config


@pytest.fixture(scope="session")
def lb_nodes(controller_config):
    nodes = controller_config.get("nodes", [])
    return [n for n in nodes if n.get("is_lb_node", False)]


@pytest.fixture(scope="session")
def non_lb_nodes(controller_config):
    nodes = controller_config.get("nodes", [])
    return [n for n in nodes if not n.get("is_lb_node", False)]


@pytest.fixture(scope="session")
def port_setup(controller_config):
    """Return port_setup from controller config, or empty list if not present."""
    return controller_config.get("port_setup", [])


class APIClient:

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, endpoint: str, data: dict = None, raw_data: str = None):
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
        url = f"{self.base_url}/{endpoint}"
        try:
            return requests.get(url, timeout=self.timeout)
        except requests.exceptions.RequestException:
            return None

    def migrate_node(self, old_ipv4: str, new_ipv4: str):
        return self.post("migrateNode", data={"old_ipv4": old_ipv4, "new_ipv4": new_ipv4})

    def cleanup(self):
        return self.post("cleanup")

    def reinitialize(self):
        return self.post("reinitialize")


@pytest.fixture(scope="session")
def api_client(request):
    url = request.config.getoption("--controller-url")
    client = APIClient(url)
    # Reinitialize controller state before the test session to guarantee
    # a fresh, known-good state regardless of previous test runs.
    resp = client.reinitialize()
    if resp is None:
        pytest.skip("Controller not reachable; cannot reinitialize")
    if resp.status_code != 200:
        pytest.skip(f"Controller reinitialize failed: {resp.status_code} {resp.text}")
    return client


@pytest.fixture(scope="session")
def arch(request):
    return request.config.getoption("--arch")


@pytest.fixture(scope="session")
def grpc_addr(request):
    return request.config.getoption("--grpc-addr")


@pytest.fixture(scope="session")
def program_name(arch):
    return "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
