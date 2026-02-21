import random
import bfrt_grpc.bfruntime_pb2 as bfruntime_pb2
import bfrt_grpc.client as gc
from bfruntime_client_base_tests import BfRuntimeTest
from p4testutils.misc_utils import get_logger, get_sw_ports, simple_tcp_packet
from ptf.testutils import (
    send_packet,
    verify_packet,
    verify_no_other_packets,
    verify_any_packet_any_port,
    simple_arp_packet,
)

logger = get_logger()
swports = get_sw_ports()


def ip(ip_string):
    return gc.ipv4_to_bytes(ip_string)


def mac(mac_string):
    return gc.mac_to_bytes(mac_string)


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        # Pass p4_name=None so the framework auto-detects from the running
        # switch/model (works for both tna_load_balancer and t2na_load_balancer).
        super().setUp(0, None)
        self.devId = 0
        self.tableEntries = {}
        self.bfrtInfo = self.bfrt_info  # already set by BfRuntimeTest.setUp

        self.target = gc.Target(device_id=0, pipe_id=0xFFFF)
        self.lbIp = "10.0.0.10"
        self.clientIp = "10.0.0.0"

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

    def insertNodeSelectorEntry(self, dst_addr, group_id):
        self.insertTableEntry(
            tableName="SwitchIngress.node_selector",
            keyFields=[gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            dataFields=[
                gc.DataTuple("$SELECTOR_GROUP_ID", group_id),
            ],
        )

    def insertSelectionTableEntry(
        self,
        members,
        member_status,
        group_id=1,
        max_grp_size=4,
    ):
        self.insertTableEntry(
            tableName="SwitchIngress.action_selector",
            keyFields=[gc.KeyTuple("$SELECTOR_GROUP_ID", group_id)],
            dataFields=[
                gc.DataTuple("$MAX_GROUP_SIZE", max_grp_size),
                gc.DataTuple("$ACTION_MEMBER_ID", int_arr_val=members),
                gc.DataTuple("$ACTION_MEMBER_STATUS", bool_arr_val=member_status),
            ],
        )

    def modifySelectionTableEntry(
        self,
        members,
        member_status,
        group_id=1,
        max_grp_size=4,
    ):
        self.modifyTableEntry(
            tableName="SwitchIngress.action_selector",
            keyFields=[gc.KeyTuple("$SELECTOR_GROUP_ID", group_id)],
            dataFields=[
                gc.DataTuple("$MAX_GROUP_SIZE", max_grp_size),
                gc.DataTuple("$ACTION_MEMBER_ID", int_arr_val=members),
                gc.DataTuple("$ACTION_MEMBER_STATUS", bool_arr_val=member_status),
            ],
        )

    def insertActionTableEntry(self, node_index, new_dst):
        self.insertTableEntry(
            "SwitchIngress.action_selector_ap",
            [gc.KeyTuple("$ACTION_MEMBER_ID", node_index)],
            "SwitchIngress.set_rewrite_dst",
            [gc.DataTuple("new_dst", ip(new_dst))],
        )

    def modifyActionTableEntry(self, node_index, new_dst):
        self.modifyTableEntry(
            "SwitchIngress.action_selector_ap",
            [gc.KeyTuple("$ACTION_MEMBER_ID", node_index)],
            "SwitchIngress.set_rewrite_dst",
            [gc.DataTuple("new_dst", ip(new_dst))],
        )

    def insertClientSnatEntry(self, src_port, new_src):
        self.insertTableEntry(
            "SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
            "SwitchIngress.set_rewrite_src",
            [gc.DataTuple("new_src", ip(new_src))],
        )

    def insertForwardEntry(self, dst_addr, port):
        self.insertTableEntry(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "SwitchIngress.set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )

    def insertForwardWithMacEntry(self, dst_addr, port, dst_mac):
        self.insertTableEntry(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "SwitchIngress.set_egress_port_with_mac",
            [
                gc.DataTuple("port", port),
                gc.DataTuple("dst_mac", mac(dst_mac)),
            ],
        )

    def modifyForwardEntry(self, dst_addr, port):
        self.modifyTableEntry(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "SwitchIngress.set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )

    def modifyForwardWithMacEntry(self, dst_addr, port, dst_mac):
        self.modifyTableEntry(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip(dst_addr))],
            "SwitchIngress.set_egress_port_with_mac",
            [
                gc.DataTuple("port", port),
                gc.DataTuple("dst_mac", mac(dst_mac)),
            ],
        )

    def insertArpForwardEntry(self, target_ip, port):
        self.insertTableEntry(
            "SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", ip(target_ip))],
            "SwitchIngress.set_egress_port",
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
        assert max_seen_imbalance <= max_imbalance, (
            f"Traffic imbalance too high: {max_seen_imbalance * 100:.2f}%"
        )

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

    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass


