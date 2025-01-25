from enum import Enum


class Node(object):
    def __init__(
        self,
        idx: int | None,
        ipv4: str,
        sw_port: int,
        is_lb_node: bool = False,
        smac: str | None = None,
        dmac: str | None = None,
    ):
        self.idx = idx
        self.ipv4 = ipv4
        self.sw_port = sw_port
        self.is_lb_node = is_lb_node
        self.smac = smac
        self.dmac = dmac

    def __eq__(self, other):
        return self.ipv4 == other.ipv4

    def __repr__(self):
        return f"Node(idx={self.idx}, ipv4={self.ipv4}, sw_port={self.sw_port}, is_lb_node={self.is_lb_node}, smac={self.smac}, dmac={self.dmac})"


class UpdateType(Enum):
    INSERT = "INSERT"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
