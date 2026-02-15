#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import signal
import sys
import logging

import grpc
from flask import Flask, jsonify, request
from node_manager import NodeManager
from bf_switch_controller import SwitchController
from utils import printGrpcError

app = Flask(__name__)

nodeManager = None

logger = logging.getLogger("P4RuntimeController")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
file_handler = logging.FileHandler("controller.log")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def main(config_file_path):
    try:
        with open(config_file_path, "r") as config_file:
            switch_configs = json.load(config_file)

        arch = os.environ.get("ARCH", "tf2")
        if arch == "tf1":
            program_name = "tna_load_balancer"
        else:
            program_name = "t2na_load_balancer"
        logger.info(f"Using program name: {program_name} (ARCH={arch})")

        switch_controllers = []
        master_config = None

        for config in switch_configs:
            if config.get("master", False):
                if master_config is not None:
                    raise Exception(
                        "Multiple master switches specified in the configuration file."
                    )
                master_config = config
                continue

            switch_controller = SwitchController(
                logger=logger,
                sw_name=program_name,
                sw_addr=config["addr"],
                sw_id=config["id"],
                client_id=config["client_id"],
                load_balancer_ip=config["load_balancer_ip"],
            )
            switch_controllers.append(switch_controller)

        if master_config is None:
            raise Exception("No master switch specified in the configuration file.")
        nodes = master_config.get("nodes", None)

        master_controller = SwitchController(
            logger=logger,
            sw_name=program_name,
            sw_addr=master_config["addr"],
            sw_id=master_config["id"],
            client_id=master_config["client_id"],
            load_balancer_ip=master_config["load_balancer_ip"],
            service_port=master_config["service_port"],
        )

        port_setup = master_config.get("port_setup", [])
        if port_setup:
            master_controller.setup_ports(port_setup)

        global nodeManager
        nodeManager = NodeManager(
            logger=logger, switch_controller=master_controller, initial_nodes=nodes
        )

        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)
        atexit.register(lambda: nodeManager.cleanup() if nodeManager else None)

    except KeyboardInterrupt:
        print("Shutting down.")
        if nodeManager:
            nodeManager.cleanup()
    except grpc.RpcError as e:
        printGrpcError(e)
        exit(1)
    except Exception as e:
        print(f"Error: {e}")
        exit(1)

    app.run(port=5000)


@app.route("/migrateNode", methods=["POST"])
def update_node():
    data = request.get_json()
    old_ipv4 = data.get("old_ipv4")
    new_ipv4 = data.get("new_ipv4")

    if not all([old_ipv4, new_ipv4]):
        logger.error(
            f"Failed to update node {old_ipv4} with {new_ipv4}: Missing parameters"
        )
        return jsonify({"error": "Missing parameters"}), 400

    try:
        nodeManager.migrateNode(old_ipv4, new_ipv4)
        logger.info(f"Successfully updated node {old_ipv4} with {new_ipv4}")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Failed to update node {old_ipv4} with {new_ipv4}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/updateForward", methods=["POST"])
def update_forward():
    """Update forward + arp_forward table entries for same-IP migration.

    Expects JSON: {"ipv4": "192.168.12.2", "sw_port": 148}
    """
    data = request.get_json()
    ipv4 = data.get("ipv4")
    sw_port = data.get("sw_port")

    if not all([ipv4, sw_port]):
        logger.error(f"updateForward: missing parameters (ipv4={ipv4}, sw_port={sw_port})")
        return jsonify({"error": "Missing parameters: ipv4 and sw_port required"}), 400

    try:
        nodeManager.updateForward(ipv4, int(sw_port))
        logger.info(f"Successfully updated forward entries: {ipv4} -> port {sw_port}")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Failed to update forward entries: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/cleanup", methods=["POST"])
def cleanup():
    global nodeManager
    if nodeManager is None:
        return jsonify({"error": "NodeManager not initialized"}), 500

    try:
        nodeManager.cleanup()
        return jsonify({"status": "success", "message": "Cleanup complete"}), 200
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/reinitialize", methods=["POST"])
def reinitialize():
    """Clean up all table entries and re-insert from original config."""
    global nodeManager
    if nodeManager is None:
        return jsonify({"error": "NodeManager not initialized"}), 500

    try:
        nodeManager.reinitialize()
        return jsonify({"status": "success", "message": "Reinitialization complete"}), 200
    except Exception as e:
        logger.error(f"Reinitialize failed: {e}")
        return jsonify({"error": str(e)}), 500


def shutdown_handler(signum, frame):
    global nodeManager
    logger.info(f"Received signal {signum}, initiating cleanup...")
    if nodeManager is not None:
        try:
            nodeManager.cleanup()
        except Exception as e:
            logger.error(f"Cleanup during shutdown failed: {e}")
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P4Runtime Controller")
    parser.add_argument(
        "--config",
        help="JSON configuration file for switches",
        type=str,
        action="store",
        required=True,
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        parser.print_help()
        print(f"\nConfiguration file not found: {args.config}")
        parser.exit(1)
    main(args.config)
