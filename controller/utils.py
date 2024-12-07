from ipaddress import ip_address
import sys


def ip_to_int(ip_string) -> int:
    return int(ip_address(ip_string))


def printGrpcError(e):
    print("gRPC Error:", e.details(), end=" ")
    status_code = e.code()
    print("(%s)" % status_code.name, end=" ")
    traceback = sys.exc_info()[2]
    if traceback:
        print("[%s:%d]" % (traceback.tb_frame.f_code.co_filename, traceback.tb_lineno))
