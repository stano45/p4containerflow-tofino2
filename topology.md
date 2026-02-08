## Network Topology Reference (2026-02-06)

### Equipment

| Device | Hardware | Access |
|--------|----------|--------|
| wedge100bf | Wedge100BF-32X, Tofino 1, 32x QSFP28 | ssh kosorins32 |
| lakewood | DELL R740, 20c/192G | ssh kosorins32, 131.130.124.93 |
| loveland | DELL R740, 20c/192G | ssh kosorins32, 131.130.124.79 |
| englewood | Management node | No access |

### V6 Topology (hybrid, confirmed)

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

No IPs assigned yet on Netronome NICs.

### Switch Port Map (confirmed via bounce tests)

| Port | D_P | Speed/FEC | OPR | Connected To | Verification |
|------|-----|-----------|-----|-------------|--------------|
| 1/2 | 134 | 25G RS | UP | Englewood (presumed) | Never reacted to any Lakewood/Loveland bounce |
| 1/3 | 135 | 25G RS | UP | Englewood (presumed) | Never reacted to any Lakewood/Loveland bounce |
| 2/0 | 140 | 25G RS | UP | Lakewood enp101s0np1 | Confirmed: DWN when np1 downed |
| 3/0 | 148 | 25G RS | UP | Loveland enp101s0np1 | Confirmed: DWN when np1 downed |
| 3/1 | 149 | 25G RS | UP | Loveland enp101s0np0 | Confirmed: DWN when np0 downed |

All other ports DWN. Only cages 1, 2, 3 have transceivers (RDY=YES). Cage 33 is CPU port. Rest empty.

### Switch Configuration

- SDE: `/home/kosorins32/p4containerflow-tofino2/open-p4studio`
- Env: `source ~/setup-open-p4studio.bash`
- Start: `make switch ARCH=tf1` (loads `tna_load_balancer`)
- Port config: **25G RS FEC** (NONE did not work after fresh switchd start)
- P4 program drops all non-IPv4 (including ARP). Egress bypassed.

### Open Issues

1. **Lakewood enp101s0np0**: NIC reports `Port: Other` (no cable detected). NIC hardware is fine (driver loads, IRQs assigned). Physical inspection needed -- cable missing or bad connector.
2. **No IPs on Netronome NICs**: Need to assign 192.168.12.x / 192.168.13.x per V6 scheme.
3. **Static ARP required**: P4 program drops ARP, so static entries needed for any IP communication through the switch.

### Quick Start (from clean state)

```bash
# Switch
source ~/setup-open-p4studio.bash
cd ~/p4containerflow-tofino2 && make switch ARCH=tf1
# In bfshell (ucli > pm):
port-add 1/- 25G RS
port-add 2/- 25G RS
port-add 3/- 25G RS
port-enb 1/-
port-enb 2/-
port-enb 3/-

# Servers: bring Netronome NICs up
sudo ip link set enp101s0np0 up
sudo ip link set enp101s0np1 up
```
