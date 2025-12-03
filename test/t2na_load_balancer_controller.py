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


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        # Setting up PTF dataplane
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

    # Placeholder methods to be overridden by subclasses
    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass


class TestController(AbstractTest):
    # Test normal load balancing, change ports dynamically, and verify load balancing

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2], swports[3], swports[4]]
        self.numPackets = 300
        self.maxImbalancePercent = 20
        self.server1Counter = 0
        self.server2Counter = 0
        self.serverTcpPort = 25565

    def setupCtrlPlane(self):
        # Nodes are now pre-configured via the controller's config file.
        # The controller initializes nodes from the "nodes" field in the master switch config.
        # Expected initial nodes: 10.0.0.0 (client), 10.0.0.2, 10.0.0.3 (LB servers)
        logger.info(
            "Setting up port change traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )
        logger.info("Nodes should be pre-configured via controller config file")

    def sendPackets(self, num_packets, server_ips, server_ports):
        for i in range(num_packets):
            logger.info("Sending packet %d...", i)
            # Random source port
            clientTcpPort = random.randint(1024, 65535)

            clientPkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst=server_ips[0],
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst=server_ips[1],
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
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

            # Sending response from server to client
            logger.info(
                "Sending response from port %d to client port %d...",
                server_ports[rcvIdx],
                self.clientPort,
            )

            serverIp = server_ips[rcvIdx]
            serverPkt = simple_tcp_packet(
                ip_src=serverIp,
                ip_dst="10.0.0.0",
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            send_packet(self, server_ports[rcvIdx], serverPkt)

            expectedPktToClient = simple_tcp_packet(
                ip_src="10.0.0.1",
                ip_dst="10.0.0.0",
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            logger.info("Verifying packet on client port %d...", self.clientPort)
            verify_packet(self, expectedPktToClient, self.clientPort)
            logger.info("Client packet received on port %d!", self.clientPort)

    def sendPacket(self):
        # First phase: initial two servers
        self.sendPackets(
            self.numPackets // 3, ["10.0.0.2", "10.0.0.3"], self.serverPorts[:2]
        )
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

        # Reset counters
        self.server1Counter = 0
        self.server2Counter = 0

        # Modify first next-hop entry to point to a new server (10.0.0.4)
        resp = self.migrate_node(old_ipv4="10.0.0.2", new_ipv4="10.0.0.4")
        assert resp, "Response is nil, is the controller running?"
        assert resp.status_code == 200, f"Response is {resp.status_code}: {resp.text}"

        self.sendPackets(
            self.numPackets // 3,
            ["10.0.0.4", "10.0.0.3"],
            [self.serverPorts[2], self.serverPorts[1]],
        )
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

        # Reset counters
        self.server1Counter = 0
        self.server2Counter = 0

        # Modify second next-hop entry to point to another new server (10.0.0.5)
        resp = self.migrate_node(old_ipv4="10.0.0.3", new_ipv4="10.0.0.5")

        assert resp is not None, "Response is nil, is the controller running?"
        assert resp.status_code == 200, f"Response is {resp.status_code}: {resp.text}"

        self.sendPackets(
            self.numPackets // 3, ["10.0.0.4", "10.0.0.5"], self.serverPorts[2:4]
        )
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

    def verifyPackets(self):
        # All verification is done during sendPacket
        pass

    def runTest(self):
        self.runTestImpl()
