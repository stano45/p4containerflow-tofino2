from bf_switch_controller import SwitchController
from internal_types import Node, UpdateType

ECMP_BASE = 1
ECMP_COUNT = 2

class NodeManager(object):
    def __init__(self, logger, switch_controller: SwitchController, lb_nodes):
        self.switch_controller = switch_controller
        self.logger = logger
        # ipv4 -> Node
        self.node_map = {}
        self.client_node = None

        self.switch_controller.insertEcmpGroupSelectEntry(
            matchDstAddr=self.switch_controller.load_balancer_ip,
            ecmp_base=ECMP_BASE,
            ecmp_count=len(lb_nodes) if lb_nodes is not None else ECMP_COUNT,
            update_type=UpdateType.INSERT
        )
        self.logger.info(
            f"Initialized load balancer with IP: {self.switch_controller.load_balancer_ip}"
        )

        if lb_nodes is not None:
            for i, lb_node in enumerate(lb_nodes):
                ip = lb_node["ip"]
                node = Node(
                    id=i + ECMP_BASE,
                    ipv4=ip,
                    sw_port=lb_node["port"],
                    smac=lb_node["mac"],
                    dmac=None,
                    is_client=False,
                )
                self.node_map[ip] = node
                self.addNode(node)
                self.logger.info(f"Added node to load balancer: {node}")

        self.switch_controller.readTableRules()

    def addNode(self, ipv4, source_mac, dest_mac, sw_port, is_client):
        return self._addNode(
            Node(
                id=None,
                ipv4=ipv4,
                sw_port=sw_port,
                smac=source_mac,
                dmac=dest_mac,
                is_client=is_client,
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
                is_client=is_client,
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
        self._updateTables(
            old_node=old_node,
            new_node=new_node,
            is_client=new_node.is_client,
            update_type=UpdateType.MODIFY,
        )

    def _updateTables(
        self, old_node: Node, new_node: Node, is_client: bool, update_type: UpdateType
    ):
        if update_type == UpdateType.INSERT:
            id = len(self.node_map) + ECMP_BASE
        else:
            id = old_node.id
        new_node.id = id

        self.switch_controller.insertEcmpNhopEntry(
            ecmp_select=id,
            dmac=new_node.dmac,
            ipv4=new_node.ipv4,
            port=new_node.sw_port,
            update_type=update_type,
        )

        self.switch_controller.insertSendFrameEntry(
            egress_port=new_node.sw_port, smac=new_node.smac, update_type=update_type
        )

        if is_client:
            self.switch_controller.insertEcmpGroupRewriteSrcEntry(
                matchDstAddr=new_node.ipv4,
                new_src=self.switch_controller.load_balancer_ip,
                update_type=update_type,
            )
            self.client_node = new_node

        self.node_map[new_node.ipv4] = new_node
        del self.node_map[old_node.ipv4]

        # TODO: check for bmv2
        # self.switch_controller.insertEcmpGroupSelectEntry(
        #     matchDstAddr=self.switch_controller.load_balancer_ip,
        #     ecmp_base=1,
        #     ecmp_count=len(self.node_map),
        #     update_type=update_type,
        # )

    # def _updateClientNode(self, node: Node, is_client: bool, update_type: UpdateType):
    #     # TODO:
    #     # if self.client is not None:
    #     #     raise Exception("Client node already exists")

    #     self.switch_controller.insertEcmpNhopEntry(
    #         ecmp_select=0,
    #         dmac=node.dmac,
    #         ipv4=node.ipv4,
    #         port=node.sw_port,
    #         update_type=update_type,
    #     )

    #     self.switch_controller.insertEcmpGroupRewriteSrcEntry(
    #         matchDstAddr=node.ipv4,
    #         new_src=self.switch_controller.load_balancer_ip,
    #         update_type=update_type,
    #     )

    #     self.switch_controller.insertSendFrameEntry(
    #         egress_port=node.sw_port, smac=node.smac, update_type=update_type
    #     )

    #     self.client_node = node
