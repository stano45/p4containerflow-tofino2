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

logger = get_logger()
swports = get_sw_ports()

print("SW Ports: ", swports)


def ip(ip_string):
    return gc.ipv4_to_bytes(ip_string)


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        super().setUp(0, "t2na_load_balancer")
        self.devId = 0
        self.tableEntries = {}
        self.bfrtInfo = self.interface.bfrt_info_get("t2na_load_balancer")

        # Set target to all pipes on device self.devId.
        self.target = gc.Target(device_id=0, pipe_id=0xFFFF)

    def clearTables(self):
        for tableName, keys in reversed(self.tableEntries.items()):
            testTable = self.bfrtInfo.table_get(tableName)
            testTable.entry_del(self.target, keys)
        self.tableEntries = {}

    def tearDown(self):
        self.clearTables()
        super().tearDown()

    def insertTableEntry(
        self, tableName, keyFields=None, actionName=None, dataFields=[]
    ):
        testTable = self.bfrtInfo.table_get(tableName)
        keyList = [testTable.make_key(keyFields)]
        dataList = [testTable.make_data(dataFields, actionName)]
        testTable.entry_add(self.target, keyList, dataList)
        existingEntries = self.tableEntries.get(tableName, [])
        existingEntries.extend(keyList)
        self.tableEntries[tableName] = existingEntries

    def modifyTableEntry(
        self, tableName, keyFields=None, actionName=None, dataFields=[]
    ):
        testTable = self.bfrtInfo.table_get(tableName)
        keyList = [testTable.make_key(keyFields)]
        dataList = [testTable.make_data(dataFields, actionName)]
        testTable.entry_mod(self.target, keyList, dataList)
        existingEntries = self.tableEntries.get(tableName, [])
        existingEntries.extend(keyList)
        # No duplicates, otherwise there's an error when deleting
        self.tableEntries[tableName] = list(set(existingEntries))

    def overrideDefaultEntry(self, tableName, actionName=None, dataFields=[]):
        testTable = self.bfrtInfo.table_get(tableName)
        data = testTable.make_data(dataFields, actionName)
        testTable.default_entry_set(self.target, data)

    def setRegisterValue(self, regName, value, index):
        regTable = self.bfrtInfo.table_get(regName)
        keyList = [regTable.make_key([gc.KeyTuple("$REGISTER_INDEX", index)])]
        valueList = []
        if isinstance(value, list):
            for val in value:
                valueList.append(gc.DataTuple(val[0], val[1]))
        else:
            valueList.append(gc.DataTuple("f1", value))
        regTable.entry_add(self.target, keyList, [regTable.make_data(valueList)])

    def entryAdd(self, tableObj, target, tableEntry):
        req = bfruntime_pb2.WriteRequest()
        gc._cpy_target(req, target)
        req.atomicity = bfruntime_pb2.WriteRequest.CONTINUE_ON_ERROR
        update = req.updates.add()
        update.type = bfruntime_pb2.Update.MODIFY
        update.entity.table_entry.CopyFrom(tableEntry)
        resp = self.interface.reader_writer_interface._write(req)
        tableObj.get_parser._parse_entry_write_response(resp)

    def insertForLoadBalancerEntry(self, dst_addr):
        self.insertTableEntry(
            "SwitchIngress.for_load_balancer",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "set_is_packet_for_load_balancer",
            [],
        )

    def insertForClientEntry(self, src_port):
        self.insertTableEntry(
            "SwitchIngress.for_client",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
            "set_is_packet_for_client",
            [],
        )

    def insertRewriteDstEntry(self, node_index, new_dst):
        self.insertTableEntry(
            "SwitchIngress.rewrite_dst",
            [gc.KeyTuple("ig_md.node_index", node_index)],
            "set_rewrite_dst",
            [gc.DataTuple("new_dst", ip(new_dst))],
        )

    def insertRewriteSrcEntry(self, src_port, new_src):
        self.insertTableEntry(
            "SwitchIngress.rewrite_src",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
            "set_rewrite_src",
            [gc.DataTuple("new_src", ip(new_src))],
        )

    def insertForward(self, dst_addr, port):
        self.insertTableEntry(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )

    def sendAndVerifyPacketAnyPort(
        self, send_port, send_pkt, expected_pkts, verify_ports
    ):
        send_packet(self, send_port, send_pkt)
        rcv_idx = verify_any_packet_any_port(self, expected_pkts, verify_ports)
        return rcv_idx

    def checkTrafficBalance(self, counters, max_imbalance=0.2):
        max_seen_imbalance = 0
        for i in range(len(counters)):
            for j in range(i + 1, len(counters)):
                counter1 = counters[i]
                counter2 = counters[j]
                total_packets = counter1 + counter2
                if total_packets == 0:
                    logger.warning(
                        f"Warning: no packets at counter indices {i} and {j}."
                    )
                    current_imbalance = 0
                else:
                    current_imbalance = abs(counter1 - counter2) / total_packets

                logger.info(
                    "Traffic distribution: Server 1: %d packets, Server 2: %d packets, Imbalance: %.2f%%",
                    counter1,
                    counter2,
                    current_imbalance * 100,
                )
                max_seen_imbalance = max(max_seen_imbalance, current_imbalance)
        assert (
            max_seen_imbalance <= max_imbalance
        ), f"Traffic imbalance too high: {max_seen_imbalance*100:.2f}%"

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


