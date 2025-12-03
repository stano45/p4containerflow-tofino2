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

# Configure logger
logger = logging.getLogger("P4RuntimeController")
logger.setLevel(logging.DEBUG)  # Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Optionally log to a file
file_handler = logging.FileHandler("controller.log")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def main(config_file_path):
    try:
        with open(config_file_path, "r") as config_file:
            switch_configs = json.load(config_file)

        switch_controllers = []
        master_config = None

        for config in switch_configs:
            # Master needs to be initialized last,
            # otherwise performing master arbitration update will fail
            if config.get("master", False):
                if master_config is not None:
                    raise Exception(
                        "Multiple master switches specified in the configuration file."
                    )
                master_config = config
                continue

            switch_controller = SwitchController(
                # p4info_file_path=config["p4info_file_path"],
                # bmv2_file_path=config["bmv2_file_path"],
                logger=logger,
                sw_name=config["name"],
                sw_addr=config["addr"],
                sw_id=config["id"],
                client_id=config["client_id"],
                load_balancer_ip=config["load_balancer_ip"],
                # proto_dump_file=config["proto_dump_file"],
                # initial_table_rules_file=config["runtime_file"],
            )
            switch_controllers.append(switch_controller)

        if master_config is None:
            raise Exception("No master switch specifiedin the configuration file.")
        nodes = master_config.get("nodes", None)

        master_controller = SwitchController(
            # p4info_file_path=master_config["p4info_file_path"],
            # bmv2_file_path=master_config["bmv2_file_path"],
            logger=logger,
            sw_name=master_config["name"],
            sw_addr=master_config["addr"],
            sw_id=master_config["id"],
            client_id=master_config["client_id"],
            load_balancer_ip=master_config["load_balancer_ip"],
            service_port=master_config["service_port"],
            # proto_dump_file=master_config["proto_dump_file"],
            # initial_table_rules_file=master_config["runtime_file"],
        )

        global nodeManager
        nodeManager = NodeManager(
            logger=logger, switch_controller=master_controller, initial_nodes=nodes
        )

        # Register cleanup handlers
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


@app.route("/cleanup", methods=["POST"])
def cleanup():
    """Endpoint to manually trigger cleanup of all table entries."""
    global nodeManager
    if nodeManager is None:
        return jsonify({"error": "NodeManager not initialized"}), 500

    try:
        nodeManager.cleanup()
        return jsonify({"status": "success", "message": "Cleanup complete"}), 200
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        return jsonify({"error": str(e)}), 500


def shutdown_handler(signum, frame):
    """Handle shutdown signals by cleaning up table entries."""
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
