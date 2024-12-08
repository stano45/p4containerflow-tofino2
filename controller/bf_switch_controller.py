from logging import Logger
from abstract_switch_controller import AbstractSwitchController
import bfrt_grpc.client as gc

from internal_types import UpdateType


class SwitchController(AbstractSwitchController):
    def __init__(
        self,
        logger: Logger,
        sw_name: str,
        sw_addr: str,
        sw_id: str | int,
        client_id: str | int,
        load_balancer_ip: str,
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

    def getUpdateFn(self, update_type: UpdateType):
        updateFn = None
        match update_type:
            case UpdateType.INSERT:
                updateFn = self.insertTableEntry
            case UpdateType.MODIFY:
                updateFn = self.modifyTableEntry
            case _:
                updateFn = self.insertTableEntry
        return updateFn

    def insertEcmpGroupSelectEntry(
        self, matchDstAddr, ecmp_base, ecmp_count, update_type: UpdateType
    ):
        self.getUpdateFn(update_type)(
            tableName="SwitchIngress.ecmp_group",
            keyFields=[
                gc.KeyTuple(
                    name="hdr.ipv4.dst_addr",
                    value=gc.ipv4_to_bytes(matchDstAddr),
                    prefix_len=32,
                )
            ],
            actionName="NoAction",
            dataFields=[],
        )

    def insertEcmpGroupRewriteSrcEntry(
        self, matchDstAddr, new_src, update_type: UpdateType
    ):
        self.getUpdateFn(update_type)(
            tableName="SwitchIngress.ecmp_group",
            keyFields=[
                gc.KeyTuple(
                    name="hdr.ipv4.dst_addr",
                    value=gc.ipv4_to_bytes(matchDstAddr),
                    prefix_len=0,
                )
            ],
            actionName="set_rewrite_src",
            dataFields=[gc.DataTuple(name="new_src", val=gc.ipv4_to_bytes(new_src))],
        )

    def insertEcmpNhopEntry(
        self, ecmp_select, dmac, ipv4, port, update_type: UpdateType
    ):
        self.getUpdateFn(update_type)(
            tableName="SwitchIngress.ecmp_nhop",
            keyFields=[gc.KeyTuple(name="ig_md.ecmp_select", value=ecmp_select)],
            actionName="set_ecmp_nhop",
            dataFields=[
                gc.DataTuple(name="nhop_ipv4", val=gc.ipv4_to_bytes(ipv4)),
                gc.DataTuple(name="port", val=port),
            ],
        )

    def deleteEcmpNhopEntry(self, ecmp_select):
        pass

    def insertSendFrameEntry(self, egress_port, smac, update_type: UpdateType):
        pass

    def deleteSendFrameEntry(self, egress_port):
        pass

    def readTableRules(self):
        pass
