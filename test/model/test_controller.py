import json
import os
import random
from bfruntime_client_base_tests import BfRuntimeTest
from p4testutils.misc_utils import get_logger, get_sw_ports, simple_tcp_packet
from ptf.testutils import (
    send_packet,
    verify_packet,
    verify_no_other_packets,
    verify_any_packet_any_port,
)
import requests
import ptf

logger = get_logger()
swports = get_sw_ports()

print("SW Ports: ", swports)

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "controller", "controller_config.json"
)
with open(CONFIG_PATH, "r") as f:
    _configs = json.load(f)
    MASTER_CONFIG = next(c for c in _configs if c.get("master", False))


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        self.dataplane = ptf.dataplane_instance
        self.dataplane.flush()

    def tearDown(self):
        pass

    def sendAndVerifyPacketAnyPort(
        self, send_port, send_pkt, expected_pkts, verify_ports
    ):
        send_packet(self, send_port, send_pkt)
        rcv_idx = verify_any_packet_any_port(self, expected_pkts, verify_ports)
        return rcv_idx

    def migrate_node(self, old_ipv4, new_ipv4):
        url = "http://127.0.0.1:5000/migrateNode"
        headers = {"Content-Type": "application/json"}
        data = {
            "old_ipv4": old_ipv4,
            "new_ipv4": new_ipv4,
        }

        response = requests.post(url, headers=headers, json=data)
        return response

    def checkTrafficBalance(self, counter1, counter2, max_imbalance_percent=20):
        total_packets = counter1 + counter2
        if total_packets == 0:
            imbalance_percentage = 0
        else:
            imbalance_percentage = abs(counter1 - counter2) / total_packets * 100

        logger.info(
            "Traffic distribution: Server 1: %d packets, Server 2: %d packets, Imbalance: %.2f%%",
            counter1,
            counter2,
            imbalance_percentage,
        )
        assert imbalance_percentage <= max_imbalance_percent, (
            f"Traffic imbalance too high: {imbalance_percentage:.2f}%"
        )

    def verifyNoOtherPackets(self):
        verify_no_other_packets(self, 0, timeout=2)

    def runTestImpl(self):
        self.setupCtrlPlane()
        logger.info("Sending Packet ...")
        self.sendPacket()
        logger.info("Verifying Packet ...")
        self.verifyPackets()
        logger.info("Verifying no other packets ...")
        self.verifyNoOtherPackets()
        logger.info("Tearing down suite...")
        self.tearDown()
        logger.info("Done!")

    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass


class TestPortSetupConfigModel(AbstractTest):
    """Validate port_setup handling in the controller config for model mode.

    In model/simulation mode, port_setup is typically absent from the config.
    If present, the controller calls setup_ports() on startup. This test
    validates the config structure is correct either way.
    """

    def setUp(self):
        super().setUp()
        self.port_setup = MASTER_CONFIG.get("port_setup", [])

    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass

    def runTest(self):
        # For model tests, port_setup is typically empty
        logger.info("port_setup entries in config: %d", len(self.port_setup))

        if not self.port_setup:
            logger.info("No port_setup in config (expected for model/simulation mode)")
            return

        # If port_setup is present, validate its structure
        valid_speeds = {
            "BF_SPEED_1G", "BF_SPEED_10G", "BF_SPEED_25G",
            "BF_SPEED_40G", "BF_SPEED_50G", "BF_SPEED_100G",
        }
        valid_fecs = {
            "BF_FEC_TYP_NONE", "BF_FEC_TYP_FIRECODE", "BF_FEC_TYP_REED_SOLOMON",
        }

        dev_ports_seen = set()
        for i, entry in enumerate(self.port_setup):
            assert "dev_port" in entry, f"port_setup[{i}] missing 'dev_port'"
            assert isinstance(entry["dev_port"], int), f"port_setup[{i}].dev_port must be int"
            assert entry["dev_port"] > 0, f"port_setup[{i}].dev_port must be positive"
            assert entry["dev_port"] not in dev_ports_seen, (
                f"Duplicate dev_port {entry['dev_port']} in port_setup"
            )
            dev_ports_seen.add(entry["dev_port"])

            speed = entry.get("speed", "BF_SPEED_25G")
            assert speed in valid_speeds, f"port_setup[{i}].speed '{speed}' invalid"

            fec = entry.get("fec", "BF_FEC_TYP_REED_SOLOMON")
            assert fec in valid_fecs, f"port_setup[{i}].fec '{fec}' invalid"

        logger.info("All %d port_setup entries are valid", len(self.port_setup))


