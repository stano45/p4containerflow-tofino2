import random
import bfrt_grpc.bfruntime_pb2 as bfruntime_pb2
import bfrt_grpc.client as gc
from ipaddress import ip_address
from bfruntime_client_base_tests import BfRuntimeTest
from p4testutils.misc_utils import get_logger, get_sw_ports, simple_tcp_packet
from ptf.testutils import (
    send_packet,
    verify_packet,
    verify_no_other_packets,
    verify_any_packet_any_port,
)
import requests

logger = get_logger()
swports = get_sw_ports()

print("SW Ports: ", swports)

def ip(ip_string) -> int:
    return int(ip_address(ip_string))


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def sendAndVerifyPacketAnyPort(
        self, send_port, send_pkt, expected_pkts, verify_ports
    ):
        send_packet(self, send_port, send_pkt)
        rcv_idx = verify_any_packet_any_port(self, expected_pkts, verify_ports)
        return rcv_idx
    
    def update_node(self, source_ip, target_ip, target_idx, is_client):
        url = "http://127.0.0.1:5000/update_node"
        headers = {"Content-Type": "application/json"}
        data = {
            "old_ipv4": source_ip,
            "new_ipv4": target_ip,
            "eport": target_idx,
            "is_client": is_client,
        }

        response = requests.post(url, headers=headers, json=data)
        return response
    
    def add_node(self, target_ip, port, is_client):
        url = "http://127.0.0.1:5000/add_node"
        headers = {"Content-Type": "application/json"}
        data = {
            "ipv4": target_ip,
            "eport": port,
            "is_client": is_client,
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
        assert (
            imbalance_percentage <= max_imbalance_percent
        ), f"Traffic imbalance too high: {imbalance_percentage:.2f}%"

    def verifyNoOtherPackets(self):
        verify_no_other_packets(self, self.devId, timeout=2)

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
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2], swports[3], swports[4]]
        self.numPackets = 300
        self.maxImbalancePercent = 20
        self.server1Counter = 0
        self.server2Counter = 0
        self.serverTcpPort = 25565

    def setupCtrlPlane(self):
        logger.info(
            "Setting up port change traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )
        self.add_node(target_ip="10.0.0.0", port=self.clientPort, is_client=True)
        self.add_node(target_ip="10.0.0.2", port=self.serverPorts[0], is_client=False)
        self.add_node(target_ip="10.0.0.3", port=self.serverPorts[1], is_client=False)

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
            logger.info("Packet %d received on port %d...", i, server_ports[rcvIdx])
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
        resp = self.update_node(source_ip="10.0.0.2", target_ip="10.0.0.4", target_idx=self.serverPorts[2],is_client=False)
        assert resp is not None, "Response is nil, is the controller running?"
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
        resp = self.update_node(source_ip="10.0.0.3", target_ip="10.0.0.5", target_idx=self.serverPorts[3],is_client=False)

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
