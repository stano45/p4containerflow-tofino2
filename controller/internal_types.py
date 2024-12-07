from enum import Enum

class Node(object):
    def __init__(self, id, ipv4, sw_port, smac, dmac, is_client):
        self.id = id
        self.ipv4 = ipv4
        self.sw_port = sw_port
        self.smac = smac
        self.dmac = dmac
        self.is_client = is_client

    def __repr__(self):
        return f"Node(is_client={self.is_client}, ipv4={self.ipv4}, sw_port={self.sw_port}, smac={self.smac}, dmac={self.dmac})"
    
class UpdateType(Enum):
    INSERT = "INSERT"
    MODIFY = "MODIFY"
    DELETE = "DELETE"