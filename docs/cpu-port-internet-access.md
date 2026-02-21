# Local Client Access for Live Migration Experiments

- [Local Client Access for Live Migration Experiments](#local-client-access-for-live-migration-experiments)
  - [Status](#status)
  - [Active Approach: Macvlan-Shim + SSH Tunnel](#active-approach-macvlan-shim--ssh-tunnel)
    - [How It Works](#how-it-works)
    - [Why It Survives Migration](#why-it-survives-migration)
    - [Key Configuration](#key-configuration)
  - [CPU Port Approach (Abandoned — Reference Only)](#cpu-port-approach-abandoned--reference-only)
    - [Overview (original plan)](#overview-original-plan)
  - [Topology (Confirmed 2026-02-18)](#topology-confirmed-2026-02-18)
    - [Equipment](#equipment)
    - [Internet-facing ports (from outside university)](#internet-facing-ports-from-outside-university)
    - [Switch Data Plane — Full Port Map](#switch-data-plane--full-port-map)
    - [Physical Cabling](#physical-cabling)
    - [NIC Details](#nic-details)
  - [CPU Port Architecture](#cpu-port-architecture)
    - [What is the CPU Port?](#what-is-the-cpu-port)
    - [Kernel Interfaces](#kernel-interfaces)
    - [P4 Program (Current State)](#p4-program-current-state)
  - [Implementation Plan: Internet → CPU Port → Server](#implementation-plan-internet--cpu-port--server)
    - [Traffic Flow](#traffic-flow)
    - [Why This Works with CRIU Migration](#why-this-works-with-criu-migration)
    - [Step-by-Step Setup](#step-by-step-setup)
      - [1. Configure veth250 (CPU port Linux interface)](#1-configure-veth250-cpu-port-linux-interface)
      - [2. Add route for macvlan subnet](#2-add-route-for-macvlan-subnet)
      - [3. Enable IP forwarding](#3-enable-ip-forwarding)
      - [4. Set up iptables NAT](#4-set-up-iptables-nat)
      - [5. Add P4 forwarding entries (via controller API)](#5-add-p4-forwarding-entries-via-controller-api)
      - [6. Static ARP entries (since P4 drops ARP between CPU port and containers)](#6-static-arp-entries-since-p4-drops-arp-between-cpu-port-and-containers)
      - [7. Run the loadgen locally](#7-run-the-loadgen-locally)
    - [Required Changes](#required-changes)
    - [Risks and Mitigations](#risks-and-mitigations)
  - [Alternative: Englewood Ports (1/0-1/3)](#alternative-englewood-ports-10-13)
  - [Quick Test Plan](#quick-test-plan)

## Status

**Active approach: macvlan-shim + SSH tunnel** (see below).

The CPU port approach (D_P 64) was explored but abandoned due to `bf_kpkt`
configuration issues: `bf_switchd` skips pkt-mgr init when `bf_kpkt` is loaded,
leaving `pkt_extraction_credits=0` for D_P 64. This means the ASIC can inject
packets into the CPU port (TX works) but cannot extract packets from it (RX
blocked). The CPU port architecture and findings are preserved below for reference.

---

## Active Approach: Macvlan-Shim + SSH Tunnel

### How It Works

A macvlan sub-interface ("shim") on lakewood gives the host network access to the
container subnet (192.168.12.0/24). An SSH tunnel from th- [Local Client Access for Live Migration Experiments](#local-client-access-for-live-migration-experiments)


```
Local Machine (loadgen + collector)
    │
    │ SSH tunnel: localhost:18080 → 192.168.12.2:8080
    │             localhost:18081 → 192.168.12.2:8081
    ▼
┌─ Lakewood ───────────────────────────────────────┐
│                                                   │
│  sshd opens TCP to 192.168.12.2 via macshim       │
│                                                   │
│  macshim (192.168.12.100, MAC 02:42:c0:a8:0c:64) │
│     ↕ macvlan bridge mode on enp101s0np1          │
│                                                   │
│  stream-server container (192.168.12.2)           │
│     ↕ macvlan on enp101s0np1                      │
│                                                   │
└───────────┬───────────────────────────────────────┘
            │ enp101s0np1 → Port 2/0 (D_P 140)
            ▼
      ┌── P4 Switch (Tofino 1) ──┐
      │                           │
      │  forward table:           │
      │  .100 → port 140 (LW)    │
      │  .2   → port 140 or 148  │
      │                           │
      └─────────┬─────────────────┘
                │ Port 3/0 (D_P 148)
                ▼
          ┌── Loveland ──┐
          │               │
          │  (after       │
          │   migration)  │
          │  h3 container │
          │  192.168.12.2 │
          └───────────────┘
```

### Why It Survives Migration

1. SSH tunnel stays alive (it's between local machine and lakewood's sshd)
2. sshd's TCP connections to 192.168.12.2 use the macshim as source (192.168.12.100)
3. Before migration: macvlan bridge delivers packets locally on lakewood
4. After migration: container removed from lakewood → packets go to parent NIC
   → P4 switch → loveland (forwarding table updated by cr_hw.sh)
5. CRIU preserves the server's TCP state; the peer IP (192.168.12.100) is unchanged
6. Return traffic: loveland → switch → port 140 (lakewood) → macshim → sshd → tunnel

### Key Configuration

- `macshim`: `ip link add macshim link enp101s0np1 type macvlan mode bridge`
- Shim IP/MAC: 192.168.12.100 / 02:42:c0:a8:0c:64 (same as H1 client config)
- SSH tunnel: `ssh -N -L 18080:192.168.12.2:8080 -L 18081:192.168.12.2:8081 lakewood`
- P4 table: 192.168.12.100 → port 140 (already in controller_config_hw.json)

---

## CPU Port Approach (Abandoned — Reference Only)

### Overview (original plan)

This section describes the planned approach to route internet traffic through the
Tofino switch's CPU port so that external clients could connect directly to the
migrating server container.

## Topology (Confirmed 2026-02-18)

### Equipment

| Device | Hardware | Management IP | SSH |
|--------|----------|---------------|-----|
| tofino-switch | Wedge100BF-32X, Tofino 1 | (management IP) | user |
| source-server | DELL R740, 20c/192G | (management IP) | user |
| target-server | DELL R740, 20c/192G | (management IP) | user |

All devices share the same management subnet. See `experiments/config_hw.env` for connection details.

### Internet-facing ports (from outside university)

| Host | Port 22 (SSH) | Port 80 | Other |
|------|:---:|:---:|:---:|
| lakewood | OPEN | OPEN (no HTTP server) | closed |
| loveland | OPEN | OPEN (no HTTP server) | closed |
| wedge100bf | OPEN | OPEN (no HTTP server) | closed |

### Switch Data Plane — Full Port Map

```
Front Panel Cages 1-32 (QSFP28, 4 channels each = 128 possible ports)
Cage 33 = CPU PCIe Ethernet port (internal)

Only ports with transceivers (RDY=YES) or configured:

PORT  | D_P | MAC  | P/PT  | Connected To              | Status
------+-----+------+-------+---------------------------+--------
1/0   | 132 | 23/0 | 3/4   | Englewood (presumed)      | RDY=YES, not configured
1/1   | 133 | 23/1 | 3/5   | Englewood (presumed)      | RDY=YES, not configured
1/2   | 134 | 23/2 | 3/6   | Englewood (presumed)      | RDY=YES, not configured
1/3   | 135 | 23/3 | 3/7   | Englewood (presumed)      | RDY=YES, not configured
2/0   | 140 | 22/0 | 3/12  | Lakewood enp101s0np1      | 25G RS, UP ✓
3/0   | 148 | 21/0 | 3/20  | Loveland enp101s0np1      | 25G RS, UP ✓
3/1   | 149 | 21/1 | 3/21  | Loveland enp101s0np0      | RDY=YES, not configured
33/0  |  64 | 32/0 | 1/64  | CPU PCIe (internal)       | Available via bf_kpkt

All other ports: RDY=NO (no transceiver), not configured.
```

### Physical Cabling

```
Lakewood                              Loveland
┌────────────────────┐                ┌────────────────────┐
│ eno1          ─────── campus LAN ──────── eno1           │  mgmt subnet
│                    │                │                    │
│ enp179s0f0np0 ●──────25G DAC──────────● enp179s0f0      │  192.168.10.x (direct)
│ enp179s0f1np1 ●──────25G DAC──────────● enp179s0f1      │  192.168.11.x (direct)
│                    │                │                    │
│ enp101s0np0   ●─ ✗ (no cable)      │                    │
│ enp101s0np1   ●──┐ (192.168.13.2)  │  enp101s0np0  ●───┐
└────────────────────┘│                └────────────────────┘│
                      │                                      │
               ┌──────┴──────────────────────────────────────┴──────┐
               │                 Wedge100BF-32X                     │
               │                                                    │
               │  Port 2/0 (D_P 140) ← Lakewood np1                │
               │  Port 3/0 (D_P 148) ← Loveland np1                │
               │  Port 3/1 (D_P 149) ← Loveland np0                │
               │  Port 1/x (D_P 132-135) ← Englewood (4 channels)  │
               │                                                    │
               │  Port 33/0 (D_P 64) = CPU PCIe ← internal         │
               │     ↕ bf_kpkt kernel module                        │
               │     ↕ veth250 (kernel) / veth251 (ASIC)            │
               │                                                    │
               │  enp2s0 = Management interface (MGMT_IP)           │
               │  enx020000000002 = BMC USB CDC Ethernet (NOT ASIC) │
               └────────────────────────────────────────────────────┘
```

### NIC Details

**Netronome NFP (through switch):**

| Server | Interface | Host IP | MAC | Switch Port |
|--------|-----------|---------|-----|-------------|
| Lakewood | enp101s0np1 | 192.168.13.2/24 | 00:15:4d:13:77:6c | 2/0 (D_P 140) |
| Loveland | enp101s0np0 | — | 00:15:4d:13:5e:94 | 3/1 (D_P 149) |
| Loveland | enp101s0np1 | — | 00:15:4d:13:5e:95 | 3/0 (D_P 148) |

**Container macvlan network (on Netronome NICs):**

| Container | IP | MAC (fixed) | Host |
|-----------|-----|-------------|------|
| stream-server (initial) | 192.168.12.2 | 02:42:c0:a8:0c:02 | Lakewood |
| stream-client | 192.168.12.100 | 02:42:c0:a8:0c:64 | Lakewood |
| (after migration) | 192.168.12.2 | 02:42:c0:a8:0c:02 | Loveland |

**Mellanox ConnectX (direct server-to-server links):**

| Link | Lakewood | Loveland | Subnet |
|------|----------|----------|--------|
| DAC 1 | enp179s0f0np0 (192.168.10.2) | enp179s0f0 (192.168.10.3) | 192.168.10.0/24 |
| DAC 2 | enp179s0f1np1 (192.168.11.2) | enp179s0f1 (192.168.11.3) | 192.168.11.0/24 |


---

## CPU Port Architecture

### What is the CPU Port?

The Tofino ASIC has a dedicated PCIe-based Ethernet port (D_P 64, front-panel
label 33/0) that connects the switch's x86 CPU to the data plane. This allows:

- **Injection**: Linux kernel sends a packet to veth250 → bf_kpkt module takes
  it from veth251 → injects into the ASIC at D_P 64 → P4 pipeline processes it
  → forwards out a physical port.
- **Punt**: P4 program sets egress port to D_P 64 → ASIC sends to bf_kpkt →
  appears on veth251 → available on veth250 in the Linux kernel.

### Kernel Interfaces

```
bf_kpkt module (loaded) — handles PCIe packet I/O between Linux and ASIC
bf_knet module (loaded but DOWN) — alternative kernel networking, not used

veth250 ←→ veth251   (mtu 10240, UP, has traffic)
  veth250 = Linux kernel side (ifindex 70)
  veth251 = ASIC side, bf_kpkt intercepts (ifindex 69)
```

The `enx020000000002` interface (MAC 02:00:00:00:00:02, driver: cdc_ether) is
the BMC USB management port, NOT the ASIC CPU port.

### P4 Program (Current State)

The `forward` table already does L3 forwarding:
```
table forward {
    key = { hdr.ipv4.dst_addr: exact; }
    actions = { set_egress_port; NoAction; }
    const default_action = NoAction;  // ← drops unmatched traffic
    size = 1024;
}
```

No P4 changes are needed for basic operation — we just add table entries via the
controller. However, to add a "default route" (send all unmatched traffic to the
CPU port), we need to either:
1. Remove `const` from `default_action` so the controller can set it at runtime, OR
2. Add specific forward entries for each client IP/subnet.


---

## Implementation Plan: Internet → CPU Port → Server

### Traffic Flow

```
Internet Client (your machine)
    │
    │ TCP to <switch-mgmt-ip>:80
    ▼
┌─ Switch Linux Kernel (enp2s0, MGMT_IP) ────────┐
│                                                  │
│  iptables NAT:                                   │
│    PREROUTING:  DNAT dst → 192.168.12.2:8080     │
│    POSTROUTING: SNAT src → 192.168.12.1          │
│                                                  │
│  ip route: 192.168.12.0/24 dev veth250           │
│                                                  │
│  veth250 (IP: 192.168.12.1)                      │
│      │                                           │
└──────┼───────────────────────────────────────────┘
       │ bf_kpkt
       ▼
   ASIC D_P 64 (CPU port ingress)
       │
       │ P4 forward table: dst=192.168.12.2 → port 140 or 148
       ▼
   Physical port (Lakewood or Loveland)
       │
       ▼
   Server container (192.168.12.2:8080, macvlan)
       │
       │ Response: dst=192.168.12.1
       ▼
   macvlan → NIC → switch port 140/148
       │
       │ P4 forward table: dst=192.168.12.1 → D_P 64
       ▼
   ASIC D_P 64 (CPU port egress)
       │ bf_kpkt
       ▼
   veth250 → Linux kernel → conntrack reverse NAT → enp2s0 → Internet
```

### Why This Works with CRIU Migration

1. Client's TCP connection is to the switch's management IP (MGMT_IP:80)
2. The switch does NAT, presenting itself as 192.168.12.1 to the server
3. Server's TCP state (checkpointed by CRIU) includes peer = 192.168.12.1:X
4. After restore on loveland, server expects packets from 192.168.12.1:X
5. The switch continues sending through the same NAT (conntrack entries persist)
6. P4 switch updates the `forward` table: 192.168.12.2 → new port (loveland)
7. Server responses to 192.168.12.1 → P4 routes to D_P 64 → switch kernel → client
8. TCP connection survives transparently

### Step-by-Step Setup

#### 1. Configure veth250 (CPU port Linux interface)

```bash
# On the switch (wedge100bf)
sudo ip addr add 192.168.12.1/24 dev veth250
sudo ip link set veth250 mtu 1500    # match standard Ethernet
```

#### 2. Add route for macvlan subnet

```bash
# On the switch
sudo ip route add 192.168.12.0/24 dev veth250
```

#### 3. Enable IP forwarding

```bash
sudo sysctl -w net.ipv4.ip_forward=1
```

#### 4. Set up iptables NAT

```bash
# DNAT: internet:80 → server container
sudo iptables -t nat -A PREROUTING -i enp2s0 -p tcp --dport 80 \
  -j DNAT --to-destination 192.168.12.2:8080

# SNAT: masquerade as the CPU port IP so the server can respond
sudo iptables -t nat -A POSTROUTING -o veth250 -j SNAT --to-source 192.168.12.1
```

#### 5. Add P4 forwarding entries (via controller API)

```bash
# Forward entry: route traffic TO 192.168.12.1 back to CPU port (D_P 64)
curl -X POST http://127.0.0.1:5000/addForward \
  -H 'Content-Type: application/json' \
  -d '{"dst_addr": "192.168.12.1", "port": 64}'

# ARP forward entry (if ARP is used on this subnet)
curl -X POST http://127.0.0.1:5000/addArpForward \
  -H 'Content-Type: application/json' \
  -d '{"target_ip": "192.168.12.1", "port": 64}'
```

#### 6. Static ARP entries (since P4 drops ARP between CPU port and containers)

```bash
# On the switch: tell Linux how to reach the server container MAC
sudo ip neigh add 192.168.12.2 lladdr 02:42:c0:a8:0c:02 dev veth250 nud permanent

# On the server container: tell it how to reach the CPU port gateway
# (run via nsenter on lakewood/loveland)
sudo nsenter -t <SERVER_PID> -n \
  ip neigh add 192.168.12.1 lladdr <veth250_MAC> dev eth0 nud permanent
```

#### 7. Run the loadgen locally

```bash
# Build for local machine
cd experiments && go build -o /tmp/loadgen ./cmd/loadgen/

# Connect to the switch's public IP
/tmp/loadgen \
  -server http://<switch-mgmt-ip>:80 \
  -connections 8 \
  -metrics-port 9090
```

### Required Changes

| Component | Change | Effort |
|-----------|--------|--------|
| P4 program | Remove `const` from forward table default_action (optional, for default route) | 1 line |
| Controller config | Add CPU port IP (192.168.12.1) as a node with sw_port=64 | Config change |
| Controller API | Add /addForward endpoint if not present | May already exist |
| build_hw.sh | Add veth250 config + iptables NAT on switch | ~15 lines |
| run_experiment.sh | Start loadgen locally instead of in container | ~20 lines |
| Collector | Support scraping local loadgen (direct HTTP, no nsenter) | ~30 lines |
| cr_hw.sh | No changes needed (migration is server-side only) | None |

### Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| bf_kpkt doesn't forward veth250→ASIC | Test with a simple ping before full experiment |
| CPU port D_P 64 needs explicit enabling | Check if bf_switchd auto-enables it; if not, add to port_setup |
| MTU mismatch (veth250 is 10240, internet is 1500) | Set veth250 mtu to 1500 |
| iptables conntrack timeout during long migration | Increase conntrack timeout; loadgen sends every 100ms so entries stay fresh |
| Port 80 already has a listener | Check with `ss -tlnp` on the switch; may need a different port |


---

## Alternative: Englewood Ports (1/0-1/3)

Ports 1/0-1/3 (D_P 132-135) have transceivers but are not configured. They are
presumed to connect to a management server (we don't
have access to). These are NOT uplinks to the campus network — the campus
connectivity goes through the management interface (enp2s0) on each device.

If we ever gain access to Englewood, these ports could provide an additional path
into the data plane, but the CPU port approach is self-contained and doesn't
require external cooperation.


---

## Quick Test Plan

1. **Verify CPU port injection** (no NAT, no iptables — just raw connectivity):
   ```bash
   # On switch: assign IP to veth250
   sudo ip addr add 192.168.12.1/24 dev veth250

   # Add forward entry: 192.168.12.1 → D_P 64
   # (via controller or bfrt_python)

   # On lakewood: from server container, ping the CPU port IP
   sudo nsenter -t <SERVER_PID> -n ping -c3 192.168.12.1
   ```

2. **Verify NAT passthrough**: Set up iptables, connect from switch itself:
   ```bash
   curl http://localhost:80  # should reach the server container
   ```

3. **Verify end-to-end from your machine**: Run loadgen locally.
