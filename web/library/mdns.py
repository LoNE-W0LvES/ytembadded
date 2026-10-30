"""Announce the app on the home network as http://<name>.local:<port> (mDNS, like an ESP32 does), so
phones and other PCs don't need this PC's IP address. run_library.ps1 turns it on in network mode.

Only the PC's home-network addresses are announced: VPN ones (Tailscale's 100.64.0.0/10) and
self-assigned ones (169.254.x.x) aren't reachable from a phone on the Wi-Fi.
"""
import atexit
import ipaddress
import logging
import socket

log = logging.getLogger(__name__)
_running = []


def lan_addresses():
    # The address of the adapter the router is on: virtual adapters (VirtualBox, VMware, Hyper-V,
    # hotspot) have private addresses too, and a phone handed one of those can't connect.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent; this only picks the outgoing adapter
            ip = s.getsockname()[0]
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_link_local and a not in ipaddress.ip_network("100.64.0.0/10"):
            return [ip]
    except OSError:
        pass  # no route out (no router?): every home-network address instead
    import ifaddr
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6
            a = ipaddress.ip_address(ip.ip)
            if a.is_private and not a.is_loopback and not a.is_link_local and a not in ipaddress.ip_network("100.64.0.0/10"):
                out.append(ip.ip)
    return out


def announce(name, port):
    """Start answering for <name>.local. Returns the address announced, or "" if it couldn't."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        ips = lan_addresses()
        if not ips:
            return ""
        zc = Zeroconf(interfaces=ips)
        info = ServiceInfo("_http._tcp.local.", f"Course Library ({name})._http._tcp.local.",
                           addresses=[socket.inet_aton(ip) for ip in ips], port=port,
                           server=f"{name}.local.", properties={"path": "/"})
        zc.register_service(info, allow_name_change=True)
    except Exception as e:  # never stop the app over this
        log.warning("mDNS announcement failed: %s", e)
        return ""
    _running.append((zc, info))
    atexit.register(lambda: (zc.unregister_service(info), zc.close()))
    return f"http://{name}.local" + ("" if port == 80 else f":{port}")
