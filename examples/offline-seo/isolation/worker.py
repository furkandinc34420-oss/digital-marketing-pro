"""Runs only INSIDE the disposable customer container; no external service calls."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys


def attempt(operation):
    try:
        operation()
    except OSError as exc:
        return {"denied": True, "errno": exc.errno}
    return {"denied": False}


def socket_access(path):
    """A socket is tested with AF_UNIX connect, never by opening it as a file."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        result = attempt(lambda: client.connect(str(path)))
    return {"path_absent": info is None, "is_socket": info is not None and stat.S_ISSOCK(info.st_mode),
            "connect_attempted": True, "connect_denied": result["denied"]}


def probe(customer, host_fixtures):
    other = "diger-demo" if customer == "demo-musteri" else "demo-musteri"
    own = Path("/data/customers") / customer / "profile.json"
    foreign = Path("/data/customers") / other / "profile.json"
    link = Path("/output/foreign-link")
    link.symlink_to(foreign)
    Path("/output/own-result.txt").write_text(customer, encoding="utf-8")
    status = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(("CapEff:", "NoNewPrivs:")):
            key, value = line.split(":", 1)
            status[key] = value.strip()
    return {
        "customer_id": customer, "uid": os.getuid(), "gid": os.getgid(),
        "visible_customers": sorted(p.name for p in Path("/data/customers").iterdir()),
        "foreign_read": attempt(foreign.read_bytes),
        "foreign_write": attempt(lambda: foreign.write_bytes(b"forbidden")),
        "foreign_symlink_read": attempt(link.read_bytes),
        "own_input_write": attempt(lambda: own.write_bytes(b"forbidden")),
        "host_fixture_read": attempt((Path(host_fixtures) / other / "profile.json").read_bytes),
        "docker_socket": socket_access(Path("/var/run/docker.sock")),
        "own_output": Path("/output/own-result.txt").read_text(encoding="utf-8-sig"),
        "environment_names": sorted(os.environ), "status": status,
        "network_interfaces": sorted(p.name for p in Path("/sys/class/net").iterdir())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer", choices=["demo-musteri", "diger-demo"], required=True)
    parser.add_argument("--action", choices=["analyze", "content", "technical", "probe"], required=True)
    parser.add_argument("--host-fixtures", required=True)
    args = parser.parse_args()
    if args.action in {"analyze", "content", "technical"}:
        sys.path.insert(0, "/app")
        import offline_seo
        scope = offline_seo.TenantScope(Path("/data/customers"), args.customer)
        if args.action == "content":
            import content_seo
            result = content_seo.analyze(scope)
        elif args.action == "technical":
            import technical_seo
            result = technical_seo.analyze(scope)
        else:
            result = offline_seo.analyze(scope)
    else:
        result = probe(args.customer, args.host_fixtures)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