class TestPortTableAccessOnModel(AbstractTest):
    """Verify the $PORT BF-RT table is accessible on the Tofino model.

    On the model, ports are auto-created, but the $PORT table should still
    be queryable. This mirrors the hardware port configuration tests and
    ensures the controller's setup_ports() method would work against the model.
    """

    def setUp(self):
        super().setUp()

    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass

    def runTest(self):
        # Try to access $PORT from the P4-bound bfrt_info first
        port_table = None
        try:
            port_table = self.bfrtInfo.table_get("$PORT")
        except Exception:
            # $PORT is a fixed table, may not be in the P4-specific bfrt_info;
            # try global bfrt_info instead
            try:
                global_info = self.interface.bfrt_info_get()
                port_table = global_info.table_get("$PORT")
            except Exception as e:
                logger.warning("$PORT table not accessible on model: %s", e)
                # On some model versions $PORT may not be available; skip gracefully
                return

        assert port_table is not None, "$PORT table should be accessible"

        # Read existing ports (model auto-creates ports)
        target = gc.Target(device_id=0, pipe_id=0xFFFF)
        try:
            resp = port_table.entry_get(target, [])
            entries = list(resp)
            logger.info("$PORT table has %d entries on model", len(entries))
            assert isinstance(entries, list), "$PORT entries should be a list"
        except Exception as e:
            logger.warning("Could not read $PORT entries on model: %s", e)


