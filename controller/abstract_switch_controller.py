from abc import ABC, abstractmethod


class AbstractSwitchController(ABC):
    def __init__(self, sw_name, sw_addr, sw_id, client_id, load_balancer_ip):
        if sw_name != None:
            self.sw_name = sw_name
        else:
            self.sw_name = ""

        self.sw_addr = sw_addr
        self.sw_id = sw_id

        if client_id != None:
            self.client_id = client_id
        else:
            self.client_id = 0

        self.load_nalancer_ip = load_balancer_ip

    @abstractmethod
    def __del__(self):
        pass

    @abstractmethod
    def insertEcmpGroupSelectEntry(
        self, matchDstAddr, ecmp_base, ecmp_count, update_type="INSERT"
    ):
        pass

    @abstractmethod
    def insertEcmpGroupRewriteSrcEntry(
        self, matchDstAddr, new_src, update_type="INSERT"
    ):
        pass

    @abstractmethod
    def insertEcmpNhopEntry(self, ecmp_select, dmac, ipv4, port, update_type="INSERT"):
        pass

    @abstractmethod
    def deleteEcmpNhopEntry(self, ecmp_select):
        pass

    @abstractmethod
    def insertSendFrameEntry(self, egress_port, smac, update_type="INSERT"):
        pass

    @abstractmethod
    def deleteSendFrameEntry(self, egress_port):
        pass

    @abstractmethod
    def readTableRules(self):
        pass
