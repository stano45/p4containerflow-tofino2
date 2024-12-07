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

def ip(ip_string) -> int:
    return int(ip_address(ip_string))


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

    def insertEcmpGroupNoAction(
        self, dst_addr, prefix_len, actionName=None, dataFields=[]
    ):
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr), prefix_len=prefix_len)],
            actionName,
            dataFields,
        )

    def insertEcmpGroupRewriteSrc(self, dst_addr, prefix_len, new_src_ip):
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr), prefix_len=prefix_len)],
            "set_rewrite_src",
            [gc.DataTuple("new_src", ip(new_src_ip))],
        )

    def insertEcmpNhop(self, ecmp_select, nhop_ipv4, port):
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [gc.KeyTuple("ig_md.ecmp_select", ecmp_select)],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip(nhop_ipv4)),
                gc.DataTuple("port", port),
            ],
        )

    def sendAndVerifyPacketAnyPort(
        self, send_port, send_pkt, expected_pkts, verify_ports
    ):
        send_packet(self, send_port, send_pkt)
        rcv_idx = verify_any_packet_any_port(self, expected_pkts, verify_ports)
        return rcv_idx

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


class TestRewriteSource(AbstractTest):
    # Test rewriting source IP when server sends packet to client
    # this is essentially NAT

    clientPort = swports[0]
    serverPort = swports[1]

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Using client port: %s and server port: %s ",
            self.clientPort,
            self.serverPort,
        )
        self.insertEcmpGroupRewriteSrc("10.0.0.0", 0, "10.0.0.1")

        self.insertEcmpNhop(0, "10.0.0.0", self.clientPort)

    def sendPacket(self):
        serverPkt = simple_tcp_packet(
            ip_src="10.0.0.2",
            ip_dst="10.0.0.0",
        )
        send_packet(self, self.serverPort, serverPkt)

    def verifyPackets(self):
        expectedPktToClient = simple_tcp_packet(
            ip_src="10.0.0.1",
            ip_dst="10.0.0.0",
        )
        verify_packet(self, expectedPktToClient, self.clientPort)

    def runTest(self):
        self.runTestImpl()


class TestForwarding(AbstractTest):
    clientPort = swports[0]
    serverPort = swports[1]

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Using client port: %s and server port: %s ",
            self.clientPort,
            self.serverPort,
        )
        self.insertEcmpGroupNoAction("10.0.0.1", 0, "NoAction", [])

        self.insertEcmpNhop(1, "10.0.0.2", self.serverPort)
        self.insertEcmpNhop(2, "10.0.0.2", self.serverPort)

    def sendPacket(self):
        serverPkt = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.1",
        )
        send_packet(self, self.clientPort, serverPkt)

    def verifyPackets(self):
        expectedPktToServer = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.2",
        )
        verify_packet(self, expectedPktToServer, self.serverPort)

    def runTest(self):
        self.runTestImpl()


class TestEvenTrafficBalancingToServer(AbstractTest):
    # Test even traffic balancing from one client to two servers

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2]]
        self.numPackets = 200
        self.maxImbalancePercent = 20
        self.server1Counter = 0
        self.server2Counter = 0

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertEcmpGroupNoAction("10.0.0.1", 32, "NoAction", [])

        # Add next-hop entries for both server IPs (10.0.0.2 and 10.0.0.3) and their respective ports
        self.insertEcmpNhop(1, "10.0.0.2", self.serverPorts[0])
        self.insertEcmpNhop(2, "10.0.0.3", self.serverPorts[1])

    def sendPacket(self):
        for i in range(self.numPackets):
            logger.info("Sending packet %d...", i)
            srcPort = random.randint(1024, 65535)  # Random source port

            clientPkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=srcPort,  # Set the random source port
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.2",
                tcp_sport=srcPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.3",
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
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

    def runTest(self):
        self.runTestImpl()


class TestBidirectionalTraffic(AbstractTest):
    # Test even traffic balancing from one client to two servers, and traffic from servers back to client

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2]]
        self.numPackets = 200
        self.maxImbalancePercent = 20
        self.server1Counter = 0
        self.server2Counter = 0
        self.serverTcpPort = 25565

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up bidirectional traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertEcmpGroupNoAction("10.0.0.1", 32, "NoAction", [])

        # Rewrite source to load balancer IP on server -> client traffic
        self.insertEcmpGroupRewriteSrc("10.0.0.0", 0, "10.0.0.1")

        # Add next-hop entries for the load balancer and both servers
        self.insertEcmpNhop(0, "10.0.0.0", self.clientPort)
        self.insertEcmpNhop(1, "10.0.0.2", self.serverPorts[0])
        self.insertEcmpNhop(2, "10.0.0.3", self.serverPorts[1])

    def sendPacket(self):
        for i in range(self.numPackets):
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
                ip_dst="10.0.0.2",
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.3",
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
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

            # Sending response from server to client
            logger.info(
                "Sending response from port %d to client port %d...",
                self.serverPorts[rcvIdx],
                self.clientPort,
            )

            serverIp = "10.0.0.2" if rcvIdx == 0 else "10.0.0.3"
            serverPkt = simple_tcp_packet(
                ip_src=serverIp,
                ip_dst="10.0.0.0",
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            send_packet(self, self.serverPorts[rcvIdx], serverPkt)

            expectedPktToClient = simple_tcp_packet(
                ip_src="10.0.0.1",
                ip_dst="10.0.0.0",
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            logger.info("Verifying packet on client port %d...", self.clientPort)
            verify_packet(self, expectedPktToClient, self.clientPort)

    def verifyPackets(self):
        self.checkTrafficBalance(
            self.server1Counter, self.server2Counter, self.maxImbalancePercent
        )

    def runTest(self):
        self.runTestImpl()


class TestPortChange(AbstractTest):
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
        self.clearTables()

        logger.info(
            "Setting up port change traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertEcmpGroupNoAction("10.0.0.1", 32, "NoAction", [])

        # Rewrite source to load balancer IP on server -> client traffic
        self.insertEcmpGroupRewriteSrc("10.0.0.0", 0, "10.0.0.1")

        # Add initial next-hop entries for the load balancer and first two servers
        self.insertEcmpNhop(0, "10.0.0.0", self.clientPort)
        self.insertEcmpNhop(1, "10.0.0.2", self.serverPorts[0])
        self.insertEcmpNhop(2, "10.0.0.3", self.serverPorts[1])

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
        self.modifyTableEntry(
            "SwitchIngress.ecmp_nhop",
            [gc.KeyTuple("ig_md.ecmp_select", 1)],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.4")),
                gc.DataTuple("port", self.serverPorts[2]),
            ],
        )
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
        self.modifyTableEntry(
            "SwitchIngress.ecmp_nhop",
            [gc.KeyTuple("ig_md.ecmp_select", 2)],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.5")),
                gc.DataTuple("port", self.serverPorts[3]),
            ],
        )
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
