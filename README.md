# p4containerflow-tofino2

P4-based L3/TCP load balancer for Intel Tofino 1/2. This repository contains:

- A P4_16 program targeting T2NA that performs L3 forwarding, connection-consistent load balancing via an ActionSelector, and optional SNAT for server-to-client traffic on a service port.
- A lightweight Python control plane (bfrt_grpc) that programs the switch tables and exposes a tiny HTTP API for live node migration.
- PTF-based tests for dataplane behavior and example controller workflows.
- Makefile automation for building with [open-p4studio](https://github.com/p4lang/open-p4studio) and Intel proprietary SDE components.

## Table of Contents

- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Hardware Setup](#hardware-setup)
  - [Required Files from Intel](#required-files-from-intel)
  - [Environment Variables](#environment-variables)
  - [Quick Setup (All-in-One)](#quick-setup-all-in-one)
  - [Step-by-Step Setup](#step-by-step-setup)
- [Build and Run](#build-and-run)
- [Testing](#testing)
- [Control Plane](#control-plane)
- [Troubleshooting](#troubleshooting)

## Repository Structure

```
p4containerflow-tofino2/
├── open-p4studio/          # Git submodule: open-source Intel P4 Studio SDE
├── load_balancer/
│   └── t2na_load_balancer.p4   # P4 program for T2NA
├── controller/
│   ├── controller.py           # Flask app + gRPC control-plane
│   ├── bf_switch_controller.py # bfrt_grpc helper for table writes
│   ├── node_manager.py         # Table population and node migration
│   ├── controller_config.json  # Configuration file
│   └── run.sh                  # Launcher script
├── test/
│   ├── t2na_load_balancer_dataplane.py   # Dataplane PTF tests
│   └── t2na_load_balancer_controller.py  # Controller integration tests
├── profiles/
│   └── tofino2-hardware.yaml   # P4Studio profile for Tofino 2 hardware
├── scripts/
│   ├── load_kernel_modules.sh  # Helper to load bf kernel modules
│   └── run_p4testgen.sh        # Example p4testgen invocation
├── diagrams/                   # Architecture diagrams
├── Makefile                    # Build automation
└── README.md
```

## Prerequisites

### Operating System

- Ubuntu 22.04 LTS (tested)
- Other Linux distributions may work but are not tested

### Python

**Python 3.11 or earlier is required.** Python 3.12+ removed the `distutils` module which is needed by open-p4studio.

On Ubuntu 22.04, Python 3.10 is the default and works fine. Verify with:

```bash
python3 --version
```

### Git

Git with submodule support is required to clone open-p4studio.

## Hardware Setup

This section describes how to set up the build environment for **real Tofino hardware**. The setup combines the open-source [open-p4studio](https://github.com/p4lang/open-p4studio) with proprietary Intel SDE components.

### Required Files from Intel

You need two files from Intel, available to authorized users via the [Intel Resource & Design Center (RDC)](https://www.intel.com/content/www/us/en/design/resource-design-center.html):

| File | Description | Example |
|------|-------------|---------|
| **SDE** | Intel Barefoot SDE archive | `bf-sde-9.13.4.tgz` |
| **BSP** | Board Support Package for your hardware | `bf-reference-bsp-9.13.4.tgz` |

Extract the SDE archive to a directory (e.g., `/home/user/bf-sde-9.13.4`). The BSP file should remain as a `.tgz` archive.

### Environment Variables

The Makefile uses the following variables:

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SDE` | Yes | Path to extracted Intel SDE directory | `/home/user/bf-sde-9.13.4` |
| `BSP` | Yes | Path to BSP `.tgz` file | `/home/user/bf-reference-bsp-9.13.4.tgz` |
| `ARCH` | No | Tofino architecture: `tf1` or `tf2` (default: `tf2`) | `tf2` |
| `PROFILE` | No | Path to p4studio profile (default: `profiles/tofino2-hardware.yaml`) | `profiles/tofino2-hardware.yaml` |

### Quick Setup (All-in-One)

If you have all prerequisites ready, run the complete setup with a single command:

```bash
# Clone the repository
git clone https://github.com/stano45/p4containerflow-tofino2/tree/main
cd p4containerflow-tofino2

# Run full hardware setup
make setup-hw SDE=/path/to/bf-sde-9.13.4 BSP=/path/to/bf-reference-bsp-9.13.4.tgz
```

This runs all setup steps in sequence. After completion:

```bash
# Source the environment (required in every new terminal)
source ~/setup-open-p4studio.bash

# Build the P4 program
make build

# Run on hardware
make switch
```

### Step-by-Step Setup

If you prefer more control, run each step individually:

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

#### 4. Create Symlink for P4 Program

```bash
make link-p4studio
```

Creates a symlink from `open-p4studio/pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer` to this repository, allowing the build system to find the P4 program.

#### 5. Configure Profile with BSP Path

```bash
make config-profile BSP=/path/to/bf-reference-bsp-9.13.4.tgz
```

Updates the `bsp-path` field in the profile YAML file with your BSP location.

#### 6. Build with Profile

```bash
make build-profile
```

Applies the p4studio profile to build open-p4studio with hardware support. This is a lengthy process (30+ minutes depending on your system).

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
echo 'source ~/setup-open-p4studio.bash' >> ~/.bashrc
```

## Build and Run

After completing the hardware setup and sourcing the environment:

### Build the P4 Program

```bash
make build
```

### Run on Hardware

```bash
# Tofino 2 (default)
make switch

# Tofino 1
make switch ARCH=tf1
```

### Run on Tofino Model (Simulation)

```bash
# Tofino 2 model
make model

# Tofino 1 model
make model ARCH=tf1
```

### Start the Controller

In a separate terminal (with environment sourced):

```bash
make controller
```

## Testing

### Dataplane Tests

```bash
# Tofino 2
make test-dataplane

# Tofino 1
make test-dataplane ARCH=tf1
```

Tests include:
- L3 forwarding (`forward` table)
- Load balancing via ActionSelector
- Bidirectional flows with SNAT
- Dynamic member updates

### Controller Tests

```bash
make test-controller
```

**Note:** Some endpoints tested are disabled in `controller/controller.py`. Enable them or adapt the tests as needed.

## Control Plane

The control plane in `controller/` uses Intel bfrt_grpc to program switch tables.

### Configuration

Edit `controller/controller_config.json`:

```json
{
  "addr": "0.0.0.0:50052",
  "name": "t2na_load_balancer",
  "load_balancer_ip": "10.0.0.100",
  "service_port": 8080,
  "nodes": [
    {"ipv4": "10.0.0.1", "sw_port": 1, "is_lb_node": true},
    {"ipv4": "10.0.0.2", "sw_port": 2, "is_lb_node": true}
  ]
}
```

### HTTP API

**POST /migrateNode**

Migrate traffic from one backend to another:

```bash
curl -X POST http://localhost:5000/migrateNode \
  -H "Content-Type: application/json" \
  -d '{"old_ipv4": "10.0.0.2", "new_ipv4": "10.0.0.4"}'
```

### Tables Programmed

- `SwitchIngress.client_snat`: SNAT for server→client traffic
- `SwitchIngress.action_selector_ap`: Action profile members
- `SwitchIngress.action_selector`: Selector group
- `SwitchIngress.node_selector`: Maps VIP to selector group
- `SwitchIngress.forward`: L3 forwarding table

## Troubleshooting

### Python Version Error

```
ERROR: Python 3.11 or earlier is required
```

Install Python 3.10 or 3.11 and ensure it's the default `python3`:

```bash
sudo apt install python3.10
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1
```

### SDE Directory Not Found

```
ERROR: SDE directory does not exist
```

Ensure you've extracted the SDE archive and provided the correct path:

```bash
tar xzf bf-sde-9.13.4.tgz
make extract-sde SDE=/full/path/to/bf-sde-9.13.4
```

### bf-drivers Not Found

```
ERROR: bf-drivers directory not found
```

Run `make extract-sde` before `make setup-rdc`:

```bash
make extract-sde SDE=/path/to/bf-sde-9.13.4
make setup-rdc SDE=/path/to/bf-sde-9.13.4
```

### Device Not Found (/dev/fpga0)

Load the kernel modules:

```bash
./scripts/load_kernel_modules.sh
```

Consider adding this as a startup service for persistence across reboots.

### bfrt_python Not Showing Program

- Press `Ctrl+C` and restart bfshell
- Verify the P4 program name in `controller_config.json` matches the loaded pipeline
- Check that switchd is running with the correct program: `make switch`

### Commands Not Found After Setup

Ensure you've sourced the environment script:

```bash
source ~/setup-open-p4studio.bash
```

Verify tools are available:

```bash
which run_switchd.sh
which p4studio
```

## License

See LICENSE file for details.

## Contact

For Intel SDE and BSP access, contact: intel.tofino.contact@intel.com