class TestRewriteSource(AbstractTest):
    # Test rewriting source IP when server sends packet to client

    clientPort = swports[0]
    serverPort = swports[1]

    def setupCtrlPlane(self):
        self.clearTables()

        # Load balancer IP
        self.insertForLoadBalancerEntry(dst_addr="10.0.0.10")
        # Fixed service port
        self.insertForClientEntry(src_port=12345)

        self.insertRewriteSrcEntry(12345, "10.0.0.10")
        self.insertForward("10.0.0.0", self.clientPort)

        logger.info(
            "Using client port: %s and server port: %s ",
            self.clientPort,
            self.serverPort,
        )

    def sendPacket(self):
        serverPkt = simple_tcp_packet(
            ip_src="10.0.0.2",
            ip_dst="10.0.0.0",
            tcp_sport=12345,
        )
        send_packet(self, self.serverPort, serverPkt)

    def verifyPackets(self):
        expectedPktToClient = simple_tcp_packet(
            ip_src="10.0.0.10",
            ip_dst="10.0.0.0",
            tcp_sport=12345,
        )
        verify_packet(self, expectedPktToClient, self.clientPort)

    def runTest(self):
        self.runTestImpl()


class TestForwarding(AbstractTest):
    def setUp(self):
        super().setUp()
        # Test basic L3 forwarding (required for k8s traffic)
        self.num_nodes = 4
        self.ports = [swports[i] for i in range(self.num_nodes)]
        self.ips = [f"10.0.0.{i}" for i in range(self.num_nodes)]

    def setupCtrlPlane(self):
        self.clearTables()

        for port in self.ports:
            logger.info("Using port: %s", port)

        for i in range(self.num_nodes):
            self.insertForward(self.ips[i], self.ports[i])

    def sendPacket(self):
        for i in range(self.num_nodes):
            for j in range(i + 1, self.num_nodes):
                pkt = simple_tcp_packet(
                    ip_src=self.ips[i],
                    ip_dst=self.ips[j],
                )
                send_packet(self, self.ports[i], pkt)
                verify_packet(self, pkt, self.ports[j])

                pkt = simple_tcp_packet(
                    ip_src=self.ips[j],
                    ip_dst=self.ips[i],
                )
                send_packet(self, self.ports[j], pkt)
                verify_packet(self, pkt, self.ports[i])

    def runTest(self):
        self.runTestImpl()


class TestEvenTrafficBalancingToServer(AbstractTest):
    # Test even traffic balancing from one client to two servers

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2]]
        self.numPackets = 100
        self.maxImbalance = 0.2
        self.server1Counter = 0
        self.server2Counter = 0

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        # Load balancer IP
        self.insertForLoadBalancerEntry(dst_addr="10.0.0.10")
        # Fixed service port
        self.insertForClientEntry(src_port=12345)
        self.insertRewriteSrcEntry(12345, "10.0.0.10")

        # Fixed number of 4 nodes in balancer, but we only have 2
        # so fill the table with only 2
        self.insertRewriteDstEntry(0, "10.0.0.1")
        self.insertRewriteDstEntry(1, "10.0.0.1")
        self.insertRewriteDstEntry(2, "10.0.0.2")
        self.insertRewriteDstEntry(3, "10.0.0.2")

        self.insertForward("10.0.0.0", self.clientPort)
        self.insertForward("10.0.0.1", self.serverPorts[0])
        self.insertForward("10.0.0.2", self.serverPorts[1])

    def sendPacket(self):
        for i in range(self.numPackets):
            logger.info("Sending packet %d...", i)
            srcPort = random.randint(12346, 65535)  # Random source port

            clientPkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.10",
                tcp_sport=srcPort,  # Set the random source port
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=srcPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.2",
                tcp_sport=srcPort,
            )

            logger.info("Verifying packet %d...", i)
            rcvIdx = self.sendAndVerifyPacketAnyPort(
                self.clientPort,
                clientPkt,
                [expectedPktToServer1, expectedPktToServer2],
                self.serverPorts,
            )
            logger.info("Packet %d received on port %d...", i, self.serverPorts[rcvIdx])
            if rcvIdx == 0:
                self.server1Counter += 1
            else:
                self.server2Counter += 1

    def verifyPackets(self):
        self.checkTrafficBalance(
            [self.server1Counter, self.server2Counter], self.maxImbalance
        )

    def runTest(self):
        self.runTestImpl()


