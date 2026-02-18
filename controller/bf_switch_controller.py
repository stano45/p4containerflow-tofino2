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

    def setup_ports(self, port_setup: list[dict]):
        """Configure switch front-panel ports via the BF-RT $PORT table.

        Each entry in port_setup should be a dict with:
            dev_port (int):  Device port number (D_P), e.g. 140
            speed (str):     BF speed string, e.g. "BF_SPEED_25G"
            fec (str):       BF FEC string, e.g. "BF_FEC_TYP_REED_SOLOMON"
            auto_neg (str):  Optional, default "PM_AN_DEFAULT"
        """
        if not port_setup:
            return

        self.logger.info("Configuring %d switch port(s)...", len(port_setup))

        # $PORT is a fixed (non-P4) table; retrieve from the P4-bound bfrt_info
        # which also includes fixed tables once the pipeline is bound.
        try:
            port_table = self.bfrt_info.table_get("$PORT")
        except Exception:
            # Fallback: get global bfrt_info (includes all fixed tables)
            self.logger.info("$PORT not in P4 bfrt_info, trying global bfrt_info")
            global_bfrt_info = self.interface.bfrt_info_get()
            port_table = global_bfrt_info.table_get("$PORT")

        target = gc.Target(device_id=self.sw_id, pipe_id=0xFFFF)

        for entry in port_setup:
            dev_port = entry["dev_port"]
            speed = entry.get("speed", "BF_SPEED_25G")
            fec = entry.get("fec", "BF_FEC_TYP_REED_SOLOMON")
            auto_neg = entry.get("auto_neg", "PM_AN_DEFAULT")

            try:
                port_table.entry_add(
                    target,
                    [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                    [port_table.make_data([
                        gc.DataTuple("$SPEED", str_val=speed),
                        gc.DataTuple("$FEC", str_val=fec),
                        gc.DataTuple("$AUTO_NEGOTIATION", str_val=auto_neg),
                        gc.DataTuple("$PORT_ENABLE", bool_val=True),
                    ])],
                )
                self.logger.info(
                    "Added port D_P=%d  speed=%s  fec=%s", dev_port, speed, fec
                )
            except Exception as e:
                # Port may already exist (e.g. re-run); try modify instead
                try:
                    port_table.entry_mod(
                        target,
                        [port_table.make_key([gc.KeyTuple("$DEV_PORT", dev_port)])],
                        [port_table.make_data([
                            gc.DataTuple("$SPEED", str_val=speed),
                            gc.DataTuple("$FEC", str_val=fec),
                            gc.DataTuple("$AUTO_NEGOTIATION", str_val=auto_neg),
                            gc.DataTuple("$PORT_ENABLE", bool_val=True),
                        ])],
                    )
                    self.logger.info(
                        "Modified existing port D_P=%d  speed=%s  fec=%s",
                        dev_port, speed, fec,
                    )
                except Exception as e2:
                    self.logger.error(
                        "Failed to add/modify port D_P=%d: add=%s  mod=%s",
                        dev_port, e, e2,
                    )

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
        dst_mac: str | None = None,
        update_type: UpdateType = UpdateType.INSERT,
    ):
        if dst_mac:
            self.getUpdateFn(update_type)(
                "pipe.SwitchIngress.forward",
                [gc.KeyTuple("hdr.ipv4.dst_addr", gc.ipv4_to_bytes(dst_addr))],
                "SwitchIngress.set_egress_port_with_mac",
                [
                    gc.DataTuple("port", port),
                    gc.DataTuple("dst_mac", gc.mac_to_bytes(dst_mac)),
                ],
            )
        else:
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
