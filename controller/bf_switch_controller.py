from abstract_switch_controller import AbstractSwitchController
import bfrt_grpc.client as gc

from utils import ip_to_int


class SwitchController(AbstractSwitchController):
    def __init__(
        self,
        logger,
        sw_name,
        sw_addr,
        sw_id,
        client_id,
        load_balancer_ip,
    ):
        super().__init__(sw_name, sw_addr, sw_id, client_id, load_balancer_ip)

        self.logger = logger
        logger.info("Establishing connection with %s", self.sw_addr)
        self.interface = gc.ClientInterface(
            self.sw_addr,
            client_id=client_id,
            device_id=0,
            notifications=None,
            perform_subscribe=True,
        )

        self.bfrt_info = self.interface.bfrt_info_get()
        if not sw_name:
            self.sw_name = self.bfrt_info.p4_name_get()
        self.interface.bind_pipeline_config(self.sw_name)
        # Set target to all pipes on device self.sw_id.
        self.target = gc.Target(device_id=self.sw_id, pipe_id=0xFFFF)

    def __del__(self):
        pass

    def insertTableEntry(
        self, tableName, keyFields=None, actionName=None, dataFields=[]
    ):
        testTable = self.bfrt_info.table_get(tableName)
        keyList = [testTable.make_key(keyFields)]
        dataList = [testTable.make_data(dataFields, actionName)]
        testTable.entry_add(self.target, keyList, dataList)

    def modifyTableEntry(
        self, tableName, keyFields=None, actionName=None, dataFields=[]
    ):
        testTable = self.bfrt_info.table_get(tableName)
        keyList = [testTable.make_key(keyFields)]
        dataList = [testTable.make_data(dataFields, actionName)]
        testTable.entry_mod(self.target, keyList, dataList)

    def getUpdateFn(self, update_type):
        updateFn = None
        if update_type == "INSERT":
            updateFn = self.insertTableEntry
        else:
            updateFn = self.modifyTableEntry
        return updateFn

    def insertEcmpGroupSelectEntry(
        self, matchDstAddr, ecmp_base, ecmp_count, update_type="INSERT"
    ):
        self.getUpdateFn(update_type)(
            "SwitchIngress.ecmp_group",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip_to_int(matchDstAddr), prefix_len=0)],
            "NoAction",
            [],
        )

    def insertEcmpGroupRewriteSrcEntry(
        self, matchDstAddr, new_src, update_type="INSERT"
    ):
        self.getUpdateFn(update_type)(
            "SwitchIngress.ecmp_group",
            [gc.KeyTuple("hdr.ipv4.dst_addr", ip_to_int(matchDstAddr), prefix_len=0)],
            "set_rewrite_src",
            [gc.DataTuple("new_src", ip_to_int(new_src))],
        )

    def insertEcmpNhopEntry(self, ecmp_select, dmac, ipv4, port, update_type="INSERT"):
        self.getUpdateFn(update_type)(
            "SwitchIngress.ecmp_nhop",
            [gc.KeyTuple("ig_md.ecmp_select", ecmp_select)],
            "set_ecmp_nhop",
            [
                gc.DataTuple("nhop_ipv4", ip_to_int(ipv4)),
                gc.DataTuple("port", port),
            ],
        )

    def deleteEcmpNhopEntry(self, ecmp_select):
        pass

    def insertSendFrameEntry(self, egress_port, smac, update_type="INSERT"):
        pass

    def deleteSendFrameEntry(self, egress_port):
        pass

    def readTableRules(self):
        pass
