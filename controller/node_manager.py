from logging import Logger

import grpc

from utils import printGrpcError
from bf_switch_controller import SwitchController
from internal_types import Node


class NodeManager(object):
    def __init__(
        self, logger: Logger, switch_controller: SwitchController, initial_nodes
    ):
        self.switch_controller = switch_controller
        self.logger = logger

        # ipv4 -> Node map
        self.nodes = {}
        # ipv4 -> idx
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
        except grpc.RpcError as e:
            raise Exception(
                f"Error inserting SNAT entry (src_port={self.switch_controller.service_port}, "
                f"new_src={self.switch_controller.load_balancer_ip}): {printGrpcError(e)}"
            )
        except Exception as e:
            raise Exception(
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
                except grpc.RpcError as e:
                    raise Exception(
                        f"Failed to insert forward table entry for {node=}: {printGrpcError(e)}"
                    )
                except Exception as e:
                    raise Exception(
                        f"Failed to insert forward table entry for {node=}: {e}"
                    )

                if node.is_lb_node:
                    try:
                        self.switch_controller.insertActionTableEntry(
                            node_index=node.idx,
                            new_dst=node.ipv4,
                        )
                    except grpc.RpcError as e:
                        raise Exception(
                            f"Failed to insert action table entry for {node=}: {printGrpcError(e)}"
                        )
                    except Exception as e:
                        raise Exception(
                            f"Failed to insert action table entry for {node=}: {e}"
                        )
                    node_indices.append(node.idx)
                    self.lb_nodes[node.ipv4] = node.idx

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
            except grpc.RpcError as e:
                raise Exception(
                    f"Error inserting empty selection entry: {printGrpcError(e)}"
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
            except grpc.RpcError as e:
                raise Exception(
                    f"Error inserting node selector entry with dst_addr={self.switch_controller.load_balancer_ip}, group_id={1} : {printGrpcError(e)}"
                )
            except Exception as e:
                raise Exception(
                    f"Error inserting node selector entry with dst_addr={self.switch_controller.load_balancer_ip}, group_id={1} : {e}"
                )

    def migrateNode(self, old_ipv4, new_ipv4):
        if old_ipv4 not in self.lb_nodes:
            raise Exception(
                f"Failed to update node {old_ipv4=}, {new_ipv4=}: Node with IP {old_ipv4} is not LB node"
            )
        if old_ipv4 not in self.nodes:
            raise Exception(
                f"Failed to update node {old_ipv4=}, {new_ipv4=}: Node with IP {old_ipv4} not found in nodes"
            )

        old_node = self.nodes[old_ipv4]
        node_index = self.lb_nodes[old_ipv4]

        try:
            # Update the action table entry to rewrite to the new IP
            self.switch_controller.insertActionTableEntry(
                node_index=node_index,
                new_dst=new_ipv4,
                update_type=UpdateType.MODIFY,
            )
            self.logger.info(
                f"Updated action table entry: node_index={node_index}, new_dst={new_ipv4}"
            )

            # Add a forward table entry for the new IP using the same port
            self.switch_controller.insertForwardEntry(
                port=old_node.sw_port,
                dst_addr=new_ipv4,
            )
            self.logger.info(
                f"Inserted forward entry for new IP: {new_ipv4} -> port {old_node.sw_port}"
            )

            # Update internal state
            self.lb_nodes[new_ipv4] = node_index
            del self.lb_nodes[old_ipv4]

            # Create new node entry and update nodes map
            new_node = Node(
                idx=old_node.idx,
                ipv4=new_ipv4,
                sw_port=old_node.sw_port,
                smac=old_node.smac,
                dmac=old_node.dmac,
                is_lb_node=True,
            )
            self.nodes[new_ipv4] = new_node
            # Keep old node in case we need to route to it later
            # del self.nodes[old_ipv4]

        except grpc.RpcError as e:
            raise Exception(
                f"Failed to migrate {old_ipv4=} to {new_ipv4=}: {printGrpcError(e)}"
            )
        except Exception as e:
            raise Exception(f"Failed to migrate {old_ipv4=} to {new_ipv4=}: {e}")