class TestController(AbstractTest):

    def setUp(self):
        super().setUp()

        nodes = MASTER_CONFIG["nodes"]
        self.loadBalancerIp = MASTER_CONFIG["load_balancer_ip"]

        self.nodesByIp = {node["ipv4"]: node for node in nodes}

        self.lbNodes = [n for n in nodes if n.get("is_lb_node", False)]
        clientNodes = [n for n in nodes if not n.get("is_lb_node", False)]

        self.clientIp = clientNodes[0]["ipv4"]
        self.clientPort = clientNodes[0]["sw_port"]

        self.lbNodeIps = [n["ipv4"] for n in self.lbNodes]
        self.lbNodePorts = [n["sw_port"] for n in self.lbNodes]

        self.servicePort = MASTER_CONFIG["service_port"]

        self.numPackets = 300
        self.maxImbalancePercent = 20
        self.server1Counter = 0
        self.server2Counter = 0

        logger.info(
            "Loaded config: client=%s (port %d), LB nodes=%s (ports %s), VIP=%s, service_port=%d",
            self.clientIp,
            self.clientPort,
            self.lbNodeIps,
            self.lbNodePorts,
            self.loadBalancerIp,
            self.servicePort,
        )

    def tearDown(self):
        logger.info("Cleaning up: calling controller cleanup endpoint...")
        try:
            url = "http://127.0.0.1:5000/cleanup"
            resp = requests.post(url)
            if resp and resp.status_code == 200:
                logger.info("Controller cleanup successful")
            else:
                logger.warning(
                    f"Controller cleanup failed: {resp.status_code if resp else 'no response'}"
                )
        except Exception as e:
            logger.warning(f"Error calling cleanup endpoint: {e}")
        super().tearDown()

    def setupCtrlPlane(self):
        logger.info(
            "Setting up test: Client port: %s, LB node ports: %s",
            self.clientPort,
            self.lbNodePorts,
        )
        logger.info("Nodes should be pre-configured via controller config file")

    def sendPackets(self, num_packets, server_ips, server_ports):
        for i in range(num_packets):
            logger.info("Sending packet %d...", i)
            clientTcpPort = random.randint(1024, 65535)

            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.loadBalancerIp,
                tcp_sport=clientTcpPort,
                tcp_dport=self.servicePort,
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=server_ips[0],
                tcp_sport=clientTcpPort,
                tcp_dport=self.servicePort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=server_ips[1],
                tcp_sport=clientTcpPort,
                tcp_dport=self.servicePort,
            )

            logger.info("Verifying packet %d...", i)
            rcvIdx = self.sendAndVerifyPacketAnyPort(
                self.clientPort,
                clientPkt,
                [expectedPktToServer1, expectedPktToServer2],
                server_ports,
            )
            logger.info(
                "Server packet %d received on port %d!", i, server_ports[rcvIdx]
            )
            if rcvIdx == 0:
                self.server1Counter += 1
            else:
                self.server2Counter += 1

            logger.info(
                "Sending response from port %d to client port %d...",
                server_ports[rcvIdx],
                self.clientPort,
            )

            serverIp = server_ips[rcvIdx]
            serverPkt = simple_tcp_packet(
                ip_src=serverIp,
                ip_dst=self.clientIp,
                tcp_sport=self.servicePort,
                tcp_dport=clientTcpPort,
            )
            send_packet(self, server_ports[rcvIdx], serverPkt)

            expectedPktToClient = simple_tcp_packet(
                ip_src=self.loadBalancerIp,
                ip_dst=self.clientIp,
                tcp_sport=self.servicePort,
                tcp_dport=clientTcpPort,
            )
            logger.info("Verifying packet on client port %d...", self.clientPort)
            verify_packet(self, expectedPktToClient, self.clientPort)
            logger.info("Client packet received on port %d!", self.clientPort)

    def sendPacket(self):
        self.sendPackets(self.numPackets // 3, self.lbNodeIps, self.lbNodePorts)
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

        self.server1Counter = 0
        self.server2Counter = 0

        newIp1 = "10.0.0.4"
        resp = self.migrate_node(old_ipv4=self.lbNodeIps[0], new_ipv4=newIp1)
        assert resp, "Response is nil, is the controller running?"
        assert resp.status_code == 200, f"Response is {resp.status_code}: {resp.text}"

        self.sendPackets(
            self.numPackets // 3,
            [newIp1, self.lbNodeIps[1]],
            [self.lbNodePorts[0], self.lbNodePorts[1]],
        )
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

        self.server1Counter = 0
        self.server2Counter = 0

        newIp2 = "10.0.0.5"
        resp = self.migrate_node(old_ipv4=self.lbNodeIps[1], new_ipv4=newIp2)
        assert resp is not None, "Response is nil, is the controller running?"
        assert resp.status_code == 200, f"Response is {resp.status_code}: {resp.text}"

        self.sendPackets(
            self.numPackets // 3,
            [newIp1, newIp2],
            [self.lbNodePorts[0], self.lbNodePorts[1]],
        )
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

    def verifyPackets(self):
        pass

    def runTest(self):
        self.runTestImpl()
