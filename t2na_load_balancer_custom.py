import logging
import itertools

from bfruntime_client_base_tests import BfRuntimeTest
from ptf.mask import Mask
from ptf.testutils import send_packet
from ptf.testutils import verify_packet
from ptf.testutils import verify_no_other_packets

from p4testutils.misc_utils import *

import bfrt_grpc.bfruntime_pb2 as bfruntime_pb2
import bfrt_grpc.client as gc

import random
from ipaddress import ip_address

logger = get_logger()
swports = get_sw_ports()

print("SW Ports: ", swports)


def ip(ip_string) -> int:
    return int(ip_address(ip_string))


class AbstractTest(BfRuntimeTest):
    def setUp(self):
        BfRuntimeTest.setUp(self, 0, "t2na_load_balancer")
        self.dev_id = 0
        self.table_entries = {}
        self.bfrt_info = None
        # Get bfrt_info and set it as part of the test.
        self.bfrt_info = self.interface.bfrt_info_get("t2na_load_balancer")

        # Set target to all pipes on device self.dev_id.
        self.target = gc.Target(device_id=0, pipe_id=0xFFFF)

    def clearTables(self):
        for table_name, keys in reversed(self.table_entries.items()):
            test_table = self.bfrt_info.table_get(table_name)
            test_table.entry_del(self.target, keys)
        self.table_entries = {}

    def tearDown(self):
        self.clearTables()
        BfRuntimeTest.tearDown(self)

    def insertTableEntry(
        self, table_name, key_fields=None, action_name=None, data_fields=[]
    ):
        test_table = self.bfrt_info.table_get(table_name)
        key_list = [test_table.make_key(key_fields)]
        data_list = [test_table.make_data(data_fields, action_name)]
        test_table.entry_add(self.target, key_list, data_list)
        existing_entries =  self.table_entries[table_name] if table_name in self.table_entries else []
        existing_entries.extend(key_list)
        self.table_entries[table_name] = existing_entries

    def modifyTableEntry(
        self, table_name, key_fields=None, action_name=None, data_fields=[]
    ):
        test_table = self.bfrt_info.table_get(table_name)
        key_list = [test_table.make_key(key_fields)]
        data_list = [test_table.make_data(data_fields, action_name)]
        test_table.entry_mod(self.target, key_list, data_list)
        existing_entries = self.table_entries[table_name] if table_name in self.table_entries else []
        existing_entries.extend(key_list)
        self.table_entries[table_name] = list(set(existing_entries))


    def _responseDumpHelper(self, request):
        for response in self.interface.stub.Read(request, timeout=2):
            yield response

    def overrideDefaultEntry(self, table_name, action_name=None, data_fields=[]):
        test_table = self.bfrt_info.table_get(table_name)
        data = test_table.make_data(data_fields, action_name)
        test_table.default_entry_set(self.target, data)

    def setRegisterValue(self, reg_name, value, index):
        reg_table = self.bfrt_info.table_get(reg_name)
        key_list = [reg_table.make_key([gc.KeyTuple("$REGISTER_INDEX", index)])]
        value_list = []
        if isinstance(value, list):
            for val in value:
                value_list.append(gc.DataTuple(val[0], val[1]))
        else:
            value_list.append(gc.DataTuple("f1", value))
        reg_table.entry_add(self.target, key_list, [reg_table.make_data(value_list)])

    def entryAdd(self, table_obj, target, table_entry):
        req = bfruntime_pb2.WriteRequest()
        gc._cpy_target(req, target)
        req.atomicity = bfruntime_pb2.WriteRequest.CONTINUE_ON_ERROR
        update = req.updates.add()
        update.type = bfruntime_pb2.Update.MODIFY
        update.entity.table_entry.CopyFrom(table_entry)
        resp = self.interface.reader_writer_interface._write(req)
        table_obj.get_parser._parse_entry_write_response(resp)


    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass

    def runTestImpl(self):
        self.setupCtrlPlane()
        logger.info("Sending Packet ...")
        self.sendPacket()
        logger.info("Verifying Packet ...")
        self.verifyPackets()
        logger.info("Verifying no other packets ...")
        verify_no_other_packets(self, self.dev_id, timeout=2)
        logger.info("Tearing down suite...")
        self.tearDown()
        logger.info("Done!")