class TestBidirectionalTraffic(AbstractTest):
    # Test even traffic balancing from one client to two servers, and traffic from servers back to client

    def setUp(self):
        super().setUp()
        self.lbIp = "10.0.0.10"
        self.clientIp = "10.0.0.0"
        self.clientPort = swports[0]
        self.numServers = 4
        self.serverPorts = [swports[i + 1] for i in range(self.numServers)]
        self.serverIps = [f"10.0.0.{i+1}" for i in range(self.numServers)]
        self.numPackets = 100
        self.maxImbalance = 0.2
        self.serverCounters = [0 for _ in range(self.numServers)]
        self.serverTcpPort = 12345

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        # Load balancer IP
        self.insertForLoadBalancerEntry(dst_addr=self.lbIp)
        # Fixed service port
        self.insertForClientEntry(src_port=self.serverTcpPort)
        self.insertRewriteSrcEntry(src_port=self.serverTcpPort, new_src=self.lbIp)
        self.insertForward(dst_addr=self.clientIp, port=self.clientPort)

        for i in range(self.numServers):
            self.insertRewriteDstEntry(node_index=i, new_dst=self.serverIps[i])
            self.insertForward(dst_addr=self.serverIps[i], port=self.serverPorts[i])

    def sendPacket(self):
        for i in range(self.numPackets):
            logger.info("Sending packet %d...", i)
            # Random source port
            clientTcpPort = random.randint(12346, 65535)

            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expecedPkts = []
            for i in range(self.numServers):
                expecedPkts.append(
                    simple_tcp_packet(
                        ip_src=self.clientIp,
                        ip_dst=self.serverIps[i],
                        tcp_sport=clientTcpPort,
                        tcp_dport=self.serverTcpPort,
                    )
                )
            logger.info("Verifying packet %d...", i)
            rcvIdx = self.sendAndVerifyPacketAnyPort(
                send_port=self.clientPort,
                send_pkt=clientPkt,
                expected_pkts=expecedPkts,
                verify_ports=self.serverPorts,
            )
            logger.info("Packet %d received on port %d...", i, self.serverPorts[rcvIdx])
            self.serverCounters[rcvIdx] += 1

            # Sending response from server to client
            logger.info(
                "Sending response from port %d to client port %d...",
                self.serverPorts[rcvIdx],
                self.clientPort,
            )

            serverIp = self.serverIps[rcvIdx]
            serverPkt = simple_tcp_packet(
                ip_src=serverIp,
                ip_dst=self.clientIp,
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            send_packet(self, self.serverPorts[rcvIdx], serverPkt)

            expectedPktToClient = simple_tcp_packet(
                ip_src=self.lbIp,
                ip_dst=self.clientIp,
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            logger.info("Verifying packet on client port %d...", self.clientPort)
            verify_packet(self, expectedPktToClient, self.clientPort)

    def verifyPackets(self):
        self.checkTrafficBalance(self.serverCounters, self.maxImbalance)

    def runTest(self):
        self.runTestImpl()


# class TestPortChange(AbstractTest):
#     # Test normal load balancing, change ports dynamically, and verify load balancing

#     def setUp(self):
#         super().setUp()
#         self.clientPort = swports[0]
#         self.serverPorts = [swports[1], swports[2], swports[3], swports[4]]
#         self.numPackets = 300
#         self.maxImbalancePercent = 20
#         self.server1Counter = 0
#         self.server2Counter = 0
#         self.serverTcpPort = 25565

#     def setupCtrlPlane(self):
#         self.clearTables()

#         logger.info(
#             "Setting up port change traffic balancing: Client port: %s, Server ports: %s",
#             self.clientPort,
#             self.serverPorts,
#         )

#         # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
#         self.insertEcmpGroupNoAction("10.0.0.1", 32, "NoAction", [])

#         # Rewrite source to load balancer IP on server -> client traffic
#         self.insertEcmpGroupRewriteSrc("10.0.0.0", 0, "10.0.0.1")

#         # Add initial next-hop entries for the load balancer and first two servers
#         self.insertEcmpNhop(0, "10.0.0.0", self.clientPort)
#         self.insertEcmpNhop(1, "10.0.0.2", self.serverPorts[0])
#         self.insertEcmpNhop(2, "10.0.0.3", self.serverPorts[1])

#     def sendPackets(self, num_packets, server_ips, server_ports):
#         for i in range(num_packets):
#             logger.info("Sending packet %d...", i)
#             # Random source port
#             clientTcpPort = random.randint(1024, 65535)

#             clientPkt = simple_tcp_packet(
#                 ip_src="10.0.0.0",
#                 ip_dst="10.0.0.1",
#                 tcp_sport=clientTcpPort,
#                 tcp_dport=self.serverTcpPort,
#             )
#             expectedPktToServer1 = simple_tcp_packet(
#                 ip_src="10.0.0.0",
#                 ip_dst=server_ips[0],
#                 tcp_sport=clientTcpPort,
#                 tcp_dport=self.serverTcpPort,
#             )
#             expectedPktToServer2 = simple_tcp_packet(
#                 ip_src="10.0.0.0",
#                 ip_dst=server_ips[1],
#                 tcp_sport=clientTcpPort,
#                 tcp_dport=self.serverTcpPort,
#             )

#             logger.info("Verifying packet %d...", i)
#             rcvIdx = self.sendAndVerifyPacketAnyPort(
#                 self.clientPort,
#                 clientPkt,
#                 [expectedPktToServer1, expectedPktToServer2],
#                 server_ports,
#             )
#             logger.info("Packet %d received on port %d...", i, server_ports[rcvIdx])
#             if rcvIdx == 0:
#                 self.server1Counter += 1
#             else:
#                 self.server2Counter += 1

#             # Sending response from server to client
#             logger.info(
#                 "Sending response from port %d to client port %d...",
#                 server_ports[rcvIdx],
#                 self.clientPort,
#             )

#             serverIp = server_ips[rcvIdx]
#             serverPkt = simple_tcp_packet(
#                 ip_src=serverIp,
#                 ip_dst="10.0.0.0",
#                 tcp_sport=self.serverTcpPort,
#                 tcp_dport=clientTcpPort,
#             )
#             send_packet(self, server_ports[rcvIdx], serverPkt)

#             expectedPktToClient = simple_tcp_packet(
#                 ip_src="10.0.0.1",
#                 ip_dst="10.0.0.0",
#                 tcp_sport=self.serverTcpPort,
#                 tcp_dport=clientTcpPort,
#             )
#             logger.info("Verifying packet on client port %d...", self.clientPort)
#             verify_packet(self, expectedPktToClient, self.clientPort)

#     def sendPacket(self):
#         # First phase: initial two servers
#         self.sendPackets(
#             self.numPackets // 3, ["10.0.0.2", "10.0.0.3"], self.serverPorts[:2]
#         )
#         self.checkTrafficBalance(
#             self.server1Counter, self.server2Counter, self.maxImbalancePercent
#         )

#         # Reset counters
#         self.server1Counter = 0
#         self.server2Counter = 0

#         # Modify first next-hop entry to point to a new server (10.0.0.4)
#         self.modifyTableEntry(
#             "SwitchIngress.ecmp_nhop",
#             [gc.KeyTuple("ig_md.ecmp_select", 1)],
#             "set_ecmp_nhop",
#             [
#                 gc.DataTuple("nhop_ipv4", ip("10.0.0.4")),
#                 gc.DataTuple("port", self.serverPorts[2]),
#             ],
#         )
#         self.sendPackets(
#             self.numPackets // 3,
#             ["10.0.0.4", "10.0.0.3"],
#             [self.serverPorts[2], self.serverPorts[1]],
#         )
#         self.checkTrafficBalance(
#             self.server1Counter, self.server2Counter, self.maxImbalancePercent
#         )

#         # Reset counters
#         self.server1Counter = 0
#         self.server2Counter = 0

#         # Modify second next-hop entry to point to another new server (10.0.0.5)
#         self.modifyTableEntry(
#             "SwitchIngress.ecmp_nhop",
#             [gc.KeyTuple("ig_md.ecmp_select", 2)],
#             "set_ecmp_nhop",
#             [
#                 gc.DataTuple("nhop_ipv4", ip("10.0.0.5")),
#                 gc.DataTuple("port", self.serverPorts[3]),
#             ],
#         )
#         self.sendPackets(
#             self.numPackets // 3, ["10.0.0.4", "10.0.0.5"], self.serverPorts[2:4]
#         )
#         self.checkTrafficBalance(
#             self.server1Counter, self.server2Counter, self.maxImbalancePercent
#         )

#     def verifyPackets(self):
#         # All verification is done during sendPacket
#         pass

#     def runTest(self):
#         self.runTestImpl()
