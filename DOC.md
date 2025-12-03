# Technical Documentation

This document provides detailed technical information about the P4 load balancer implementation, control plane architecture, and internal workings.

## Table of Contents

- [Step-by-Step Setup Guides](#step-by-step-setup-guides)
  - [Model Setup (Step-by-Step)](#model-setup-step-by-step)
  - [Hardware Setup (Step-by-Step)](#hardware-setup-step-by-step)
- [Load Balancer Overview](#load-balancer-overview)
- [P4 Program Architecture](#p4-program-architecture)
  - [Parser](#parser)
  - [Ingress Pipeline](#ingress-pipeline)
  - [Tables](#tables)
  - [Checksum Handling](#checksum-handling)
- [Control Plane](#control-plane)
  - [Architecture](#architecture)
  - [Configuration](#configuration)
  - [HTTP API](#http-api)
  - [Node Migration](#node-migration)
- [Packet Flow](#packet-flow)
  - [Client to Server](#client-to-server)
  - [Server to Client](#server-to-client)
- [ActionSelector and Load Balancing](#actionselector-and-load-balancing)

---

## Step-by-Step Setup Guides

These guides provide fine-grained control over the build process. For most users, the quick setup commands in the README are sufficient.

### Model Setup (Step-by-Step)

If you prefer more control over the model setup, run each step individually:

#### 1. Initialize the open-p4studio Submodule

```bash
make init-submodule
```

This clones the open-p4studio repository into the `open-p4studio/` directory.

#### 2. Build with Profile

**Tofino 2 (default):**

```bash
make build-profile PROFILE=profiles/tofino2-model.yaml
```

**Tofino 1:**

```bash
make build-profile PROFILE=profiles/tofino-model.yaml
```

Applies the p4studio profile to build open-p4studio for model simulation. This is a lengthy process (30+ minutes on a fast machine).

#### 3. Generate Environment Script

```bash
make setup-env
```

Creates `~/setup-open-p4studio.bash` which sets up PATH and other environment variables for the SDE tools.

**Important:** You must source this script in every new terminal:

```bash
source ~/setup-open-p4studio.bash
```

To make it permanent, add to your shell profile:

```bash
# For bash
echo 'source ~/setup-open-p4studio.bash' >> ~/.bashrc

# For zsh
echo 'source ~/setup-open-p4studio.bash' >> ~/.zshrc
```

### Hardware Setup (Step-by-Step)

If you prefer more control over the hardware setup, run each step individually:

#### 1. Initialize the open-p4studio Submodule

```bash
make init-submodule
```

This clones the open-p4studio repository into the `open-p4studio/` directory.

#### 2. Extract SDE Packages

```bash
make extract-sde SDE=/path/to/bf-sde-9.13.4
```

Runs `extract_all.sh` in the SDE directory to extract all Intel packages.

#### 3. Setup RDC (Proprietary Driver Files)

```bash
make setup-rdc SDE=/path/to/bf-sde-9.13.4
```

This step:

- Extracts the SDE version from the directory name (e.g., `9.13.4`)
- Configures `open-p4studio/hw/rdc_setup.sh` with the correct paths
- Copies proprietary driver files from the SDE into open-p4studio

#### 4. Configure Profile with BSP Path

**Tofino 2 (default):**

```bash
make config-profile BSP=/path/to/bf-reference-bsp-9.13.4.tgz PROFILE=profiles/tofino2-hardware.yaml
```

**Tofino 1:**

```bash
make config-profile BSP=/path/to/bf-reference-bsp-9.13.4.tgz PROFILE=profiles/tofino-hardware.yaml
```

Updates the `bsp-path` field in the profile YAML file with your BSP location.

#### 5. Extract BSP

```bash
make extract-bsp BSP=/path/to/bf-reference-bsp-9.13.4.tgz
```

Extracts the `bf-platforms` package from the BSP tarball into `open-p4studio/pkgsrc/bf-platforms`.

#### 6. Build with Profile

**Tofino 2 (default):**

```bash
make build-profile PROFILE=profiles/tofino2-hardware.yaml
```

**Tofino 1:**

```bash
make build-profile PROFILE=profiles/tofino-hardware.yaml
```

Applies the p4studio profile to build open-p4studio with hardware support. This is a lengthy process (30+ minutes on a fast machine, 1+ hour on the Tofino switch itself).

#### 7. Generate Environment Script

```bash
make setup-env
```

Creates `~/setup-open-p4studio.bash` which sets up PATH and other environment variables for the SDE tools.

**Important:** You must source this script in every new terminal:

```bash
source ~/setup-open-p4studio.bash
```

To make it permanent, add to your shell profile:

```bash
# For bash
echo 'source ~/setup-open-p4studio.bash' >> ~/.bashrc

# For zsh
echo 'source ~/setup-open-p4studio.bash' >> ~/.zshrc
```

---

## Load Balancer Overview

This is a Layer 3/TCP load balancer implemented in P4_16 for Intel Tofino 1/2 ASICs. It provides:

- **Connection-consistent load balancing**: Uses a 5-tuple hash (src IP, dst IP, protocol, src port, dst port) to ensure all packets from the same TCP connection go to the same backend server.
- **SNAT for return traffic**: Rewrites the source IP of server responses to the load balancer's VIP so clients see a consistent address.
- **Live node migration**: Allows seamless migration of traffic from one backend to another without disrupting existing connections.
- **L3 forwarding**: Standard IPv4 forwarding based on destination address.

### Key Features

| Feature                 | Description                                |
| ----------------------- | ------------------------------------------ |
| **Protocol Support**    | TCP (easily extensible to UDP)             |
| **Hash Algorithm**      | CRC16 for consistent hashing               |
| **Max Backend Servers** | 4 (configurable via ActionProfile size)    |
| **Max Groups**          | 1 (configurable)                           |
| **Checksum Updates**    | IPv4 and TCP checksums updated in deparser |

---

## P4 Program Architecture

The P4 program (`load_balancer/t2na_load_balancer.p4`) is structured as a single ingress pipeline with no egress processing (bypass egress for efficiency).

### Parser

The parser extracts Ethernet, IPv4, and TCP headers:

```
Ethernet → IPv4 → TCP → Accept
```

**Checksum Verification**: The parser computes and verifies the IPv4 checksum. For TCP, it computes a partial checksum that will be completed in the deparser after any address rewrites.

### Ingress Pipeline

The ingress control block processes packets in this order:

1. **Validate packet**: Drop invalid IPv4 packets or those with TTL < 1
2. **Load balancing** (`node_selector` table): Match on VIP, select backend via ActionSelector
3. **SNAT** (`client_snat` table): Rewrite source IP for server→client traffic (only if not a load-balanced packet)
4. **Forwarding** (`forward` table): Set egress port based on destination IP
5. **Bypass egress**: Skip egress pipeline entirely

### Tables

#### `node_selector` Table

Performs load balancing using an ActionSelector.

| Field               | Match Type | Description                           |
| ------------------- | ---------- | ------------------------------------- |
| `hdr.ipv4.dst_addr` | exact      | Virtual IP (VIP) of the load balancer |
| `hdr.ipv4.src_addr` | selector   | Used for hash computation             |
| `hdr.ipv4.dst_addr` | selector   | Used for hash computation             |
| `hdr.ipv4.protocol` | selector   | Used for hash computation             |
| `hdr.tcp.src_port`  | selector   | Used for hash computation             |
| `hdr.tcp.dst_port`  | selector   | Used for hash computation             |

**Action**: `set_rewrite_dst(new_dst)` - Rewrites destination IP to the selected backend server.

#### `client_snat` Table

Performs Source NAT for server→client traffic.

| Field              | Match Type | Description               |
| ------------------ | ---------- | ------------------------- |
| `hdr.tcp.src_port` | exact      | Service port (e.g., 8080) |

**Action**: `set_rewrite_src(new_src)` - Rewrites source IP to the load balancer VIP.

#### `forward` Table

Standard L3 forwarding.

| Field               | Match Type | Description            |
| ------------------- | ---------- | ---------------------- |
| `hdr.ipv4.dst_addr` | exact      | Destination IP address |

**Action**: `set_egress_port(port)` - Sets the egress port for the packet.

#### `action_selector_ap` (Action Profile)

Stores the backend server entries (members) for the ActionSelector.

| Key                 | Description                       |
| ------------------- | --------------------------------- |
| `$ACTION_MEMBER_ID` | Index of the backend server (0-3) |

**Action**: `set_rewrite_dst(new_dst)` - IP address to rewrite destination to.

#### `action_selector` (Selector Group)

Groups action profile members and distributes traffic using CRC16 hash.

| Field                   | Description                     |
| ----------------------- | ------------------------------- |
| `$SELECTOR_GROUP_ID`    | Group identifier                |
| `$MAX_GROUP_SIZE`       | Maximum members in group (4)    |
| `$ACTION_MEMBER_ID`     | Array of member indices         |
| `$ACTION_MEMBER_STATUS` | Array of active/inactive status |

### Checksum Handling

The P4 program handles checksums carefully:

1. **Parser**: Verifies IPv4 checksum, computes partial TCP checksum
2. **Ingress**: Sets flags when addresses are modified
3. **Deparser**: Recomputes IPv4 and TCP checksums if flags are set

```p4
// In deparser - recompute checksums for modified packets
if (ig_md.checksum_upd_ipv4) {
    hdr.ipv4.hdr_checksum = ipv4_checksum.update({...});
}
if (ig_md.checksum_upd_tcp) {
    hdr.tcp.checksum = tcp_checksum.update({
        hdr.ipv4.src_addr,
        hdr.ipv4.dst_addr,
        ig_md.checksum_tcp_tmp
    });
}
```

---

## Control Plane

### Architecture

The control plane consists of three main components:

```
┌─────────────────────────────────────────────────────────┐
│                     Flask HTTP API                      │
│                      (port 5000)                        │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                    NodeManager                          │
│  - Manages node state (lb_nodes, all nodes)             │
│  - Handles migrations                                   │
│  - Orchestrates table updates                           │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                  SwitchController                       │
│  - bfrt_grpc connection to switch                       │
│  - Low-level table operations                           │
│  - Insert/Modify/Delete entries                         │
└─────────────────────────────────────────────────────────┘
```

### Configuration

The controller is configured via `controller/controller_config.json`:

```json
[
  {
    "id": 0,
    "client_id": 0,
    "name": "t2na_load_balancer",
    "addr": "127.0.0.1:50052",
    "master": true,
    "load_balancer_ip": "10.0.0.10",
    "service_port": 12345,
    "nodes": [
      {
        "ipv4": "10.0.0.0",
        "sw_port": 11
      },
      {
        "ipv4": "10.0.0.1",
        "is_lb_node": true,
        "sw_port": 24
      },
      {
        "ipv4": "10.0.0.2",
        "is_lb_node": true,
        "sw_port": 2
      }
    ]
  }
]
```

#### Configuration Fields

| Field              | Description                                       |
| ------------------ | ------------------------------------------------- |
| `id`               | Switch device ID                                  |
| `client_id`        | gRPC client ID for bfrt connection                |
| `addr`             | gRPC address of the switch (IP:port)              |
| `master`           | Whether this is the master controller instance    |
| `load_balancer_ip` | Virtual IP (VIP) that clients connect to          |
| `service_port`     | TCP port for the service (used for SNAT matching) |
| `nodes`            | Array of backend nodes                            |

#### Node Configuration

| Field        | Required | Description                                     |
| ------------ | -------- | ----------------------------------------------- |
| `ipv4`       | Yes      | IP address of the node                          |
| `sw_port`    | Yes      | Switch port the node is connected to            |
| `is_lb_node` | No       | If `true`, this node is a load balancer backend |
| `mac`        | No       | MAC address (optional, for future L2 features)  |

### HTTP API

#### POST /migrateNode

Migrate traffic from one backend to another.

**Request:**

```json
{
  "old_ipv4": "10.0.0.2",
  "new_ipv4": "10.0.0.4"
}
```

**Response (success):**

```json
{
  "status": "success"
}
```

**Response (error):**

```json
{
  "error": "Node with IP 10.0.0.2 is not LB node"
}
```

**Example:**

```bash
curl -X POST http://localhost:5000/migrateNode \
  -H "Content-Type: application/json" \
  -d '{"old_ipv4": "10.0.0.2", "new_ipv4": "10.0.0.4"}'
```

#### POST /cleanup

Remove all table entries created by the controller.

**Response:**

```json
{
  "status": "success",
  "message": "Cleanup complete"
}
```

### Node Migration

The migration process updates the ActionProfile member to point to a new destination IP without modifying the selector group membership. This ensures:

1. **Connection consistency**: Existing connections continue to hash to the same member index
2. **Zero downtime**: The member is updated atomically
3. **Seamless transition**: New connections also go to the new backend

**Migration steps:**

1. Modify the action profile entry to rewrite to new IP
2. Add forward table entry for new IP (if not exists)
3. Update internal node state

---

## Packet Flow

### Client to Server

```
Client (10.0.0.100:54321) → Load Balancer VIP (10.0.0.10:8080) → Backend (10.0.0.2:8080)

1. Packet arrives with dst=10.0.0.10
2. node_selector matches on dst=10.0.0.10 (VIP)
3. ActionSelector hashes 5-tuple, selects member index
4. Action rewrites dst=10.0.0.2 (selected backend)
5. forward table matches dst=10.0.0.2, sets egress port
6. Deparser updates checksums
7. Packet sent to backend
```

### Server to Client

```
Backend (10.0.0.2:8080) → Client (10.0.0.100:54321)

1. Packet arrives with src=10.0.0.2, src_port=8080
2. node_selector does NOT match (dst is client IP, not VIP)
3. client_snat matches on src_port=8080
4. Action rewrites src=10.0.0.10 (VIP)
5. forward table matches dst=10.0.0.100, sets egress port
6. Deparser updates checksums
7. Client sees response from VIP
```

---

## ActionSelector and Load Balancing

The load balancer uses Tofino's ActionSelector for consistent hashing:

```p4
Hash<bit<16>>(HashAlgorithm_t.CRC16) sel_hash;
ActionProfile(4) action_selector_ap;
ActionSelector(
    action_selector_ap,     // action profile
    sel_hash,               // hash extern
    SelectorMode_t.FAIR,    // distribution algorithm
    4,                      // max group size
    1                       // max number of groups
) action_selector;
```

### How It Works

1. **Hash Computation**: The 5-tuple (src/dst IP, protocol, src/dst port) is hashed using CRC16
2. **Member Selection**: Hash value selects an active member from the group
3. **Action Execution**: The selected member's action (set_rewrite_dst) is executed

### Fair Mode

`SelectorMode_t.FAIR` ensures traffic is distributed evenly across active members. When a member is deactivated, traffic is redistributed among remaining active members.

### Connection Consistency

Because the hash is computed on the 5-tuple, all packets from the same TCP connection will:

- Compute the same hash value
- Select the same member index
- Go to the same backend server

This is essential for stateful protocols like TCP where session state is maintained on the backend.
