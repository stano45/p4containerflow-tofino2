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
        service_port: int,
    ):
        super().__init__(
            sw_name=sw_name,
            sw_addr=sw_addr,
            sw_id=sw_id,
            client_id=client_id,
            load_balancer_ip=load_balancer_ip,
            service_port=service_port,
        )

        self.logger = logger
        logger.info("Establishing connection with %s", self.sw_addr)
        self.interface = gc.ClientInterface(
            self.sw_addr,
            client_id=client_id,
            device_id=0,
            notifications=None,
            perform_subscribe=True,
        )

        if not sw_name:
            self.bfrt_info = self.interface.bfrt_info_get()
            self.sw_name = self.bfrt_info.p4_name_get()

        self.interface.bind_pipeline_config(self.sw_name)
        # Set target to all pipes on device self.sw_id.
        self.target = gc.Target(device_id=self.sw_id, pipe_id=0xFFFF)
        self.bfrt_info = self.interface.bfrt_info_get(self.sw_name)

    def __del__(self):
        pass

    def insertTableEntry(
        self, tableName: str, keyFields=None, actionName=None, dataFields=[]
    ):
        testTable = self.bfrt_info.table_get(tableName)
        keyList = [testTable.make_key(keyFields)]
        dataList = [testTable.make_data(dataFields, actionName)]
        testTable.entry_add(self.target, keyList, dataList)

    def modifyTableEntry(
        self, tableName: str, keyFields=None, actionName=None, dataFields=[]
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

    def insertNodeSelectorEntry(
        self,
        dst_addr: str,
        group_id: int = 1,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            tableName="SwitchIngress.node_selector",
            keyFields=[gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
            dataFields=[
                gc.DataTuple("$SELECTOR_GROUP_ID", group_id),
            ],
        )

    def insertSelectionTableEntry(
        self,
        members: list[int],
        member_status: list[bool],
        group_id: int = 1,
        max_grp_size: int = 4,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            tableName="SwitchIngress.action_selector",
            keyFields=[gc.KeyTuple("$SELECTOR_GROUP_ID", group_id)],
            dataFields=[
                gc.DataTuple("$MAX_GROUP_SIZE", max_grp_size),
                gc.DataTuple("$ACTION_MEMBER_ID", int_arr_val=members),
                gc.DataTuple("$ACTION_MEMBER_STATUS", bool_arr_val=member_status),
            ],
        )

    def insertActionTableEntry(
        self,
        node_index: int,
        new_dst: str,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            "SwitchIngress.action_selector_ap",
            [gc.KeyTuple("$ACTION_MEMBER_ID", node_index)],
            "SwitchIngress.set_rewrite_dst",
            [gc.DataTuple("new_dst", gc.ipv4_to_bytes(new_dst))],
        )

    def insertClientSnatEntry(
        self,
        src_port: int,
        new_src: str,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            "SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
            "SwitchIngress.set_rewrite_src",
            [gc.DataTuple("new_src", gc.ipv4_to_bytes(new_src))],
        )

    def insertForwardEntry(
        self,
        dst_addr: str,
        port: int,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            "SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
            "SwitchIngress.set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )
