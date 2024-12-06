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
        self.table_entries = []
        self.bfrt_info = None
        # Get bfrt_info and set it as part of the test.
        self.bfrt_info = self.interface.bfrt_info_get("t2na_load_balancer")

        # Set target to all pipes on device self.dev_id.
        self.target = gc.Target(device_id=0, pipe_id=0xFFFF)

    def tearDown(self):
        # Reset tables.
        for elt in reversed(self.table_entries):
            test_table = self.bfrt_info.table_get(elt[0])
            test_table.entry_del(self.target, elt[1])
        self.table_entries = []

        # End session.
        BfRuntimeTest.tearDown(self)

    def insertTableEntry(
        self, table_name, key_fields=None, action_name=None, data_fields=[]
    ):
        test_table = self.bfrt_info.table_get(table_name)
        key_list = [test_table.make_key(key_fields)]
        data_list = [test_table.make_data(data_fields, action_name)]
        test_table.entry_add(self.target, key_list, data_list)
        self.table_entries.append((table_name, key_list))

    def deleteTableEntries(self, table_name):
        test_table = self.bfrt_info.table_get(table_name)
        test_table.entry_del(self.target)
        self.table_entries.clear()

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

    def setDirectRegisterValue(self, tbl_name, value):
        test_table = self.bfrt_info.table_get(tbl_name)
        table_id = test_table.info.id
        req = bfruntime_pb2.ReadRequest()
        req.client_id = self.client_id
        gc._cpy_target(req, self.target)
        entity = req.entities.add()
        table = entity.table_entry
        table.table_id = table_id
        table_entry = None
        for response in self._responseDumpHelper(req):
            for entity in response.entities:
                assert entity.WhichOneof("entity") == "table_entry"
                table_entry = entity.table_entry
                break
        if table_entry is None:
            raise self.failureException(
                "No entry in the table that the meter is attached to."
            )
        table_entry.ClearField("data")
        value_list = []
        if isinstance(value, list):
            for val in value:
                df = table_entry.data.fields.add()
        else:
            df = table_entry.data.fields.add()
            df.value = gc.DataTuple(gc.DataTuple("f1", value))
        self.entryAdd(test_table, self.target, table_entry)

    def setupCtrlPlane(self):
        pass

    def sendPacket(self):
        pass

    def verifyPackets(self):
        pass

    def cleanupCtrlPlane(self):
        self.deleteTableEntries("SwitchIngress.ecmp_group")
        self.deleteTableEntries("SwitchIngress.ecmp_nhop")

    def runTestImpl(self):
        self.setupCtrlPlane()
        logger.info("Sending Packet ...")
        self.sendPacket()
        logger.info("Verifying Packet ...")
        self.verifyPackets()
        logger.info("Verifying no other packets ...")
        verify_no_other_packets(self, self.dev_id, timeout=2)
        logger.info("Cleaning up control plane...")
        self.cleanupCtrlPlane()
        logger.info("Done!")


class TestRewriteSource(AbstractTest):
    # Test rewriting source IP when server sends packet to client
    # this is essentially NAT

    client_port = swports[0]
    server_port = swports[1]

    def setupCtrlPlane(self):
        self.cleanupCtrlPlane()

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
            [gc.DataTuple("nhop_ipv4", ip("10.0.0.0")), gc.DataTuple("port", self.client_port)],
        )

    def cleanupCtrlPlane(self):
        return super().cleanupCtrlPlane()

    def sendPacket(self):
        server_pkt = simple_tcp_packet(
            ip_src="10.0.0.2",
            ip_dst="10.0.0.0",
            tcp_sport="12345",
            tcp_dport="6789",
            with_tcp_chksum=True,
            ip_ttl=64
        )
        send_packet(self, self.server_port, server_pkt)

    def verifyPackets(self):
        expected_pkt_to_client = simple_tcp_packet(
            ip_src="10.0.0.1",
            ip_dst="10.0.0.0",
            tcp_sport="12345",
            tcp_dport="6789",
            with_tcp_chksum=True,
            ip_ttl=63
        )
        verify_packet(self, expected_pkt_to_client, self.client_port)

    def runTest(self):
        self.runTestImpl()


class TestForwarding(AbstractTest):
    client_port = swports[0]
    server_port = swports[1]

    def setupCtrlPlane(self):
        self.cleanupCtrlPlane()

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
            [gc.DataTuple("nhop_ipv4", ip("10.0.0.2")), gc.DataTuple("port", self.server_port)],
        )
        self.insertTableEntry(
            "SwitchIngress.ecmp_nhop",
            [
                gc.KeyTuple("ig_md.ecmp_select", 2),
            ],
            "set_ecmp_nhop",
            [gc.DataTuple("nhop_ipv4", ip("10.0.0.2")), gc.DataTuple("port", self.server_port)],
        )
        

    def cleanupCtrlPlane(self):
        return super().cleanupCtrlPlane()

    def sendPacket(self):
        server_pkt = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.1",
            tcp_sport="12345",
            tcp_dport="6789",
            with_tcp_chksum=True,
            ip_ttl=64
        )
        send_packet(self, self.client_port, server_pkt)

    def verifyPackets(self):
        expected_pkt_to_server = simple_tcp_packet(
            ip_src="10.0.0.0",
            ip_dst="10.0.0.2",
            tcp_sport="12345",
            tcp_dport="6789",
            with_tcp_chksum=True,
            ip_ttl=63

        )
        verify_packet(self, expected_pkt_to_server, self.server_port)

    def runTest(self):
        self.runTestImpl()
