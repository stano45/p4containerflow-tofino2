from logging import Logger
import random
from bf_switch_controller import SwitchController
from internal_types import Node, UpdateType


class NodeManager(object):
    def __init__(
        self, logger: Logger, switch_controller: SwitchController, initial_nodes
    ):
        self.switch_controller = switch_controller
        self.logger = logger

        # ipv4 -> Node map
        self.nodes = {}
        self.lb_nodes = {}

        try:
            self.switch_controller.insertClientSnatEntry(
                src_port=switch_controller.service_port,
                new_src=switch_controller.load_balancer_ip,
            )
            self.logger.info(
                f"Inserted client SNAT entry for "
                f"load balancer IP={self.switch_controller.load_balancer_ip}, "
                f"service port={self.switch_controller.service_port}"
            )

        except Exception as e:
            self.logger.critical(
                f"Error inserting SNAT entry (src_port={self.switch_controller.service_port}, "
                f"new_src={self.switch_controller.load_balancer_ip}): {e}"
            )

        if initial_nodes is not None:
            node_indices = []
            for i, node in enumerate(initial_nodes):
                node = Node(
                    idx=i,
                    ipv4=node["ipv4"],
                    sw_port=node["sw_port"],
                    smac=node["mac"] if "mac" in node else None,
                    dmac=None,
                    is_lb_node=node["is_lb_node"] if "is_lb_node" in node else False,
                )
                self.nodes[node.ipv4] = node

                try:
                    self.switch_controller.insertForwardEntry(
                        port=node.sw_port,
                        dst_addr=node.ipv4,
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to insert forward table entry for {node=}: {e}"
                    )

                if node.is_lb_node:
                    try:
                        self.switch_controller.insertActionTableEntry(
                            node_index=node.idx,
                            new_dst=node.ipv4,
                        )
                    except Exception as e:
                        self.logger.error(
                            f"Failed to insert action table entry for {node=}: {e}"
                        )
                    node_indices.append(node.idx)

            try:
                self.switch_controller.insertSelectionTableEntry(
                    members=node_indices,
                    member_status=[True] * len(node_indices),
                    group_id=1,
                    max_grp_size=4,
                )
                self.logger.info(
                    f"Inserted selection table entry with members={node_indices}, member_status={[True] * len(node_indices)}, group_id={1}, max_grp_size={4}"
                )
            except Exception as e:
                self.logger.critical(f"Error inserting empty selection entry: {e}")

            try:
                self.switch_controller.insertNodeSelectorEntry(
                    dst_addr=self.switch_controller.load_balancer_ip, group_id=1
                )
                self.logger.info(
                    f"Inserted node selector entry: dst_addr={self.switch_controller.load_balancer_ip}, group_id={1}"
                )
            except Exception as e:
                self.logger.critical(
                    f"Error inserting node selector entry with dst_addr={self.switch_controller.load_balancer_ip}, group_id={1} : {e}"
                )

    def addNode(self, ipv4: str, sw_port: int, is_client: bool, is_lb_node: bool):
        return self._addNode(
            Node(
                idx=random.randint(0, 60000),
                ipv4=ipv4,
                sw_port=sw_port,
                is_lb_node=is_lb_node,
            )
        )

    def updateNode(self, old_ipv4, ipv4, source_mac, dest_mac, sw_port, is_client):
        if old_ipv4 not in self.node_map:
            raise Exception(
                f"Failed to update node: Node with IP {old_ipv4} does not exist"
            )
        return self._updateNode(
            old_node=self.node_map[old_ipv4],
            new_node=Node(
                id=None,
                ipv4=ipv4,
                sw_port=sw_port,
                smac=source_mac,
                dmac=dest_mac,
            ),
        )

    def _addNode(self, node: Node):
        if node.ipv4 in self.node_map:
            raise Exception(f"Failed to add node: {node} already exists")

        self._updateTables(
            old_node=None,
            new_node=node,
            is_client=node.is_client,
            update_type=UpdateType.INSERT,
        )

        self.switch_controller.readTableRules()

    def _updateNode(self, old_node: Node, new_node: Node):
        if new_node.ipv4 in self.node_map:
            raise Exception(
                f"Failed to update node: {self.node_map[new_node.ipv4]} already exists"
            )
        if old_node.is_client != new_node.is_client:
            if old_node.is_client:
                raise Exception(
                    f"Failed to update node: trying to update a client with a server node: {old_node=}, {new_node=}"
                )
            else:
                raise Exception(
                    f"Failed to update node: trying to update a server node with a client node: {old_node=}, {new_node=}"
                )
        pass

    def _updateTables(self, old_node: Node, new_node: Node, update_type: UpdateType):
        pass