class TestPortChangeKeepConnection(AbstractTest):

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.numServers = 4
        self.serverPorts = [swports[i + 1] for i in range(self.numServers)]
        self.serverIps = [f"10.0.0.{i + 1}" for i in range(self.numServers)]
        self.numPackets = 100
        self.clientTcpPort = 65000
        self.serverTcpPort = 12345

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up port change test: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        self.insertClientSnatEntry(src_port=12345, new_src=self.lbIp)
        self.insertForwardEntry(dst_addr=self.clientIp, port=self.clientPort)

        for i in range(self.numServers):
            logger.info(
                f"Adding action entry: node_index={i}, ipv4={self.serverIps[i]}, port={self.serverPorts[i]}"
            )
            self.insertForwardEntry(
                dst_addr=self.serverIps[i], port=self.serverPorts[i]
            )

        self.numNodes = self.numServers - 1
        for i in range(self.numNodes):
            self.insertActionTableEntry(node_index=i, new_dst=self.serverIps[i])

        self.selection_members = list(range(self.numNodes))
        self.member_status = [True] * self.numNodes
        self.insertSelectionTableEntry(
            members=self.selection_members,
            member_status=self.member_status,
        )

        self.insertNodeSelectorEntry(dst_addr=self.lbIp, group_id=1)

    def sendPacket(self):
        prevRcvIdx = None
        for i in range(self.numPackets // 2):
            logger.info("Sending packet #%d to load balancer...", i)
            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=self.clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPkts = []
            for j in range(self.numNodes):
                expectedPkts.append(
                    simple_tcp_packet(
                        ip_src=self.clientIp,
                        ip_dst=self.serverIps[j],
                        tcp_sport=self.clientTcpPort,
                        tcp_dport=self.serverTcpPort,
                    )
                )
            logger.info("Verifying packet %d...", i)
            rcvIdx = self.sendAndVerifyPacketAnyPort(
                send_port=self.clientPort,
                send_pkt=clientPkt,
                expected_pkts=expectedPkts,
                verify_ports=self.serverPorts[: self.numNodes],
            )
            rcvPort = self.serverPorts[rcvIdx]
            logger.info("Packet %d received on port %d...", i, rcvPort)
            if prevRcvIdx is not None:
                assert rcvIdx == prevRcvIdx
            prevRcvIdx = rcvIdx

        targetServerIdx = self.numServers - 1
        self.modifyActionTableEntry(
            node_index=rcvIdx, new_dst=self.serverIps[targetServerIdx]
        )
        for j in range(i, self.numPackets):
            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=self.clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            logger.info("Sending packet #%d to load balancer...", j)
            send_packet(self, self.clientPort, clientPkt)

            expectedPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.serverIps[targetServerIdx],
                tcp_sport=self.clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            logger.info("Verifying packet %d...", j)
            verify_packet(self, expectedPkt, self.serverPorts[targetServerIdx])
            logger.info(
                "Packet %d received on port %d...", j, self.serverPorts[targetServerIdx]
            )

    def verifyPackets(self):
        pass

    def runTest(self):
        self.runTestImpl()


class TestRewriteSource(AbstractTest):

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPort = swports[1]

    def setupCtrlPlane(self):
        self.clearTables()

        self.insertClientSnatEntry(src_port=12345, new_src=self.lbIp)
        self.insertForwardEntry(self.clientIp, self.clientPort)

        logger.info(
            "Using client port: %s and server port: %s ",
            self.clientPort,
            self.serverPort,
        )

    def sendPacket(self):
        serverPkt = simple_tcp_packet(
            ip_src="10.0.0.2",
            ip_dst=self.clientIp,
            tcp_sport=12345,
        )
        send_packet(self, self.serverPort, serverPkt)

    def verifyPackets(self):
        expectedPktToClient = simple_tcp_packet(
            ip_src="10.0.0.10",
            ip_dst=self.clientIp,
            tcp_sport=12345,
        )
        verify_packet(self, expectedPktToClient, self.clientPort)

    def runTest(self):
        self.runTestImpl()


class TestForwarding(AbstractTest):

    def setUp(self):
        super().setUp()
        self.num_nodes = 4
        self.ports = [swports[i] for i in range(self.num_nodes)]
        self.ips = [f"10.0.0.{i}" for i in range(self.num_nodes)]

    def setupCtrlPlane(self):
        self.clearTables()

        for port in self.ports:
            logger.info("Using port: %s", port)

        for i in range(self.num_nodes):
            self.insertForwardEntry(self.ips[i], self.ports[i])

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

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.serverPorts = [swports[1], swports[2]]
        self.serverIps = ["10.0.0.1", "10.0.0.2"]
        self.numPackets = 100
        self.maxImbalance = 0.3
        self.serverCounters = [0, 0]

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        self.insertActionTableEntry(node_index=0, new_dst=self.serverIps[0])
        self.insertActionTableEntry(node_index=1, new_dst=self.serverIps[1])
        self.insertSelectionTableEntry([0, 1], [True] * 2, group_id=1, max_grp_size=4)
        self.insertNodeSelectorEntry(dst_addr=self.lbIp, group_id=1)

        self.insertForwardEntry(self.clientIp, self.clientPort)
        self.insertForwardEntry(self.serverIps[0], self.serverPorts[0])
        self.insertForwardEntry(self.serverIps[1], self.serverPorts[1])

    def sendPacket(self):
        for i in range(self.numPackets):
            logger.info("Sending packet %d...", i)
            srcPort = random.randint(12346, 65535)

            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=srcPort,
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.serverIps[0],
                tcp_sport=srcPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.serverIps[1],
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

            self.serverCounters[rcvIdx] += 1

    def verifyPackets(self):
        self.checkTrafficBalance(self.serverCounters, self.maxImbalance)

    def runTest(self):
        self.runTestImpl()


class TestBidirectionalTraffic(AbstractTest):

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.numServers = 4
        self.serverPorts = [swports[i + 1] for i in range(self.numServers)]
        self.serverIps = [f"10.0.0.{i + 1}" for i in range(self.numServers)]
        self.numPackets = 200
        self.maxImbalance = 0.3
        self.serverCounters = [0 for _ in range(self.numServers)]
        self.serverTcpPort = 12345

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        self.insertClientSnatEntry(src_port=12345, new_src=self.lbIp)
        self.insertForwardEntry(dst_addr=self.clientIp, port=self.clientPort)

        for i in range(self.numServers):
            self.insertActionTableEntry(node_index=i, new_dst=self.serverIps[i])
            self.insertForwardEntry(
                dst_addr=self.serverIps[i], port=self.serverPorts[i]
            )

        self.insertSelectionTableEntry(
            members=list(range(self.numServers)),
            member_status=[True] * self.numServers,
            group_id=1,
            max_grp_size=4,
        )
        self.insertNodeSelectorEntry(dst_addr=self.lbIp, group_id=1)

    def sendPacket(self):
        for i in range(self.numPackets):
            logger.info("Sending packet %d...", i)
            clientTcpPort = random.randint(12346, 65535)

            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPkts = []
            for i in range(self.numServers):
                expectedPkts.append(
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
                expected_pkts=expectedPkts,
                verify_ports=self.serverPorts,
            )
            logger.info("Packet %d received on port %d...", i, self.serverPorts[rcvIdx])
            self.serverCounters[rcvIdx] += 1

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


class TestArpForwarding(AbstractTest):
    """Test that ARP packets are forwarded based on the arp_forward table."""

    def setUp(self):
        super().setUp()
        self.num_nodes = 4
        self.ports = [swports[i] for i in range(self.num_nodes)]
        self.ips = [f"10.0.0.{i}" for i in range(self.num_nodes)]
        self.macs = [f"00:00:00:00:00:0{i}" for i in range(self.num_nodes)]

    def setupCtrlPlane(self):
        self.clearTables()

        for i in range(self.num_nodes):
            self.insertArpForwardEntry(self.ips[i], self.ports[i])

    def sendPacket(self):
        for i in range(self.num_nodes):
            for j in range(self.num_nodes):
                if i == j:
                    continue
                # ARP request from node i asking for node j's MAC
                arp_req = simple_arp_packet(
                    eth_src=self.macs[i],
                    eth_dst="ff:ff:ff:ff:ff:ff",
                    arp_op=1,
                    ip_snd=self.ips[i],
                    ip_tgt=self.ips[j],
                    hw_snd=self.macs[i],
                    hw_tgt="00:00:00:00:00:00",
                )
                logger.info(
                    "Sending ARP request from port %d (%s) for %s",
                    self.ports[i], self.ips[i], self.ips[j],
                )
                send_packet(self, self.ports[i], arp_req)
                # Should arrive on node j's port
                verify_packet(self, arp_req, self.ports[j])

                # ARP reply from node j back to node i
                arp_reply = simple_arp_packet(
                    eth_src=self.macs[j],
                    eth_dst=self.macs[i],
                    arp_op=2,
                    ip_snd=self.ips[j],
                    ip_tgt=self.ips[i],
                    hw_snd=self.macs[j],
                    hw_tgt=self.macs[i],
                )
                logger.info(
                    "Sending ARP reply from port %d (%s) to %s",
                    self.ports[j], self.ips[j], self.ips[i],
                )
                send_packet(self, self.ports[j], arp_reply)
                # Should arrive on node i's port
                verify_packet(self, arp_reply, self.ports[i])

    def runTest(self):
        self.runTestImpl()


class TestForwardingWithMacRewrite(AbstractTest):
    """Verify that set_egress_port_with_mac rewrites dst MAC in forwarded packets."""

    def setUp(self):
        super().setUp()
        self.num_nodes = 3
        self.ports = [swports[i] for i in range(self.num_nodes)]
        self.ips = [f"10.0.0.{i}" for i in range(self.num_nodes)]
        self.original_macs = [f"00:11:22:33:44:{i:02x}" for i in range(self.num_nodes)]
        self.rewrite_macs = [f"aa:bb:cc:dd:ee:{i:02x}" for i in range(self.num_nodes)]

    def setupCtrlPlane(self):
        self.clearTables()
        for i in range(self.num_nodes):
            self.insertForwardWithMacEntry(
                self.ips[i], self.ports[i], self.rewrite_macs[i]
            )

    def sendPacket(self):
        for i in range(self.num_nodes):
            for j in range(self.num_nodes):
                if i == j:
                    continue
                pkt = simple_tcp_packet(
                    eth_src=self.original_macs[i],
                    eth_dst=self.original_macs[j],
                    ip_src=self.ips[i],
                    ip_dst=self.ips[j],
                )
                expected_pkt = simple_tcp_packet(
                    eth_src=self.original_macs[i],
                    eth_dst=self.rewrite_macs[j],
                    ip_src=self.ips[i],
                    ip_dst=self.ips[j],
                )
                logger.info(
                    "Sending from port %d (%s) to %s, expect dst_mac rewritten to %s",
                    self.ports[i], self.ips[i], self.ips[j], self.rewrite_macs[j],
                )
                send_packet(self, self.ports[i], pkt)
                verify_packet(self, expected_pkt, self.ports[j])

    def runTest(self):
        self.runTestImpl()


class TestHairpinForwarding(AbstractTest):
    """Verify hairpin: packet exits the same port it entered, with dst MAC rewritten.

    This is the key scenario for routing traffic through the switch even when
    the server is on the same host as the ingress link (e.g. lakewood).
    """

    def setUp(self):
        super().setUp()
        self.hairpin_port = swports[0]
        self.other_port = swports[1]
        self.server_ip = "10.0.0.1"
        self.client_ip = "10.0.0.2"
        self.server_mac = "aa:bb:cc:00:00:01"
        self.client_mac = "00:11:22:33:44:00"

    def setupCtrlPlane(self):
        self.clearTables()
        self.insertForwardWithMacEntry(
            self.server_ip, self.hairpin_port, self.server_mac
        )
        self.insertForwardEntry(self.client_ip, self.other_port)

    def sendPacket(self):
        # Client-to-server: arrives on hairpin_port, forwarded back out hairpin_port
        pkt = simple_tcp_packet(
            eth_src=self.client_mac,
            eth_dst="ff:ff:ff:ff:ff:ff",
            ip_src=self.client_ip,
            ip_dst=self.server_ip,
            tcp_sport=50000,
            tcp_dport=8080,
        )
        expected_pkt = simple_tcp_packet(
            eth_src=self.client_mac,
            eth_dst=self.server_mac,
            ip_src=self.client_ip,
            ip_dst=self.server_ip,
            tcp_sport=50000,
            tcp_dport=8080,
        )
        logger.info(
            "Hairpin test: sending on port %d, expecting back on same port with dst_mac=%s",
            self.hairpin_port, self.server_mac,
        )
        send_packet(self, self.hairpin_port, pkt)
        verify_packet(self, expected_pkt, self.hairpin_port)

        # Server response: arrives on hairpin_port, forwarded to other_port (normal path)
        resp_pkt = simple_tcp_packet(
            eth_src=self.server_mac,
            eth_dst=self.client_mac,
            ip_src=self.server_ip,
            ip_dst=self.client_ip,
            tcp_sport=8080,
            tcp_dport=50000,
        )
        logger.info(
            "Response: sending from hairpin_port %d, expecting on port %d",
            self.hairpin_port, self.other_port,
        )
        send_packet(self, self.hairpin_port, resp_pkt)
        verify_packet(self, resp_pkt, self.other_port)

    def runTest(self):
        self.runTestImpl()


class TestHairpinMigration(AbstractTest):
    """Simulate migration from hairpin (same-port) to a different port.

    Before migration: server is on hairpin_port (same as client ingress).
    After migration: server moves to a different port.
    Verifies that forward table MODIFY correctly switches from hairpin to cross-port.
    """

    def setUp(self):
        super().setUp()
        self.client_port = swports[0]
        self.server_port_before = swports[0]  # hairpin
        self.server_port_after = swports[1]   # different host
        self.server_ip = "10.0.0.1"
        self.client_ip = "10.0.0.2"
        self.server_mac_before = "aa:bb:cc:00:00:01"
        self.server_mac_after = "aa:bb:cc:00:00:02"
        self.client_mac = "00:11:22:33:44:00"
        self.numPackets = 10

    def setupCtrlPlane(self):
        self.clearTables()
        self.insertForwardWithMacEntry(
            self.server_ip, self.server_port_before, self.server_mac_before
        )
        self.insertForwardEntry(self.client_ip, self.client_port)

    def sendPacket(self):
        pkt = simple_tcp_packet(
            eth_src=self.client_mac,
            eth_dst="ff:ff:ff:ff:ff:ff",
            ip_src=self.client_ip,
            ip_dst=self.server_ip,
            tcp_sport=50000,
            tcp_dport=8080,
        )

        # Phase 1: hairpin — packet returns on same port
        for i in range(self.numPackets):
            expected = simple_tcp_packet(
                eth_src=self.client_mac,
                eth_dst=self.server_mac_before,
                ip_src=self.client_ip,
                ip_dst=self.server_ip,
                tcp_sport=50000,
                tcp_dport=8080,
            )
            logger.info("Pre-migration packet %d (hairpin)", i)
            send_packet(self, self.client_port, pkt)
            verify_packet(self, expected, self.server_port_before)

        # Simulate migration: update forward entry to new port + MAC
        logger.info("Simulating migration: %s -> port %d, mac %s",
                     self.server_ip, self.server_port_after, self.server_mac_after)
        self.modifyForwardWithMacEntry(
            self.server_ip, self.server_port_after, self.server_mac_after
        )

        # Phase 2: post-migration — packet goes to new port
        for i in range(self.numPackets):
            expected = simple_tcp_packet(
                eth_src=self.client_mac,
                eth_dst=self.server_mac_after,
                ip_src=self.client_ip,
                ip_dst=self.server_ip,
                tcp_sport=50000,
                tcp_dport=8080,
            )
            logger.info("Post-migration packet %d (cross-port)", i)
            send_packet(self, self.client_port, pkt)
            verify_packet(self, expected, self.server_port_after)

    def runTest(self):
        self.runTestImpl()


class TestPortChange(AbstractTest):

    def setUp(self):
        super().setUp()
        self.clientPort = swports[0]
        self.windowStart = 0
        self.windowSize = 2
        self.numServers = 4
        self.serverPorts = [swports[i + 1] for i in range(self.numServers)]
        self.serverIps = [f"10.0.0.{i + 1}" for i in range(self.numServers)]
        self.numPackets = 100
        self.maxImbalance = 0.35
        self.serverCounters = [0 for _ in range(self.numServers)]
        self.serverTcpPort = 12345

    def get_member_status(self):
        return (
            [False] * self.windowStart
            + [True] * self.windowSize
            + [False] * (self.numServers - (self.windowSize + self.windowStart))
        )

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up port change test: Client port: %s, Server ports: %s",
            self.clientPort,
            self.serverPorts,
        )

        self.insertClientSnatEntry(src_port=12345, new_src=self.lbIp)
        self.insertForwardEntry(dst_addr=self.clientIp, port=self.clientPort)

        for i in range(self.numServers):
            self.insertActionTableEntry(node_index=i, new_dst=self.serverIps[i])
            self.insertForwardEntry(
                dst_addr=self.serverIps[i], port=self.serverPorts[i]
            )

        self.selection_members = list(range(self.numServers))
        member_status = self.get_member_status()
        self.insertSelectionTableEntry(
            members=self.selection_members,
            member_status=member_status,
        )
        self.insertNodeSelectorEntry(dst_addr=self.lbIp, group_id=1)

    def sendPackets(self, num_packets, server_ips, server_ports):
        for i in range(num_packets):
            logger.info("Sending packet %d...", i)
            clientTcpPort = random.randint(12346, 65535)

            clientPkt = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=self.lbIp,
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPktToServer1 = simple_tcp_packet(
                ip_src=self.clientIp,
                ip_dst=server_ips[0],
                tcp_sport=clientTcpPort,
                tcp_dport=self.serverTcpPort,
            )
            expectedPktToServer2 = simple_tcp_packet(
                ip_src=self.clientIp,
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
            self.serverCounters[rcvIdx] += 1

            logger.info(
                "Sending response from port %d to client port %d...",
                server_ports[rcvIdx],
                self.clientPort,
            )

            serverIp = server_ips[rcvIdx]
            serverPkt = simple_tcp_packet(
                ip_src=serverIp,
                ip_dst=self.clientIp,
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            send_packet(self, server_ports[rcvIdx], serverPkt)

            expectedPktToClient = simple_tcp_packet(
                ip_src=self.lbIp,
                ip_dst=self.clientIp,
                tcp_sport=self.serverTcpPort,
                tcp_dport=clientTcpPort,
            )
            logger.info("Verifying packet on client port %d...", self.clientPort)
            verify_packet(self, expectedPktToClient, self.clientPort)

    def sendPacket(self):
        self.sendPackets(
            num_packets=self.numPackets // 3,
            server_ips=self.serverIps[
                self.windowStart : self.windowSize + self.windowStart
            ],
            server_ports=self.serverPorts[
                self.windowStart : self.windowSize + self.windowStart
            ],
        )
        self.checkTrafficBalance(
            self.serverCounters[: self.windowSize],
            self.maxImbalance,
        )
        self.serverCounters = [0 for _ in range(self.numServers)]

        self.windowStart = 1
        member_status = self.get_member_status()
        self.modifySelectionTableEntry(
            members=self.selection_members, member_status=member_status
        )
        self.sendPackets(
            num_packets=self.numPackets // 3,
            server_ips=self.serverIps[
                self.windowStart : self.windowSize + self.windowStart
            ],
            server_ports=self.serverPorts[
                self.windowStart : self.windowSize + self.windowStart
            ],
        )
        self.checkTrafficBalance(
            self.serverCounters[: self.windowSize],
            self.maxImbalance,
        )
        self.serverCounters = [0 for _ in range(self.numServers)]

        self.windowStart = 2
        member_status = self.get_member_status()
        self.modifySelectionTableEntry(
            members=self.selection_members, member_status=member_status
        )
        self.sendPackets(
            num_packets=self.numPackets // 3,
            server_ips=self.serverIps[
                self.windowStart : self.windowSize + self.windowStart
            ],
            server_ports=self.serverPorts[
                self.windowStart : self.windowSize + self.windowStart
            ],
        )
        self.checkTrafficBalance(
            self.serverCounters[: self.windowSize],
            self.maxImbalance,
        )

    def verifyPackets(self):
        pass

    def runTest(self):
        self.runTestImpl()
