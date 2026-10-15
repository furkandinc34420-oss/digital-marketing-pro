"""Guard selected CPython DNS, connect and sendto events; allow loopback stubs.

Activate by putting this directory on PYTHONPATH, also inherited by subprocesses.
Not an OS boundary. Does not cover native extensions, other executables,
sendmsg, or processes started without this module. Does not control file writes.
"""
import ipaddress
import socket
import sys


def deny_external_network(event, args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        if str(args[0]) not in {"localhost", "127.0.0.1", "::1"}:
            raise socket.gaierror(socket.EAI_NONAME, "Offline tests: external DNS disabled")
    if event in {"socket.connect", "socket.sendto"}:
        # Local Unix sockets are filesystem IPC, not external IP networking.
        # Filesystem permissions and the worker's mount boundary govern access.
        if args[0].family == socket.AF_UNIX:
            return
        address = args[1] if event == "socket.connect" else args[-1]
        try:
            local = isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, IndexError, TypeError):
            local = False
        if not local:
            raise ConnectionRefusedError("Offline tests: external connections disabled")


sys.addaudithook(deny_external_network)
