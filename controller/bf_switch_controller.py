from logging import Logger
import time
from abstract_switch_controller import AbstractSwitchController
import bfrt_grpc.client as gc

from internal_types import UpdateType


def connect_with_retry(
    logger: Logger,
    sw_addr: str,
    client_id: int,
    device_id: int = 0,
    num_tries: int = 10,
    retry_delay: float = 2.0,
) -> gc.ClientInterface:
    """
    Attempt to connect to the gRPC server with proper retries.
    Each retry creates a fresh connection attempt.
    """
    last_error = None
    for attempt in range(1, num_tries + 1):
        try:
            logger.info("Connection attempt #%d to %s", attempt, sw_addr)
            interface = gc.ClientInterface(
                sw_addr,
                client_id=client_id,
                device_id=device_id,
                notifications=None,
                perform_subscribe=True,
            )
            logger.info("Successfully connected to %s", sw_addr)
            return interface
        except Exception as e:
            last_error = e
            logger.warning(
                "Connection attempt #%d failed: %s. Retrying in %.1fs...",
                attempt,
                str(e),
                retry_delay,
            )
            time.sleep(retry_delay)

    raise RuntimeError(
        f"Failed to connect to {sw_addr} after {num_tries} attempts: {last_error}"
    )


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
        num_tries: int = 10,
        retry_delay: float = 2.0,
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
        self.interface = connect_with_retry(
            logger=logger,
            sw_addr=self.sw_addr,
            client_id=client_id,
            device_id=0,
            num_tries=num_tries,
            retry_delay=retry_delay,
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
            tableName="pipe.SwitchIngress.node_selector",
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
            tableName="pipe.SwitchIngress.action_selector",
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
            "pipe.SwitchIngress.action_selector_ap",
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
            "pipe.SwitchIngress.client_snat",
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
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
            "SwitchIngress.set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )

    def insertArpForwardEntry(
        self,
        target_ip: str,
        port: int,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        self.getUpdateFn(update_type)(
            "pipe.SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(target_ip))],
            "SwitchIngress.set_egress_port",
            [
                gc.DataTuple("port", port),
            ],
        )

    def deleteArpForwardEntry(self, target_ip: str):
        self.deleteTableEntry(
            "pipe.SwitchIngress.arp_forward",
            [gc.KeyTuple("hdr.arp.target_proto_addr", gc.ipv4_to_bytes(target_ip))],
        )

    def deleteTableEntry(self, tableName: str, keyFields=None):
        """Delete a specific table entry by key."""
        table = self.bfrt_info.table_get(tableName)
        if keyFields:
            keyList = [table.make_key(keyFields)]
            table.entry_del(self.target, keyList)
        else:
            # Delete all entries if no key specified
            table.entry_del(self.target)

    def deleteForwardEntry(self, dst_addr: str):
        """Delete a forward table entry."""
        self.deleteTableEntry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
        )

    def deleteClientSnatEntry(self, src_port: int):
        """Delete a client SNAT table entry."""
        self.deleteTableEntry(
            "pipe.SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
        )

    def deleteActionTableEntry(self, node_index: int):
        """Delete an action table entry."""
        self.deleteTableEntry(
            "pipe.SwitchIngress.action_selector_ap",
            [gc.KeyTuple("$ACTION_MEMBER_ID", node_index)],
        )

    def deleteSelectionTableEntry(self, group_id: int = 1):
        """Delete a selection table entry."""
        self.deleteTableEntry(
            "pipe.SwitchIngress.action_selector",
            [gc.KeyTuple("$SELECTOR_GROUP_ID", group_id)],
        )

    def deleteNodeSelectorEntry(self, dst_addr: str):
        """Delete a node selector table entry."""
        self.deleteTableEntry(
            "pipe.SwitchIngress.node_selector",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
        )

    def deleteTableEntry(self, tableName: str, keyFields=None):
        """Delete a single table entry by key."""
        table = self.bfrt_info.table_get(tableName)
        keyList = [table.make_key(keyFields)]
        table.entry_del(self.target, keyList)

    def clearTable(self, tableName: str):
        """Clear all entries from a table."""
        table = self.bfrt_info.table_get(tableName)
        table.entry_del(self.target)  # No keys = delete all

    def deleteForwardEntry(self, dst_addr: str):
        self.deleteTableEntry(
            "pipe.SwitchIngress.forward",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
        )

    def deleteClientSnatEntry(self, src_port: int):
        self.deleteTableEntry(
            "pipe.SwitchIngress.client_snat",
            [gc.KeyTuple("hdr.tcp.src_port", src_port)],
        )

    def deleteActionTableEntry(self, node_index: int):
        self.deleteTableEntry(
            "pipe.SwitchIngress.action_selector_ap",
            [gc.KeyTuple("$ACTION_MEMBER_ID", node_index)],
        )

    def deleteSelectionTableEntry(self, group_id: int = 1):
        self.deleteTableEntry(
            "pipe.SwitchIngress.action_selector",
            [gc.KeyTuple("$SELECTOR_GROUP_ID", group_id)],
        )

    def deleteNodeSelectorEntry(self, dst_addr: str):
        self.deleteTableEntry(
            "pipe.SwitchIngress.node_selector",
            [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
        )
