# P4ContainerFlow: Technical Report

This document describes the design and implementation of P4ContainerFlow on Intel Tofino hardware. It covers the build environment (open-p4studio), practical challenges when working with the Tofino switch, the P4 program architecture and its rewrite from V1Model to T2NA, and the Python control plane.

## Setting Up Open-P4Studio

Compiling and running P4 programs on an Intel Tofino switch requires the P4 Software Development Environment (SDE). This project uses open-p4studio, the open-source variant of Intel's P4Studio, pinned as a Git submodule at SDE version 9.13.4. The build system wraps the process behind - [P4ContainerFlow: Technical Report](#p4containerflow-technical-report)
- [P4ContainerFlow: Technical Report](#p4containerflow-technical-report)
  - [Setting Up Open-P4Studio](#setting-up-open-p4studio)
    - [Prerequisites](#prerequisites)
    - [Model Setup (Software Simulation)](#model-setup-software-simulation)
    - [Hardware Setup](#hardware-setup)
  - [Challenges and Solutions When Using the Tofino Switch](#challenges-and-solutions-when-using-the-tofino-switch)
    - [Testbed Topology](#testbed-topology)
    - [Port Configuration and FEC](#port-configuration-and-fec)
    - [ARP and Static Routing](#arp-and-static-routing)
    - [Kernel Modules and Device Access](#kernel-modules-and-device-access)
    - [CPU Port and Internet Access](#cpu-port-and-internet-access)
    - [gRPC Connection Lifecycle](#grpc-connection-lifecycle)
  - [Rewriting a P4 Program from V1Model to T2NA](#rewriting-a-p4-program-from-v1model-to-t2na)
    - [Pipeline Overview](#pipeline-overview)
    - [Pipeline Structure](#pipeline-structure)
    - [Metadata](#metadata)
    - [Checksum Handling](#checksum-handling)
    - [Load Balancing with ActionSelector](#load-balancing-with-actionselector)
    - [Ingress Apply Logic](#ingress-apply-logic)
    - [Packet Flow](#packet-flow)
    - [Compiler Constraints](#compiler-constraints)
  - [The Control Plane](#the-control-plane)
    - [Architecture](#architecture)
    - [Configuration](#configuration)
    - [Table Management](#table-management)
    - [HTTP API](#http-api)
    - [Controller Startup Script](#controller-startup-script)

### Prerequisites

The host system must run Ubuntu 22.04 LTS (other distributions have not been tested) and Python 3.11 or earlier. Python 3.12 and later versions removed the `distutils` module, which open-p4studio still depends on. If the system's default Python is too recent, the build will fail with an import error that gives no hint about the actual cause. The Makefile includes a `make check-python` target that catches this early.

A Vagrantfile is included for reproducible development environments. It provisions an Ubuntu 22.04 VM with 32 GB of RAM, 10 CPUs, and 50 GB of disk, then runs the full model setup automatically. This is the easiest way to get a working build without fighting the host system's Python or library versions.

### Model Setup (Software Simulation)

For development without Tofino hardware, the SDE ships a software model that simulates the ASIC pipeline. Running `make setup-model` handles the full sequence: it initializes the Git submodule (`make init-submodule`), applies the build profile at `profiles/tofino2-model.yaml`, compiles the SDE and all its dependencies, and finally generates an environment script. After the build completes, every new terminal session must source that script:

```bash
source ~/setup-open-p4studio.bash
```

This sets `PATH`, `SDE`, `SDE_INSTALL`, and other variables that the compiler, model, and runtime tools expect. Forgetting to source it is the most common reason for "command not found" errors during development.

The model build profile (`profiles/tofino2-model.yaml`) is minimal. It enables `bfrt` (BF Runtime table access), `grpc` (for the controller to connect remotely), and `thrift-driver` (for bfshell and interactive debugging). It targets the `tofino2` architecture. It does not include platform libraries, board support, or SAI, because the software model does not interact with physical hardware.

Running the model requires three terminals. The first runs `make model` to start the `tofino-model` simulator process. The second runs `make model-switch` to start `bf_switchd`, which connects to the model over a local TCP socket. The third is used for tests or the controller. It is clunky, but the real hardware workflow works the same way: `switchd` is always a separate process from whatever controls it.

### Hardware Setup

Building for real hardware is harder than the model path. Open-p4studio is an incomplete SDE on its own. It ships with stub implementations for the driver layer, particularly the port manager and serdes interfaces, because those components contain proprietary IP (Avago/Broadcom serdes firmware, Credo/Alphawave PHY drivers, platform-specific register access code). To produce a working hardware build, you need two additional archives from Intel, available only to authorized users through the Intel Resource and Design Center (RDC): the full proprietary SDE (for example `bf-sde-9.13.4.tgz`) and the Board Support Package for the specific switch platform (for example `bf-reference-bsp-9.13.4.tgz`).

The Makefile provides `make setup-hw SDE=... BSP=...`, but it is worth understanding what it does. Things break, and when they do, you need to know which step failed and why.

**Step 1: Extract the proprietary SDE.** The Intel SDE archive contains an `extract_all.sh` script that unpacks its internal packages (bf-drivers, bf-utils, p4-compilers, and others) into versioned directories. Running `make extract-sde SDE=/path/to/bf-sde-9.13.4` calls this script. The result is a directory tree like `bf-sde-9.13.4/bf-drivers-9.13.4/`, which contains the proprietary driver sources that open-p4studio lacks.

**Step 2: Graft proprietary driver files into the open-source tree.** This is the critical step that makes the hardware build possible. Open-p4studio includes a script at `open-p4studio/hw/rdc_setup.sh` that defines a `rdc_setup` function. This function copies specific files and directories from the proprietary `bf-drivers` into the open-source `pkgsrc/bf-drivers`, replacing the stubs with real implementations. The files it copies include the `bf_switchd` entry point (`bf_switchd.c` and its `CMakeLists.txt`), the Avago serdes libraries (`libavago*`), PHY driver directories for Alphawave, Credo, Avago, and MicroP, and dozens of platform-specific source files in `src/port_mgr/port_mgr_tof1/`, `port_mgr_tof2/`, and `port_mgr_tof3/`. Running `make setup-rdc` patches the two path variables (`RDC_BFD` and `OS_BFD`) in `rdc_setup.sh` to point at the correct directories for your installation, then sources and runs the function. If the SDE version does not match the open-p4studio version, the file lists in `rdc_setup.sh` may reference files that do not exist, and the copy will fail silently for those entries. There is no version compatibility check.

**Step 3: Configure the build profile with the BSP path.** The hardware build profile (`profiles/tofino2-hardware.yaml`) differs from the model profile in several ways. It sets `global-options.asic: true`, enables `bf-platforms` (the board support layer) with the `newport` platform variant, enables `switch` with the `y2_tofino2` profile and SAI support, and disables TDI. The BSP path is written directly into the YAML file by `make config-profile`, which uses `sed` to replace the `bsp-path:` value. The model profile needs none of this because the software model does not require platform-specific hardware abstraction.

**Step 4: Extract the BSP into pkgsrc.** The BSP archive is a nested tarball. The outer archive (`bf-reference-bsp-9.13.4.tgz`) contains a directory with a `packages/` subdirectory, which itself contains a `bf-platforms-*.tgz` tarball. Running `make extract-bsp` unpacks the outer tarball into a temporary directory, locates the `bf-platforms` tarball inside it, extracts that into `open-p4studio/pkgsrc/`, and renames the versioned directory to `bf-platforms`. This provides the board-specific code (QSFP management, platform detection, LED control, fan/thermal monitoring) that `bf_switchd` loads at runtime through `libpltfm_mgr.so`.

**Step 5: Build everything.** With the proprietary files grafted and the BSP in place, `make build-profile` invokes `p4studio profile apply` on the hardware profile. This runs CMake configuration followed by a full build of the SDE, including the compiler toolchain (`p4c-barefoot`, `bfas`), the driver stack (`bf-drivers` with the real port manager), the platform library, and all enabled features. On a fast multi-core machine this takes 30 or more minutes. On the Tofino switch itself (which is typically an embedded Xeon), it can exceed an hour.

**The switchd configuration file.** After the build, `switchd` is configured through a JSON file (`t2na_load_balancer.conf`) that specifies the chip family, PCIe sysfs prefix, firmware paths, the compiled P4 pipeline (the `tofino2.bin` binary and `context.json`), and importantly, the `agent0` field. In the hardware conf, `agent0` is set to `"lib/libpltfm_mgr.so"`, which tells `switchd` to load the platform manager and talk to the real ASIC over PCIe. The model conf omits `agent0` entirely, causing `switchd` to default to model mode and communicate with the `tofino-model` simulator over a local TCP socket instead. This single field is the difference between targeting real hardware and targeting the simulator.

**Compiling the P4 program.** Once the SDE is built and the environment is sourced, `make build` compiles the P4 program using `p4c-barefoot`. The compiler produces a `.bfa` (Barefoot Assembly) file, which is then passed through the `bfas` assembler to produce `tofino2.bin`, the binary loaded by `switchd`. The build also emits `bf-rt.json` (the BF Runtime schema that the controller uses to discover tables) and `context.json` (pipeline context for `switchd`). Common P4 includes (`headers.p4`, `util.p4`) are copied from the SDE's example programs into `load_balancer/common/` the first time the program is compiled. The `make install` target copies all build artifacts into `$SDE_INSTALL/share/p4/targets/tofino2/t2na_load_balancer/` and places the conf file alongside them.

**Pain points.** The build has no real incremental mode. Changing a build profile option or updating the submodule often means rebuilding from scratch. The `rdc_setup.sh` file list is manually maintained and can drift between SDE versions. Dependency resolution in open-p4studio can break silently when system packages change. If the Avago libraries are missing or the wrong version, `switchd` will compile but crash at runtime with opaque dlopen errors. Pinning the OS and Python version is not optional. Things will break otherwise.

## Challenges and Solutions When Using the Tofino Switch

The Tofino switch has a lot of undocumented behavior that you only discover by running into it. This section describes the issues we hit during development and what we did about them.

### Testbed Topology

```
          Management Network (out of band, SSH access)
          .........+..............+..............+
     +---------+    +---------+        +---------+
     | Tofino  |    |lakewood |        |loveland |
     | Switch  |    |Dell R740|        |Dell R740|
     |Wedge100 |    |20c/192G |        |20c/192G |
     |  BF-32X |    |         |        |         |
     +----+----+    +--+---+--+        +--+---+--+
          |            |   |              |   |
          | 25G NFP    |   | 25G Mellanox |   | 25G NFP
          | (switch)   |   | (direct DAC) |   | (switch)
     Port |            |   |              |   |
     2/0  +------------+   +--------------+   |
    D_P140| RS FEC         server-to-server   |
          |                 checkpoint xfer   |
     Port |                                   |
     3/0  +-----------------------------------+
    D_P148| RS FEC
          |
     Port |
    33/0  + CPU PCIe (D_P 64, internal)
    D_P 64  via bf_kpkt/veth250
```

### Port Configuration and FEC

The testbed consists of a Wedge100BF-32X switch (Tofino 1, 32 QSFP28 ports) connected to two Dell R740 servers, lakewood and loveland. The servers use Netronome NFP NICs at 25G through the switch and Mellanox ConnectX NICs on direct server-to-server 25G DAC links. All switch ports run at 25G with Reed-Solomon Forward Error Correction (RS FEC). During testing, setting FEC to `NONE` caused link failures after a fresh `switchd` start, even though the same cables worked fine with RS FEC enabled. The controller configures ports automatically through entries in `controller_config_hw.json`, using the `$PORT` BF Runtime table to set speed, FEC type, and auto-negotiation per device port. Port 2/0 (device port 140) connects to lakewood and port 3/0 (device port 148) connects to loveland.

One minor annoyance: lakewood's Netronome interfaces carry `np0`/`np1` suffixes (for example `enp101s0np0`), while loveland's interfaces do not. Both servers are identical hardware running the same driver version. We never figured out why the naming differs. You just have to know.

### ARP and Static Routing

The P4 program forwards only IPv4 and ARP packets. Everything else is dropped at the parser. Because the load balancer rewrites destination addresses, it cannot rely on the kernel's normal ARP resolution. Static ARP entries must be configured on both servers for any IP address reachable through the switch. Without them, the first packet to a new destination triggers an ARP request that the switch either drops (if it is not IPv4) or forwards to the wrong host. The `arp_forward` table in the P4 program handles ARP forwarding based on the target protocol address, but only for addresses that have been explicitly programmed by the controller.

### Kernel Modules and Device Access

The Tofino ASIC is accessed through `/dev/fpga0`, which requires the Barefoot kernel modules to be loaded. Three modules are relevant: `bf_kdrv` (the core FPGA driver), `bf_kpkt` (kernel packet path), and `bf_knet` (kernel network interface). After a reboot, these modules are not loaded automatically unless a systemd service has been set up for it. Running `make load-kmods` iterates through all three modules and invokes their load scripts from `$SDE_INSTALL/bin/`. In the original installation (before the Makefile was in place), a custom startup service was written for this purpose. If `switchd` fails with "device not found," the kernel modules are almost certainly the cause.

### CPU Port and Internet Access

The Tofino exposes a CPU port (device port 64) accessible via the `bf_kpkt` kernel module and `veth250`. This port could bridge internet traffic into the P4 data plane, but a configuration issue in `bf_switchd` prevents it from working: when `bf_kpkt` is loaded, `switchd` skips the packet manager initialization, leaving `pkt_extraction_credits` at zero for device port 64. The ASIC can inject packets into the CPU port (TX works), but it cannot extract packets from it (RX is blocked). After spending time debugging this, we gave up on the CPU port approach. Instead, a macvlan sub-interface ("shim") on lakewood provides host-level access to the container subnet (`192.168.12.0/24`), and SSH tunnels carry metrics traffic to the control machine.

### gRPC Connection Lifecycle

The Python controller communicates with `switchd` over gRPC using the BF Runtime (bfrt) interface. When `switchd` is restarted, the gRPC channel becomes stale and the controller must be restarted as well to establish a fresh connection. The experiment automation script detects this by checking a `$SWITCHD_FRESH` flag and restarting the controller if the switch process was recently launched. Another annoyance: `bfrt_python` (the interactive P4 shell) sometimes does not display the loaded program after connecting. Pressing Ctrl+C and restarting `bfshell` resolves the issue. The program name reported by `bfrt_python` must match the `p4_name` field in `controller_config.json`; a mismatch results in silent failures when the controller tries to access tables.

## Rewriting a P4 Program from V1Model to T2NA

The load balancer was originally written for the V1Model architecture, the reference software switch used by BMv2. Deploying it on Tofino 2 required a rewrite targeting the Tofino 2 Native Architecture (T2NA). Both use P4-16, but the pipeline structure, available externs, metadata handling, and compiler constraints are quite different.

### Pipeline Overview

```
                              T2NA Pipeline
  +------------------------------------------------------------------+
  |                         INGRESS                                  |
  |                                                                  |
  |  +------------------+   +--------------+   +------------------+  |
  |  | IngressParser    |   | Ingress      |   | IngressDeparser  |  |
  |  |                  |   |              |   |                  |  |
  |  | TofinoParser()   |-->| node_selector|-->| if checksum_upd: |  |
  |  | Ethernet/IPv4/TCP|   | client_snat  |   |   recompute IPv4 |  |
  |  | Checksum verify  |   | forward      |   |   recompute TCP  |  |
  |  | TCP partial cksum|   | bypass_egress|   | emit(hdr)        |  |
  |  +------------------+   +--------------+   +------------------+  |
  +------------------------------------------------------------------+
  |                         EGRESS (bypassed)                        |
  |  +------------------+   +--------------+   +------------------+  |
  |  | EmptyEgressParser|   | EmptyEgress  |   |EmptyEgressDeparse|  |
  |  +------------------+   +--------------+   +------------------+  |
  +------------------------------------------------------------------+
```

### Pipeline Structure

In V1Model, the pipeline is declared as `V1Switch(parser, verify, ingress, egress, compute, deparser)`, where checksum verification and computation are separate control blocks that run before ingress and after egress, respectively. T2NA uses a different ordering: `Pipeline(IngressParser, Ingress, IngressDeparser, EgressParser, Egress, EgressDeparser)`. Checksum operations live inside the parser and deparser directly, and there are no standalone verify or compute blocks. The final declaration in the rewritten program is:

```p4
Pipeline(SwitchIngressParser(),
         SwitchIngress(),
         SwitchIngressDeparser(),
         EmptyEgressParser(),
         EmptyEgress(),
         EmptyEgressDeparser()) pipe;

Switch(pipe) main;
```

Since the load balancer performs all processing in ingress, the egress pipeline is left empty and bypassed entirely using the `BypassEgress()` extern. V1Model has no equivalent mechanism for skipping egress. The empty egress blocks (`EmptyEgressParser`, `EmptyEgress`, `EmptyEgressDeparser`) come from `common/util.p4`, a shared include from the SDE's example programs. Similarly, `TofinoIngressParser()` is defined there and must be called in every parser's `start` state to extract platform-specific metadata before any user headers.

### Metadata

V1Model provides a single `standard_metadata` struct that contains fields like `ingress_port`, `egress_spec`, and `packet_length`. Tofino replaces this with four separate intrinsic metadata structs: `ingress_intrinsic_metadata_t` (port, timestamp), `ingress_intrinsic_metadata_from_parser_t` (parser errors), `ingress_intrinsic_metadata_for_deparser_t` (digest, resubmit control), and `ingress_intrinsic_metadata_for_tm_t` (egress port, QoS). The ingress control block signature in T2NA takes all four as parameters.

The program also defines its own `metadata_t` struct carrying five fields: `is_lb_packet` (whether the packet matched the load balancer table), `checksum_err_ipv4_igprs` (parser checksum verification result), `checksum_tcp_tmp` (partial TCP checksum deposited by the parser), `checksum_upd_ipv4` and `checksum_upd_tcp` (flags that tell the deparser whether to recompute checksums). These flags are set by actions like `set_rewrite_dst` and `set_rewrite_src` when they modify header fields, so the deparser only pays the cost of checksum recomputation for packets that actually had their addresses changed.

### Checksum Handling

Checksum computation took the most effort to get right. In V1Model, the `MyVerifyChecksum` and `MyComputeChecksum` control blocks run as standalone pipeline stages. On Tofino, checksumming units are only available in the parser and deparser sections. The rewritten parser verifies the IPv4 checksum by calling `ipv4_checksum.add(hdr.ipv4)` followed by `ipv4_checksum.verify()`. For TCP, it computes a partial checksum by subtracting the fields that might be modified (source and destination addresses) using `tcp_checksum.subtract()`, then deposits the intermediate result into metadata via `subtract_all_and_deposit()`. The deparser conditionally recomputes both checksums if the ingress logic modified any addresses, using the deposited partial value to avoid reprocessing the entire TCP payload.

This subtract-then-redeposit pattern is the standard Tofino approach for incremental checksum updates. It avoids reading the full packet payload in the deparser, which would consume parse bandwidth. The trick is that you subtract the old values of the mutable fields in the parser, store the residual, and then in the deparser you add the new values back along with that residual.

### Load Balancing with ActionSelector

V1Model implementations of consistent hashing typically use `hash()` calls inside actions with manual member selection logic. T2NA provides a native `ActionSelector` extern that handles this at the hardware level. The load balancer declares a CRC16 hash, an `ActionProfile` with capacity for four members, and an `ActionSelector` that ties them together in `FAIR` mode:

```p4
Hash<bit<16>>(HashAlgorithm_t.CRC16) sel_hash;
ActionProfile(4) action_selector_ap;
ActionSelector(action_selector_ap, sel_hash, SelectorMode_t.FAIR, 4, 1) action_selector;
```

The `node_selector` table uses the five-tuple (source IP, destination IP, protocol, source port, destination port) as selector keys, with the destination address as an exact-match key. The exact-match key identifies the virtual IP; the selector keys determine which backend member the connection hashes to. The action `set_rewrite_dst` rewrites the destination address and marks checksum flags. `SelectorMode_t.FAIR` ensures traffic is distributed evenly across active members. When a member is deactivated (its `$ACTION_MEMBER_STATUS` is set to inactive), traffic is automatically redistributed among the remaining active members. Because the hash is computed on the five-tuple, all packets from the same TCP connection compute the same hash value, select the same member index, and go to the same backend server. Without this, TCP connections would break whenever traffic gets redistributed.

A separate `forward` table maps the rewritten destination to an egress port. The `client_snat` table matches on `hdr.tcp.src_port` (exact) and rewrites the source IP to the VIP for server-to-client replies. The `arp_forward` table matches on `hdr.arp.target_proto_addr` (exact) and sets the egress port for ARP packets. Splitting this across multiple tables means forwarding rules can be updated independently during migration without touching the load balancer logic.

At the BF Runtime level, the ActionSelector is programmed through three related tables. The `action_selector_ap` table maps `$ACTION_MEMBER_ID` (an integer index, 0 through 3) to a `set_rewrite_dst` action with the backend's IP address. The `action_selector` table defines a group identified by `$SELECTOR_GROUP_ID`, with `$MAX_GROUP_SIZE` set to 4, and arrays of `$ACTION_MEMBER_ID` and `$ACTION_MEMBER_STATUS` (active or inactive) that control which members participate. The `node_selector` table ties a VIP (exact match on destination address) to this group. When the controller needs to migrate traffic, it modifies the action profile member's destination IP via the `action_selector_ap` table. The group membership and hash remain unchanged, so the member index stays the same and existing connections are not disrupted.

### Ingress Apply Logic

```
                     Ingress Apply Flow

                    +-------------+
                    | Packet In   |
                    +------+------+
                           |
                    +------v------+
                    | ARP valid?  |--yes--> arp_forward --> bypass_egress --> out
                    +------+------+
                           | no
                    +------v------+
                    | IPv4 valid  |--no---> drop
                    | && TTL >= 1 |
                    +------+------+
                           | yes
                    +------v------+
                    |node_selector|  (ActionSelector: hash 5-tuple,
                    | match VIP?  |   rewrite dst to backend IP)
                    +------+------+
                           |
                    +------v------+
                    |is_lb_packet |--yes--> skip SNAT
                    |   == false? |
                    +------+------+
                           | no (server reply)
                    +------v------+
                    | client_snat |  (rewrite src IP to VIP)
                    +------+------+
                           |
                    +------v------+
                    |   forward   |  (set egress port)
                    +------+------+
                           |
                    +------v------+
                    |bypass_egress|
                    +------+------+
                           |
                    +------v------+
                    | Packet Out  |
                    +-------------+
```

The ingress `apply` block processes packets in a specific order. ARP packets hit `arp_forward`, bypass egress, and return immediately. Invalid IPv4 packets (bad header or TTL below 1) are silently dropped. Valid IPv4/TCP packets go through `node_selector` (which may rewrite the destination to a backend server), then `client_snat` (which rewrites the source address for server-to-client replies, but only if the packet was not a load-balanced packet), then `forward` (which sets the egress port), and finally `bypass_egress`. Packets with IPv4 checksum errors are tagged by overwriting the destination MAC with `0x0000deadbeef` for debugging purposes rather than being dropped, which makes them easy to identify in packet captures.

### Packet Flow

A concrete example makes the table interactions easier to follow.

For a client-to-server packet, a client at `10.0.0.100:54321` sends a TCP packet to the VIP `10.0.0.10:8080`. The parser extracts Ethernet, IPv4, and TCP headers and verifies the IPv4 checksum. In ingress, the `node_selector` table matches on `dst_addr = 10.0.0.10` (the VIP). The ActionSelector hashes the five-tuple and selects a member index, say member 0, whose action rewrites the destination to `10.0.0.2` (the chosen backend). The `is_lb_packet` flag is set to true, so `client_snat` is skipped (SNAT only applies to server responses). The `forward` table matches on `dst_addr = 10.0.0.2` and sets the egress port. The deparser recomputes the IPv4 and TCP checksums because the destination address changed. The packet arrives at the backend server.

For the return path, the backend at `10.0.0.2:8080` sends a response to `10.0.0.100:54321`. The `node_selector` table does not match because the destination is the client's real IP, not the VIP. The `is_lb_packet` flag remains false, so `client_snat` is applied. It matches on `src_port = 8080` (the service port) and rewrites the source address from `10.0.0.2` to `10.0.0.10` (the VIP). The `forward` table matches on `dst_addr = 10.0.0.100` and sets the egress port toward the client. The deparser recomputes checksums. The client sees the response coming from the VIP, unaware of which backend handled it.

### Compiler Constraints

The Tofino compiler enforces hardware constraints that BMv2 does not care about at all. Tables must fit into physical pipeline stages. The Packet Header Vector (PHV) has a fixed allocation budget. Hash distribution units are shared resources. Multi-step arithmetic that works on BMv2 may need to be restructured to fit within a single stage or split across stages in a way the compiler can schedule. Some of these constraints only show up during compilation, which slows down iterative development. The P4 source uses conditional compilation (`#if __TARGET_TOFINO__ == 3 ... #elif __TARGET_TOFINO__ == 2 ... #else ... #endif`) to support TNA, T2NA, and T3NA from a single file, selecting the correct platform includes at compile time.

## The Control Plane

The control plane is a Python application with three parts: a Flask HTTP API (`controller.py`), a BF Runtime switch controller (`bf_switch_controller.py`), and a node manager (`node_manager.py`). It runs on the switch itself and talks to `switchd` over a localhost gRPC connection.

```
 HTTP clients                          Tofino Switch
 (experiment                     +---------------------------+
  scripts,                       |                           |
  curl)                          |  Flask API  (port 5000)   |
    |                            |  /migrateNode             |
    |   POST /updateForward      |  /updateForward           |
    +--------------------------->|  /cleanup                  |
                                 |  /reinitialize            |
                                 +----------|----------------+
                                            |
                                 +----------v----------------+
                                 |      NodeManager          |
                                 |  nodes{}  lb_nodes{}      |
                                 |  migrateNode()            |
                                 |  updateForward()          |
                                 +----------|----------------+
                                            |
                                 +----------v----------------+
                                 |    SwitchController       |
                                 |    (bf_switch_controller)  |
                                 |                           |
                                 |  gRPC to bf_switchd       |
                                 |  127.0.0.1:50052          |
                                 +----------|----------------+
                                            |
                                 +----------v----------------+
                                 |      bf_switchd           |
                                 |  P4 pipeline (ASIC)       |
                                 |  node_selector            |
                                 |  action_selector          |
                                 |  forward / arp_forward    |
                                 |  client_snat              |
                                 +---------------------------+
```

### Architecture

On startup, the controller loads a JSON configuration file that describes the switch connection, the P4 program name, the virtual IP, the service port, port setup parameters, and the initial set of nodes. It selects the correct program name based on the `ARCH` environment variable (`tna_load_balancer` for Tofino 1, `t2na_load_balancer` for Tofino 2). The `SwitchController` connects to `switchd` via the `bfrt_grpc.client` library, binds to the P4 pipeline, and optionally configures physical ports (speed, FEC, auto-negotiation) through the `$PORT` BF Runtime table. The `NodeManager` then inserts the initial forwarding and load balancing entries.

### Configuration

The controller is configured via a JSON file (either `controller_config.json` for model or `controller_config_hw.json` for hardware). The file is a JSON array of switch entries, each containing `id` (device ID), `client_id` (gRPC client ID), `name` (P4 program name), `addr` (gRPC address, typically `127.0.0.1:50052`), `master` (boolean, whether this is the master controller instance), `load_balancer_ip` (the VIP that clients connect to), and `service_port` (TCP port used for SNAT matching, for example 8080). The hardware config adds a `port_setup` array with per-port entries specifying `dev_port`, `speed` (for example `BF_SPEED_25G`), `fec` (for example `BF_FEC_TYP_REED_SOLOMON`), and `auto_neg`. It also adds an optional `dst_mac` per node for L2 forwarding.

Each entry also contains a `nodes` array. Each node has a required `ipv4` address and `sw_port` (the switch port it is connected to), an optional `is_lb_node` boolean (if true, this node participates in load balancing as a backend), and an optional `dst_mac` for the forwarding table's MAC rewrite action. Nodes that are not load balancer backends still get forward and ARP entries but are not added to the ActionSelector group.

### Table Management

The `NodeManager` maintains two pieces of state: a map from IPv4 address to node metadata, and a map from IPv4 address to action profile member index for load-balanced nodes. During setup, it inserts entries into all five P4 tables. The `node_selector` table gets a group entry (the `ActionSelector` group) whose members point to `set_rewrite_dst` actions. The `action_selector_ap` profile gets one member per backend node. The `forward` and `arp_forward` tables get entries for every node. The `client_snat` table gets an entry for the service port (mapping server replies back to the VIP).

Table cleanup has to follow a specific order: `node_selector` first (because it references the action selector group), then `action_selector` (the group), then `action_selector_ap` (the members), and finally `forward`, `arp_forward`, and `client_snat`. If you delete them in the wrong order, BF Runtime throws gRPC errors because of referential integrity.

### HTTP API

The controller exposes six HTTP endpoints. `POST /migrateNode` takes `old_ipv4` and `new_ipv4` and updates the action profile member and forwarding entries to point to the new server. `POST /updateForward` takes `ipv4`, `sw_port`, and an optional `dst_mac` and updates only the forwarding and ARP tables, which is used for same-IP migration where only the physical port changes. `POST /addForward` inserts a new forwarding entry. `POST /deleteClientSnat` removes the SNAT entry for the service port, which is necessary for same-IP migration (otherwise the SNAT rule rewrites the source address to the VIP and the client's TCP stack rejects it). `POST /cleanup` removes all table entries. `POST /reinitialize` does a cleanup followed by a full re-insertion from the original configuration.

### Controller Startup Script

The `run.sh` script deals with the awkward Python path setup needed for the BF Runtime gRPC client. The SDE installs its Python libraries (bfrt_grpc, tofino, p4testutils) into a non-standard location under `$SDE_INSTALL`. The script builds a `PYTHONPATH` that includes these directories but carefully filters out the SDE's bundled gRPC package to avoid conflicting with the version installed in the controller's own virtual environment (managed by `uv`). If you get the path wrong, the symptom is usually missing method attributes or protobuf version mismatches at import time.
