from abc import ABC, abstractmethod


class AbstractSwitchController(ABC):
    def __init__(
        self, sw_name, sw_addr, sw_id, client_id, load_balancer_ip, service_port
    ):
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

        self.load_balancer_ip = load_balancer_ip
        self.service_port = service_port

    @abstractmethod
    def __del__(self):
        pass
