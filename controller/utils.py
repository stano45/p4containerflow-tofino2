from ipaddress import ip_address


def ip_to_int(ip_string) -> int:
    return int(ip_address(ip_string))