class TestRewriteSource(AbstractTest):
    # Test rewriting source IP when server sends packet to client
    # this is essentially NAT

    client_port = swports[0]
    server_port = swports[1]

    def clearTables(self):
        return super().clearTables()

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Using client port: %s and server port: %s ",
            self.client_port,
            self.server_port,
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                # Client IP
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.0"), prefix_len=0),
            ],
            "set_rewrite_src",
            [
                # Load Balancer IP
                gc.DataTuple("new_src", ip("10.0.0.1"))
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 0),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.0")),
                gc.DataTuple("port", self.client_port),
            ],
        )

    def sendPacket(self):
        server_pkt = simple_tcp_packet(
            ip_src="10.0.0.2",
            ip_dst="10.0.0.0",
        )
        send_packet(self, self.server_port, server_pkt)

    def verifyPackets(self):
        expected_pkt_to_client = simple_tcp_packet(
            ip_src="10.0.0.1",
            ip_dst="10.0.0.0",
        )
        verify_packet(self, expected_pkt_to_client, self.client_port)

    def runTest(self):
        self.runTestImpl()


class TestForwarding(AbstractTest):
    client_port = swports[0]
    server_port = swports[1]

    def clearTables(self):
        return super().clearTables()

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Using client port: %s and server port: %s ",
            self.client_port,
            self.server_port,
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                # Server IP
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.1"), prefix_len=0),
            ],
            "NoAction",
            [],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 1),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.2")),
                gc.DataTuple("port", self.server_port),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.2")),
                gc.DataTuple("port", self.server_port),
            ],
        )
    def sendPacket(self):
        server_pkt = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.1",
        )
        send_packet(self, self.client_port, server_pkt)

    def verifyPackets(self):
        expected_pkt_to_server = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.2",
        )
        verify_packet(self, expected_pkt_to_server, self.server_port)

    def runTest(self):
        self.runTestImpl()


class TestEvenTrafficBalancingToServer(AbstractTest):
    # Test even traffic balancing from one client to two servers

    def setUp(self):
        super().setUp()
        self.client_port = swports[0]
        self.server_ports = [swports[1], swports[2]]
        self.num_packets = 200
        # Stay safe to avoid flaky tests
        # This should already prove the load balancer works
        self.max_imbalance_percent = 20
        self.server1_counter = 0
        self.server2_counter = 0


    def clearTables(self):
        return super().clearTables()

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.client_port,
            self.server_ports,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.1"), prefix_len=32),
            ],
            "NoAction",
            [],
        )

        # Add next-hop entries for both server IPs (10.0.0.2 and 10.0.0.3) and their respective ports
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 1),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.2")),
                gc.DataTuple("port", self.server_ports[0]),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.3")),
                gc.DataTuple("port", self.server_ports[1]),
            ],
        )

    def sendPacket(self):
        # Generate and send multiple packets from client to load balancer
        for i in range(self.num_packets):
            logger.info("Sending packet %d...", i)
            src_port = random.randint(1024, 65535)  # Random source port

            client_pkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=src_port,  # Set the random source port
            )
            send_packet(self, self.client_port, client_pkt)

            expected_pkt_to_server1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.2",
                tcp_sport=src_port,
            )
            expected_pkt_to_server2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.3",
                tcp_sport=src_port,
            )
            logger.info("Verifying packet %d...", i)
            rcv_idx = verify_any_packet_any_port(
                self, [expected_pkt_to_server1, expected_pkt_to_server2], self.server_ports
            )
            logger.info("Packet %d received on port %d...", i, self.server_ports[rcv_idx])
            if rcv_idx == 0:
                self.server1_counter += 1
            else:
                self.server2_counter += 1


    def verifyPackets(self):
        total_packets = self.server1_counter + self.server2_counter
        imbalance_percentage = abs(self.server1_counter - self.server2_counter) / total_packets * 100

        logger.info(
            "Traffic distribution: Server 1: %d packets, Server 2: %d packets, Imbalance: %.2f%%",
            self.server1_counter,
            self.server2_counter,
            imbalance_percentage,
        )
        assert imbalance_percentage <= self.max_imbalance_percent, f"Traffic imbalance too high: {imbalance_percentage:.2f}%"

    def runTest(self):
        self.runTestImpl()


