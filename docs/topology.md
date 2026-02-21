## Network Topology Reference

> This file documents the specific testbed used during development.
> Adapt the IPs, interfaces, and hostnames to match your own deployment.
> SSH targets and NIC names are configured in `experiments/config_hw.env`.

### Equipment

| Device | Hardware | Management IP | Access |
|--------|----------|---------------|--------|
| tofino-switch | Wedge100BF-32X, Tofino 1, 32x QSFP28 | (management IP) | ssh user |
| source-server (lakewood) | DELL R740, 20c/192G | (management IP) | ssh user |
| target-server (loveland) | DELL R740, 20c/192G | (management IP) | ssh user |

The three nodes share a management subnet for SSH access. The data plane uses separate NICs connected through the switch.

### V6 Topology (hybrid, confirmed)
- [Network Topology Reference](#network-topology-reference)
  - [Equipment](#equipment)
  - [V6 Topology (hybrid, confirmed)](#v6-topology-hybrid-confirmed)
  - [NIC Details](#nic-details)
  - [Switch Port Map (confirmed via bounce tests + bfshell `pm show -a`)](#switch-port-map-confirmed-via-bounce-tests--bfshell-pm-show--a)
  - [CPU Port Details (D\_P 64, Port 33/0)](#cpu-port-details-d_p-64-port-330)
  - [Switch Configuration](#switch-configuration)
  - [Open Issues](#open-issues)
  - [Quick Start (from clean state)](#quick-start-from-clean-state)
  - [Switch Interface Summary](#switch-interface-summary)

```
Lakewood                          Loveland
┌──────────────────┐              ┌──────────────────┐
│ enp179s0f0np0 ●──────25G DAC──────● enp179s0f0    │  192.168.10.x  DIRECT
│ enp179s0f1np1 ●──────25G DAC──────● enp179s0f1    │  192.168.11.x  DIRECT
│                  │              │                  │
│ enp101s0np0   ●─ ╳ (no link)   │                  │
│ enp101s0np1   ●──┐             │  enp101s0np0  ●──┐
└──────────────────┘│             └──────────────────┘│
                    │                                 │
              ┌─────┴─────────────────────────────────┴─────┐
              │              Wedge100BF-32X                  │
              │  Cage 2/0 ← Lakewood np1                    │
              │  Cage 3/0 ← Loveland np1                    │
              │  Cage 3/1 ← Loveland np0                    │
              │  Cage 1/2, 1/3 ← Englewood (presumed)       │
              └─────────────────────────────────────────────┘
```

Mellanox NICs (enp179) are direct server-to-server links. This is correct per V6.
Netronome NICs (enp101) go through the switch.

### NIC Details

**Mellanox ConnectX (OUI 40:a6:b7) -- direct links, working:**

| Server | Interface | IP | MAC |
|--------|-----------|-----|-----|
| Lakewood | enp179s0f**0np0** | 192.168.10.2/24 | 40:a6:b7:20:a1:64 |
| Lakewood | enp179s0f**1np1** | 192.168.11.2/24 | 40:a6:b7:20:a1:65 |
| Loveland | enp179s0f0 | 192.168.10.3/24 | 40:a6:b7:21:fb:64 |
| Loveland | enp179s0f1 | 192.168.11.3/24 | 40:a6:b7:21:fb:65 |

Naming gotcha: Lakewood has `np0`/`np1` suffixes, Loveland does not.

**Netronome NFP (OUI 00:15:4d, driver: `nfp`) -- through switch:**

| Server | Interface | MAC | Link | Switch Port |
|--------|-----------|-----|------|-------------|
| Lakewood | enp101s0np0 | 00:15:4d:13:77:6b | **NO** (Port: Other, no cable detected) | unknown/disconnected |
| Lakewood | enp101s0np1 | 00:15:4d:13:77:6c | YES 25G | **2/0** (D_P 140) |
| Loveland | enp101s0np0 | 00:15:4d:13:5e:94 | YES 25G | **3/1** (D_P 149) |
| Loveland | enp101s0np1 | 00:15:4d:13:5e:95 | YES 25G | **3/0** (D_P 148) |

Lakewood enp101s0np1 has host IP **192.168.13.2/24** (separate from container macvlan subnet 192.168.12.0/24).

### Switch Port Map (confirmed via bounce tests + bfshell `pm show -a`)

| Port | D_P | MAC | Speed/FEC | OPR | Connected To | Verification |
|------|-----|-----|-----------|-----|-------------|--------------|
| 1/0 | 132 | 23/0 | — | RDY=YES, not configured | Englewood (presumed) | Transceiver present |
| 1/1 | 133 | 23/1 | — | RDY=YES, not configured | Englewood (presumed) | Transceiver present |
| 1/2 | 134 | 23/2 | — | RDY=YES, not configured | Englewood (presumed) | Transceiver present |
| 1/3 | 135 | 23/3 | — | RDY=YES, not configured | Englewood (presumed) | Transceiver present |
| 2/0 | 140 | 22/0 | 25G RS | UP | Lakewood enp101s0np1 | Confirmed: DWN when np1 downed |
| 3/0 | 148 | 21/0 | 25G RS | UP | Loveland enp101s0np1 | Confirmed: DWN when np1 downed |
| 3/1 | 149 | 21/1 | — | RDY=YES, not configured | Loveland enp101s0np0 | Confirmed: DWN when np0 downed |
| **33/0** | **64** | 32/0 | — | **CPU PCIe port** | Internal (bf_kpkt) | veth250/veth251 in Linux |

Cages 1-3 have transceivers (RDY=YES). Cage 33 is the CPU PCIe Ethernet port. All others: RDY=NO, empty.

### CPU Port Details (D_P 64, Port 33/0)

The Tofino ASIC has a PCIe-based Ethernet port connecting the x86 CPU to the data
plane. This allows the Linux kernel to inject packets into (and receive from) the
P4 pipeline.

| Interface | Role | Details |
|-----------|------|---------|
| veth250 | Linux kernel side | ifindex=70, MAC=06:8d:c7:bb:92:60, MTU=10240, UP |
| veth251 | ASIC side (bf_kpkt) | ifindex=69, paired with veth250 |
| bf_knet | Alternative kernel net iface | DOWN, not configured (using bf_kpkt veth instead) |
| enx020000000002 | **NOT the CPU port** | BMC USB CDC Ethernet (driver: cdc_ether) |

**Packet path**: Linux → veth250 → veth251 → bf_kpkt → ASIC D_P 64 → P4 pipeline.

See `docs/cpu-port-internet-access.md` for using this to route internet traffic
through the P4 data plane.

### Switch Configuration

- SDE: `$HOME/p4containerflow-tofino2/open-p4studio`
- Env: `source ~/setup-open-p4studio.bash`
- Start: `make switch ARCH=tf1` (loads `tna_load_balancer`)
- Port config: **25G RS FEC** (NONE did not work after fresh switchd start)
- P4 program drops all non-IPv4 (including ARP). Egress bypassed.

### Open Issues

1. **Lakewood enp101s0np0**: NIC reports `Port: Other` (no cable detected). NIC hardware is fine (driver loads, IRQs assigned). Physical inspection needed -- cable missing or bad connector.
2. **Static ARP required**: P4 program drops ARP, so static entries needed for any IP communication through the switch.
3. **Cage 1 (Englewood ports)**: 4 channels with transceivers present but never configured. These are NOT campus uplinks. Campus connectivity is via the management interfaces (eno1/enp2s0).
4. **CPU port (D_P 64)**: Available via bf_kpkt/veth250 but never used. Can be used to bridge internet traffic into the P4 data plane. See `docs/cpu-port-internet-access.md`.

### Quick Start (from clean state)

```bash
# Switch
source ~/setup-open-p4studio.bash
cd ~/p4containerflow-tofino2 && make switch ARCH=tf1
# Controller configures ports 2/0 (D_P 140) and 3/0 (D_P 148) automatically
# via controller_config_hw.json port_setup entries.

# Servers: bring Netronome NICs up
sudo ip link set enp101s0np0 up
sudo ip link set enp101s0np1 up
```

### Switch Interface Summary

| Interface | Purpose | Network |
|-----------|---------|---------|
| enp2s0 | Management, SSH, internet access | Campus subnet |
| enx020000000002 | BMC management (USB CDC) | Link-local only |
| veth0-veth63 | PTF test interfaces (one pair per port channel) | Internal |
| veth250/veth251 | CPU port (D_P 64) packet I/O via bf_kpkt | Can bridge to data plane |
| bf_knet | Alternative CPU port interface | DOWN, unused |
