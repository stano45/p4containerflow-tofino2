"""
Pytest configuration and fixtures for P4 Load Balancer tests.

This file is only used by pytest, not PTF.
"""

import json
import os

try:
    import pytest
    import requests
    PYTEST_AVAILABLE = True
except ImportError:
    PYTEST_AVAILABLE = False

if PYTEST_AVAILABLE:

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
            default=os.path.join(os.path.dirname(__file__), "..", "controller", "controller_config.json"),
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

    @pytest.fixture(scope="module")
    def controller_url(request):
        return request.config.getoption("--controller-url")

    @pytest.fixture(scope="module")
    def config_path(request):
        return request.config.getoption("--config")

    @pytest.fixture(scope="module")
    def controller_config(config_path):
        with open(config_path, "r") as f:
            configs = json.load(f)
        master_config = next(c for c in configs if c.get("master", False))
        return master_config

    @pytest.fixture(scope="module")
    def lb_nodes(controller_config):
        nodes = controller_config.get("nodes", [])
        return [n for n in nodes if n.get("is_lb_node", False)]

    @pytest.fixture(scope="module")
    def non_lb_nodes(controller_config):
        nodes = controller_config.get("nodes", [])
        return [n for n in nodes if not n.get("is_lb_node", False)]

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

    @pytest.fixture(scope="module")
    def api_client(controller_url):
        return APIClient(controller_url)

    @pytest.fixture(scope="module")
    def arch(request):
        return request.config.getoption("--arch")

    @pytest.fixture(scope="module")
    def grpc_addr(request):
        return request.config.getoption("--grpc-addr")

    @pytest.fixture(scope="module")
    def program_name(arch):
        return "tna_load_balancer" if arch == "tf1" else "t2na_load_balancer"
