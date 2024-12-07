import queue as Queue
import threading
import time
from abstract_switch_controller import AbstractSwitchController
import grpc
import bfrt_grpc.bfruntime_pb2_grpc as bfruntime_pb2_grpc
import bfrt_grpc.bfruntime_pb2 as bfruntime_pb2

import google.rpc.status_pb2 as status_pb2
import google.rpc.code_pb2 as code_pb2


class SwitchController(AbstractSwitchController):
    def __init__(
        self,
        logger,
        sw_name,
        sw_addr,
        sw_id,
        client_id,
    ):
        super().__init__(sw_name, sw_addr, sw_id, client_id)

        self.logger = logger
        gigabyte = 1024**3
        logger.info("Establishing insecure channel with %s", self.sw_addr)
        self.channel = grpc.insecure_channel(
            self.sw_addr,
            options=[
                ("grpc.max_send_message_length", gigabyte),
                ("grpc.max_receive_message_length", gigabyte),
                ("grpc.max_metadata_size", gigabyte),
            ],
        )

        self.stub = bfruntime_pb2_grpc.BfRuntimeStub(self.channel)

        self.set_up_stream()

        # Subscribe to receive notifications
        num_tries = 5
        cur_tries = 0
        success = False
        while cur_tries < num_tries and not success:
            self.subscribe()
            self.logger.info("Subscribe attempt #%d", cur_tries + 1)
            # Wait for 5 seconds max for each attempt
            success = self.is_subscribe_successful(5)
            cur_tries += 1

        # Set forwarding pipeline config (For the time being we are just
        # associating a client with a p4). Currently the grpc server supports
        # only one client to be in-charge of one p4.
        if self.sw_name and self.sw_name != "":
            self.bindPipelineConfig()

    def bindPipelineConfig(self):
        req = bfruntime_pb2.SetForwardingPipelineConfigRequest()
        req.client_id = self.client_id
        req.action = bfruntime_pb2.SetForwardingPipelineConfigRequest.BIND
        config = req.config.add()
        config.p4_name = self.sw_name
        self.logger.info("Binding with p4_name " + self.sw_name)
        try:
            self.stub.SetForwardingPipelineConfig(req)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNKNOWN:
                raise e
        self.logger.info("Binding with p4_name %s successful!!", self.sw_name)

    def addConfigToSetForwardRequest(
        self, req, p4_name, bfruntime_info, input_profiles
    ):
        def read_file(file_name):
            data = ""
            with open(file_name, "r") as myfile:
                data = myfile.read()
            return data

        config = req.config.add()
        config.p4_name = p4_name
        config.bfruntime_info = read_file(bfruntime_info)
        for input_profile in input_profiles:
            profile = config.profiles.add()
            profile.profile_name = input_profile.profile_name
            profile.context = read_file(input_profile.context_file)
            profile.binary = read_file(input_profile.binary_file)
            profile.pipe_scope.extend(input_profile.pipe_scope)

    def write(self, req):
        req.client_id = self.client_id
        try:
            self.stub.Write(req)
        except grpc.RpcError as e:
            self.printGrpcError(e)
            raise e

    def read(self, req):
        try:
            return self.stub.Read(req)
        except grpc.RpcError as e:
            self.printGrpcError(e)
            raise e

    def set_up_stream(self):
        self._stream_out_q = Queue.Queue()
        self._stream_in_q = Queue.Queue()
        self._exception_q = Queue.Queue()

        def stream_iterator():
            while True:
                p = self._stream_out_q.get()
                if p is None:
                    break
                yield p

        def stream_recv(stream):
            try:
                for p in stream:
                    self._stream_in_q.put(p)
            except grpc.RpcError as e:
                self._exception_q.put(e)

        self.stream = self.stub.StreamChannel(stream_iterator())
        self._stream_recv_thread = threading.Thread(
            target=stream_recv, args=(self.stream,)
        )
        self._stream_recv_thread.start()

    def subscribe(self):
        req = bfruntime_pb2.StreamMessageRequest()
        req.client_id = self.client_id
        req.subscribe.device_id = self.sw_id
        req.subscribe.notifications.enable_learn_notifications = True
        req.subscribe.notifications.enable_idletimeout_notifications = True
        req.subscribe.notifications.enable_port_status_change_notifications = True
        self._stream_out_q.put(req)

    def is_subscribe_successful(self, timeout=1):
        msg = self.get_stream_message("subscribe", timeout)
        if msg is None:
            self.logger.info("Subscribe timeout exceeded %ds", timeout)
            return False
        else:
            self.logger.info(
                "Subscribe response received %d", msg.subscribe.status.code
            )
            if msg.subscribe.status.code != code_pb2.OK:
                self.logger.info("Subscribe failed")
                return False
        return True


    def get_stream_message(self, type_, timeout=1):
        start = time.time()
        try:
            while True:
                remaining = timeout - (time.time() - start)
                if remaining < 0:
                    break
                msg = self._stream_in_q.get(timeout=remaining)
                if not msg.HasField(type_):
                    # Put the msg back in for someone else to read
                    # TODO make separate queues for each msg type
                    self._stream_in_q.put(msg)
                    continue
                return msg
        except:  # timeout expired
            pass
        return None

    def __del__(self):
        pass

    def insertEcmpGroupSelectEntry(
        self, matchDstAddr, ecmp_base, ecmp_count, update_type="INSERT"
    ):
        pass

    def insertEcmpGroupRewriteSrcEntry(
        self, matchDstAddr, new_src, update_type="INSERT"
    ):
        pass

    def insertEcmpNhopEntry(self, ecmp_select, dmac, ipv4, port, update_type="INSERT"):
        pass

    def deleteEcmpNhopEntry(self, ecmp_select):
        pass

    def insertSendFrameEntry(self, egress_port, smac, update_type="INSERT"):
        pass

    def deleteSendFrameEntry(self, egress_port):
        pass

    def readTableRules(self):
        pass

    def printGrpcError(self, grpc_error):
        status_code = grpc_error.code()
        self.logger.error("gRPC Error %s %s", grpc_error.details(), status_code.name)

        if status_code != grpc.StatusCode.UNKNOWN:
            return
        bfrt_errors = self.parseGrpcErrorBinaryDetails(grpc_error)
        if bfrt_errors is None:
            return
        self.logger.error("Errors in batch:")
        for idx, bfrt_error in bfrt_errors:
            code_name = code_pb2._CODE.values_by_number[bfrt_error.canonical_code].name
            self.logger.error(
                "\t* At index %d %s %s\n", idx, code_name, bfrt_error.message
            )
        return bfrt_errors

    def parseGrpcErrorBinaryDetails(self, grpc_error):
        if grpc_error.code() != grpc.StatusCode.UNKNOWN:
            return None

        error = None
        # The gRPC Python package does not have a convenient way to access the
        # binary details for the error: they are treated as trailing metadata.
        for meta in grpc_error.trailing_metadata():
            if meta[0] == "grpc-status-details-bin":
                error = status_pb2.Status()
                error.ParseFromString(meta[1])
                break
        if error is None:  # no binary details field
            return None
        if len(error.details) == 0:
            # binary details field has empty Any details repeated field
            return None

        indexed_p4_errors = []
        for idx, one_error_any in enumerate(error.details):
            p4_error = bfruntime_pb2.Error()
            if not one_error_any.Unpack(p4_error):
                return None
            if p4_error.canonical_code == code_pb2.OK:
                continue
            indexed_p4_errors += [(idx, p4_error)]
        return indexed_p4_errors
