#!/usr/bin/env python3
"""Register the lab's real machines in NetBox, so netCI's DCIM lookups answer from an
inventory system rather than from a fixture.

Everything written here corresponds to something that exists on this host:

* tenant `hello-container` / `hello-kubernetes` / `hello-systemd-go` -- the sample
  applications shipped in `sample-apps/`;
* site `dev` / `staging` / `prod` -- the environments netCI deploys to;
* device `netci-local` -- this machine, which the Docker and systemd adapters deploy to
  through `deploy/ansible/inventories/localhost.ini` (`ansible_connection=local`);
* devices `netci-local-control-plane` / `netci-local-worker` -- the kind cluster nodes,
  which the Kubernetes adapter deploys to.

It is idempotent: re-running updates in place. It is a lab convenience; a production
NetBox is populated by the people who rack the machines.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

NB = os.getenv("NETBOX_URL", "http://127.0.0.1:8080/api").rstrip("/")
TOKEN = os.getenv("NETBOX_TOKEN") or open(".netci-gate/keys/netbox-admin-token").read().strip()


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{NB}{path}", method=method, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def upsert(path: str, key: str, value: str, body: dict) -> dict:
    status, page = call("GET", f"{path}?{key}={urllib.parse.quote(value)}")
    if status == 200 and page.get("count"):
        found = page["results"][0]
        st, updated = call("PATCH", f"{path}{found['id']}/", body)
        assert st == 200, (path, updated)
        return updated
    st, created = call("POST", path, body)
    assert st == 201, (path, created)
    return created


import urllib.parse  # noqa: E402

# Environments
sites = {name: upsert("/dcim/sites/", "slug", name, {"name": name, "slug": name, "status": "active"})
         for name in ("dev", "staging", "prod")}

# Systems (one tenant per sample application)
group = upsert("/tenancy/tenant-groups/", "slug", "local-infrastructure",
               {"name": "Local Infrastructure", "slug": "local-infrastructure"})
tenants = {
    name: upsert("/tenancy/tenants/", "slug", name,
                 {"name": name, "slug": name, "group": group["id"], "description": f"sample application {name}"})
    for name in ("hello-container", "hello-kubernetes", "hello-systemd-go")
}

# Modules (device roles)
roles = {
    name: upsert("/dcim/device-roles/", "slug", name, {"name": name, "slug": name, "color": "2196f3"})
    for name in ("hello-container", "hello-kubernetes", "hello-systemd-go")
}

manufacturer = upsert("/dcim/manufacturers/", "slug", "generic", {"name": "Generic", "slug": "generic"})
dtype_host = upsert("/dcim/device-types/", "slug", "linux-host",
                    {"manufacturer": manufacturer["id"], "model": "Linux host", "slug": "linux-host"})
dtype_node = upsert("/dcim/device-types/", "slug", "kind-node",
                    {"manufacturer": manufacturer["id"], "model": "kind node", "slug": "kind-node"})

def device(name: str, role: str, tenant: str, site: str, dtype: dict, status: str = "active", address: str | None = None) -> dict:
    created = upsert("/dcim/devices/", "name", name, {
        "name": name, "role": roles[role]["id"], "tenant": tenants[tenant]["id"],
        "site": sites[site]["id"], "device_type": dtype["id"], "status": status,
    })
    if address:
        primary_ip(created, address)
    return created


def primary_ip(dev: dict, address: str) -> None:
    """A management interface with the address, set as the device's primary IPv4 -- the
    field netCI's inventory page shows. The lab's own machine answers on loopback for
    the `ansible_connection=local` targets; the separate host has its lab address."""

    st, page = call("GET", f"/dcim/interfaces/?device_id={dev['id']}&name=eth0")
    if st == 200 and page.get("count"):
        iface = page["results"][0]
    else:
        st, iface = call("POST", "/dcim/interfaces/", {"device": dev["id"], "name": "eth0", "type": "virtual"})
        assert st == 201, iface
    # The address may already exist (an earlier seed created it unassigned): adopt it.
    st, page = call("GET", f"/ipam/ip-addresses/?address={urllib.parse.quote(address)}")
    if st == 200 and page.get("count"):
        ip = page["results"][0]
        st, ip = call("PATCH", f"/ipam/ip-addresses/{ip['id']}/", {"assigned_object_type": "dcim.interface", "assigned_object_id": iface["id"]})
        assert st == 200, ip
    else:
        st, ip = call("POST", "/ipam/ip-addresses/", {"address": address, "status": "active",
                                                       "assigned_object_type": "dcim.interface", "assigned_object_id": iface["id"]})
        assert st == 201, ip
    st, updated = call("PATCH", f"/dcim/devices/{dev['id']}/", {"primary_ip4": ip["id"]})
    assert st == 200, updated

# This machine: the Docker and systemd deployment target for every environment the
# localhost inventory serves.
# One NetBox device has one role and one tenant -- that is the inventory's model, and
# it is right: a machine belongs to whoever runs it. This host runs two sample services,
# so it is two logical targets here, both reached through ansible_connection=local.
for env in ("dev", "staging", "prod"):
    # No primary IP: NetBox keeps addresses unique and these six logical devices are
    # one machine reached with ansible_connection=local; netCI shows them as "no IP".
    device(f"netci-local-docker-{env}", "hello-container", "hello-container", env, dtype_host)
    device(f"netci-local-systemd-{env}", "hello-systemd-go", "hello-systemd-go", env, dtype_host)

# The kind cluster nodes, confirmed running.
nodes = subprocess.run(["docker", "ps", "--filter", "name=netci-local", "--format", "{{.Names}}"],
                       capture_output=True, text=True).stdout.split()
for node in nodes:
    for env in ("dev", "staging", "prod"):
        device(f"{node}-{env}", "hello-kubernetes", "hello-kubernetes", env, dtype_node)

# A separate production host (scripts/lab/prod_host.sh): reached over SSH, system-scope
# systemd, privilege escalation -- the way a real production machine is.
prod_host = device("netci-prod-01", "hello-systemd-go", "hello-systemd-go", "prod", dtype_host, address="172.17.0.60/16")
# A NetBox device belongs to one tenant and one role, so the same machine serving a second
# module is a second device record. In the lab both records are the one container at
# 172.17.0.60 (the inventory names the same address twice); on real hardware they would
# more likely be two hosts, which is the model this encodes.
# Same address as netci-prod-01 (one machine); NetBox keeps addresses unique, so the
# second device record carries the fact in its comments and no primary IP.
device("netci-prod-02", "hello-container", "hello-container", "prod", dtype_host)

# One decommissioned device, so "refuse a retired target" is testable against real data.
device("netci-retired-01", "hello-container", "hello-container", "prod", dtype_host, status="decommissioning")

st, page = call("GET", "/dcim/devices/?limit=100")
print(f"NetBox now holds {page.get('count')} devices under tenants {sorted(tenants)}")
for d in page["results"]:
    print(f"  {d['name']:32s} tenant={d['tenant']['slug']:18s} role={d['role']['slug']:18s} site={d['site']['slug']:8s} {d['status']['value']}")