class TestBidirectionalTraffic(AbstractTest):
    # Test even traffic balancing from one client to two servers, and traffic from servers back to client

    def setUp(self):
        super().setUp()
        self.client_port = swports[0]
        self.server_ports = [swports[1], swports[2]]
        self.num_packets = 200
        # Stay safe to avoid flaky tests
        # This should already prove the load balancer works
        self.max_imbalance_percent = 20
        self.server1_counter = 0
        self.server2_counter = 0
        self.server_tcp_port = 25565

    def clearTables(self):
        return super().clearTables()

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.client_port,
            self.server_ports,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.1"), prefix_len=32),
            ],
            "NoAction",
            [],
        )

        # Rewrite source to load balancer ip on server -> client traffic
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                # Client IP
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.0"), prefix_len=0),
            ],
            "set_rewrite_src",
            [
                # Load Balancer IP
                gc.DataTuple("new_src", ip("10.0.0.1"))
            ],
        )

        # Add next-hop entries for both server IPs (10.0.0.2 and 10.0.0.3) and their respective ports
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 0),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.0")),
                gc.DataTuple("port", self.client_port),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 1),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.2")),
                gc.DataTuple("port", self.server_ports[0]),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.3")),
                gc.DataTuple("port", self.server_ports[1]),
            ],
        )

    def sendPacket(self):
        # Generate and send multiple packets from client to load balancer
        for i in range(self.num_packets):
            logger.info("Sending packet %d...", i)
            # Random source port
            client_tcp_port = random.randint(1024, 65535)

            client_pkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=client_tcp_port, 
                tcp_dport=self.server_tcp_port
            )
            send_packet(self, self.client_port, client_pkt)

            expected_pkt_to_server1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.2",
                tcp_sport=client_tcp_port,
                tcp_dport=self.server_tcp_port,
            )
            expected_pkt_to_server2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.3",
                tcp_sport=client_tcp_port,
                tcp_dport=self.server_tcp_port,
            )
            logger.info("Verifying packet %d...", i)
            rcv_idx = verify_any_packet_any_port(
                self, [expected_pkt_to_server1, expected_pkt_to_server2], self.server_ports
            )
            logger.info("Packet %d received on port %d...", i, self.server_ports[rcv_idx])

            if rcv_idx == 0:
                self.server1_counter += 1
            else:
                self.server2_counter += 1
                
            logger.info("Sending response from port %d to client port %d...", self.server_ports[rcv_idx], self.client_port)

            server_pkt = simple_tcp_packet(
                ip_src="10.0.0.2" if rcv_idx == 0 else "10.0.0.3",
                ip_dst="10.0.0.0",
                tcp_sport=self.server_tcp_port,
                tcp_dport=client_tcp_port
            )
            send_packet(self, self.server_ports[rcv_idx], server_pkt)

            expected_pkt_to_client  = simple_tcp_packet(
                ip_src="10.0.0.1",
                ip_dst="10.0.0.0",
                tcp_sport=self.server_tcp_port,
                tcp_dport=client_tcp_port
            )
            logger.info("Verifying packet on client port %d...", self.client_port)
            verify_packet(self, expected_pkt_to_client, self.client_port)


    def verifyPackets(self):
        total_packets = self.server1_counter + self.server2_counter
        imbalance_percentage = abs(self.server1_counter - self.server2_counter) / total_packets * 100

        logger.info(
            "Traffic distribution: Server 1: %d packets, Server 2: %d packets, Imbalance: %.2f%%",
            self.server1_counter,
            self.server2_counter,
            imbalance_percentage,
        )
        assert imbalance_percentage <= self.max_imbalance_percent, f"Traffic imbalance too high: {imbalance_percentage:.2f}%"

    def runTest(self):
        self.runTestImpl()


class TestPortChange(AbstractTest):
    # First test normal load balancing, change one port, test again, change the second one, test again,
    # verify whether load balancing still works
    def setUp(self):
        super().setUp()
        self.client_port = swports[0]
        self.server_ports = [swports[1], swports[2], swports[3], swports[4]]
        self.num_packets = 300
        # Stay safe to avoid flaky tests
        # This should already prove the load balancer works
        self.max_imbalance_percent = 20
        self.server1_counter = 0
        self.server2_counter = 0
        self.server_tcp_port = 25565

    def clearTables(self):
        return super().clearTables()

    def setupCtrlPlane(self):
        self.clearTables()

        logger.info(
            "Setting up even traffic balancing: Client port: %s, Server ports: %s",
            self.client_port,
            self.server_ports,
        )

        # Insert ECMP group entry pointing to the load balancer IP (10.0.0.1)
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.1"), prefix_len=32),
            ],
            "NoAction",
            [],
        )

        # Rewrite source to load balancer ip on server -> client traffic
        self.insertTableEntry(
            "SwitchIngress.ecmp_group",
            [
                # Client IP
                gc.KeyTuple("hdr.ipv4.dst_addr", ip("10.0.0.0"), prefix_len=0),
            ],
            "set_rewrite_src",
            [
                # Load Balancer IP
                gc.DataTuple("new_src", ip("10.0.0.1"))
            ],
        )

        # Add next-hop entries for both server IPs (10.0.0.2 and 10.0.0.3) and their respective ports
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 0),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.0")),
                gc.DataTuple("port", self.client_port),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 1),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.2")),
                gc.DataTuple("port", self.server_ports[0]),
            ],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.3")),
                gc.DataTuple("port", self.server_ports[1]),
            ],
        )

    def sendPackets(self, num_packets, server_ips, server_ports):
        for i in range(num_packets):
            logger.info("Sending packet %d...", i)
            # Random source port
            client_tcp_port = random.randint(1024, 65535)

            client_pkt = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst="10.0.0.1",
                tcp_sport=client_tcp_port, 
                tcp_dport=self.server_tcp_port
            )
            send_packet(self, self.client_port, client_pkt)

            expected_pkt_to_server1 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst=server_ips[0],
                tcp_sport=client_tcp_port,
                tcp_dport=self.server_tcp_port,
            )
            expected_pkt_to_server2 = simple_tcp_packet(
                ip_src="10.0.0.0",
                ip_dst=server_ips[1],
                tcp_sport=client_tcp_port,
                tcp_dport=self.server_tcp_port,
            )

            logger.info("Verifying packet %d...", i)
            rcv_idx = verify_any_packet_any_port(
                self, [expected_pkt_to_server1, expected_pkt_to_server2], server_ports
            )
            logger.info("Packet %d received on port %d...", i, server_ports[rcv_idx])

            if rcv_idx == 0:
                self.server1_counter += 1
            else:
                self.server2_counter += 1
                
            logger.info("Sending response from port %d to client port %d...", self.server_ports[rcv_idx], self.client_port)

            server_pkt = simple_tcp_packet(
                ip_src=server_ips[rcv_idx],
                ip_dst="10.0.0.0",
                tcp_sport=self.server_tcp_port,
                tcp_dport=client_tcp_port
            )
            send_packet(self, server_ports[rcv_idx], server_pkt)

            expected_pkt_to_client  = simple_tcp_packet(
                ip_src="10.0.0.1",
                ip_dst="10.0.0.0",
                tcp_sport=self.server_tcp_port,
                tcp_dport=client_tcp_port
            )
            logger.info("Verifying packet on client port %d...", self.client_port)
            verify_packet(self, expected_pkt_to_client, self.client_port)


    def sendPacket(self):
        self.sendPackets(self.num_packets//3, ["10.0.0.2", "10.0.0.3"], self.server_ports[:2])
        self.checkBalance()

        self.server1_counter = 0
        self.server2_counter = 0
        self.modifyTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 1),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.4")),
                gc.DataTuple("port", self.server_ports[2]),
            ],
        )
        self.sendPackets(self.num_packets//3, ["10.0.0.4", "10.0.0.3"], [self.server_ports[2], self.server_ports[1]])
        self.checkBalance()

        self.server1_counter = 0
        self.server2_counter = 0
        self.modifyTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip("10.0.0.5")),
                gc.DataTuple("port", self.server_ports[3]),
            ],
        )
        self.sendPackets(self.num_packets//3, ["10.0.0.4", "10.0.0.5"], self.server_ports[2:4])
        self.checkBalance()



    def checkBalance(self):
        total_packets = self.server1_counter + self.server2_counter
        imbalance_percentage = abs(self.server1_counter - self.server2_counter) / total_packets * 100

        logger.info(
            "Traffic distribution: Server 1: %d packets, Server 2: %d packets, Imbalance: %.2f%%",
            self.server1_counter,
            self.server2_counter,
            imbalance_percentage,
        )
        assert imbalance_percentage <= self.max_imbalance_percent, f"Traffic imbalance too high: {imbalance_percentage:.2f}%"
   
    def verifyPackets(self):
        pass
   
    def runTest(self):
        self.runTestImpl()
