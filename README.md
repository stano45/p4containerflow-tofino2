# p4containerflow-tofino2

P4-based L3/TCP load balancer for Intel Tofino 1/2. This repository contains:

- A P4_16 program targeting T2NA that performs L3 forwarding, connection-consistent load balancing via an ActionSelector, and optional SNAT for server-to-client traffic on a service port.
- A lightweight Python control plane (bfrt_grpc) that programs the switch tables and exposes a tiny HTTP API for live node migration.
- PTF-based tests for dataplane behavior and example controller workflows.
- Makefile automation for building with [open-p4studio](https://github.com/p4lang/open-p4studio) and Intel proprietary SDE components.

This README is a practical guide: how to build, run, and test the project. For deeper context, see the `docs/` directory:

- **[docs/technical_report.md](docs/technical_report.md)** -- How everything works: open-p4studio build internals, hardware challenges, the V1Model-to-T2NA rewrite, packet flows, and the control plane architecture.
- **[docs/experiment_report.md](docs/experiment_report.md)** -- Benchmarking setup, migration procedure, metrics collection, and results from a 25-migration experiment run.
- **[docs/topology.md](docs/topology.md)** -- Lab testbed reference: equipment, IPs, interfaces, and cabling.
- **[docs/cpu-port-internet-access.md](docs/cpu-port-internet-access.md)** -- Investigation into using the Tofino CPU port for container internet access (abandoned; documents the macvlan-shim workaround).

## Table of Contents
- [p4containerflow-tofino2](#p4containerflow-tofino2)
  - [Table of Contents](#table-of-contents)
  - [Repository Structure](#repository-structure)
  - [Prerequisites](#prerequisites)
    - [Operating System](#operating-system)
    - [Python](#python)
    - [Git](#git)
    - [uv (Optional)](#uv-optional)
  - [Model Setup (Simulation)](#model-setup-simulation)
    - [Quick Setup](#quick-setup)
    - [Building the P4 Program](#building-the-p4-program)
    - [Running the Model](#running-the-model)
      - [1. Run the Tofino Model](#1-run-the-tofino-model)
      - [2. Run the Switch Daemon](#2-run-the-switch-daemon)
      - [3. Start the Controller](#3-start-the-controller)
    - [Running Tests (Model)](#running-tests-model)
      - [Dataplane Tests](#dataplane-tests)
      - [Controller Tests](#controller-tests)
    - [Clean Targets](#clean-targets)
  - [Hardware Setup](#hardware-setup)
    - [Required Files from Intel](#required-files-from-intel)
    - [Environment Variables](#environment-variables)
    - [Available Profiles](#available-profiles)
    - [Quick Setup (All-in-One)](#quick-setup-all-in-one)
    - [Building the P4 Program](#building-the-p4-program-1)
    - [Running on Hardware](#running-on-hardware)
      - [1. Load Kernel Modules](#1-load-kernel-modules)
      - [2. Run the Switch](#2-run-the-switch)
      - [3. Start the Controller](#3-start-the-controller-1)
    - [Running Tests (Hardware)](#running-tests-hardware)
      - [Dataplane Tests](#dataplane-tests-1)
      - [Controller Tests](#controller-tests-1)
    - [Clean Targets](#clean-targets-1)
  - [Troubleshooting](#troubleshooting)
    - [Python Version Error](#python-version-error)
    - [SDE Directory Not Found](#sde-directory-not-found)
    - [bf-drivers Not Found](#bf-drivers-not-found)
    - [Device Not Found (/dev/fpga0)](#device-not-found-devfpga0)
    - [bfrt\_python Not Showing Program](#bfrt_python-not-showing-program)
    - [Commands Not Found After Setup](#commands-not-found-after-setup)
  - [License](#license)
  - [Contact](#contact)



## Repository Structure

```
p4containerflow-tofino2/
├── open-p4studio/          # Git submodule: open-source Intel P4 Studio SDE
├── load_balancer/
│   ├── t2na_load_balancer.p4   # P4 program for Tofino 2 (T2NA)
│   ├── t2na_load_balancer.conf # Switchd config for Tofino 2
│   └── tna_load_balancer.conf  # Switchd config for Tofino 1
├── controller/
│   ├── controller.py           # Flask app + gRPC control-plane
│   ├── bf_switch_controller.py # bfrt_grpc helper for table writes
│   ├── node_manager.py         # Table population and node migration
│   ├── controller_config.json  # Configuration file
│   └── run.sh                  # Launcher script
├── test/
│   ├── model/                       # PTF-based tests (tofino-model)
│   │   ├── test_dataplane.py        # Dataplane functionality tests
│   │   └── test_controller.py       # Controller integration tests
│   └── hardware/                    # Pytest-based tests (real hardware)
│       ├── test_dataplane.py        # Low-level table operations
│       ├── test_controller.py       # HTTP API tests
│       └── run.sh                   # Test runner
├── profiles/
│   ├── tofino2-hardware.yaml   # P4Studio profile for Tofino 2 hardware
│   ├── tofino2-model.yaml      # P4Studio profile for Tofino 2 model
│   ├── tofino-hardware.yaml    # P4Studio profile for Tofino 1 hardware
│   └── tofino-model.yaml       # P4Studio profile for Tofino 1 model
├── build/                      # Build output directory (generated)
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

### uv (Optional)

[uv](https://docs.astral.sh/uv/) is used for Python dependency management in the controller. If not installed, it will be installed automatically when running the controller.

## Model Setup (Simulation)

If you don't have Tofino hardware and want to run on the Tofino model (software simulation), follow these steps.

### Quick Setup

Run the complete model setup with one make target:

**Tofino 2 (default):**

```bash
git clone https://github.com/stano45/p4containerflow-tofino2
cd p4containerflow-tofino2
make setup-model
```

**Tofino 1:**

```bash
git clone https://github.com/stano45/p4containerflow-tofino2
cd p4containerflow-tofino2
make setup-model PROFILE=profiles/tofino-model.yaml ARCH=tf1
```

**Note:** This is a lengthy process (30+ minutes on a fast machine).

After completion, source the environment (required in every new terminal):

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

### Building the P4 Program

After sourcing the environment:

**Tofino 2 (default):**

```bash
make build
```

**Tofino 1:**

```bash
make build ARCH=tf1
```

Build output is placed in `build/t2na_load_balancer/` (or `build/tna_load_balancer/` for tf1).

### Running the Model

#### 1. Run the Tofino Model

The model simulates the Tofino hardware. In the first terminal:

**Tofino 2 (default):**

```bash
make model
```

**Tofino 1:**

```bash
make model ARCH=tf1
```

#### 2. Run the Switch Daemon

The switch daemon (`switchd`) connects to the model and loads the P4 program. In a second terminal (with environment sourced):

**Tofino 2 (default):**

```bash
make switch
```

**Tofino 1:**

```bash
make switch ARCH=tf1
```

#### 3. Start the Controller

In a third terminal (with environment sourced):

```bash
make controller
```

### Running Tests (Model)

#### Dataplane Tests

Runs on the Tofino model. Requires model and switch running (steps 1-2 above), controller NOT running:

**Tofino 2 (default):**

```bash
make test-dataplane
```

**Tofino 1:**

```bash
make test-dataplane ARCH=tf1
```

Tests include:

- L3 forwarding (`forward` table)
- Load balancing via ActionSelector
- Bidirectional flows with SNAT
- Dynamic member updates

#### Controller Tests

Runs on the Tofino model. Requires model, switch, and controller running (steps 1-3 above):

```bash
make test-controller
```

**Note:** Some endpoints tested are disabled in `controller/controller.py`. Enable them or adapt the tests as needed.

### Clean Targets

```bash
# Clean P4 build output
make clean-build

# Clean SDE build (requires rebuild with build-profile)
make clean-sde

# Rebuild SDE from scratch
make rebuild-sde
```

## Hardware Setup

This section describes how to set up the build environment for **real Tofino hardware**. The setup combines the open-source [open-p4studio](https://github.com/p4lang/open-p4studio) with proprietary Intel SDE components.

### Required Files from Intel

You need two files from Intel, available to authorized users via the [Intel Resource & Design Center (RDC)](https://www.intel.com/content/www/us/en/design/resource-design-center.html):

| File    | Description                             | Example                       |
| ------- | --------------------------------------- | ----------------------------- |
| **SDE** | Intel Barefoot SDE archive              | `bf-sde-9.13.4.tgz`           |
| **BSP** | Board Support Package for your hardware | `bf-reference-bsp-9.13.4.tgz` |

Extract the SDE archive to a directory (e.g., `/home/user/bf-sde-9.13.4`). The BSP file should remain as a `.tgz` archive.

### Environment Variables

The Makefile uses the following variables:

| Variable  | Required | Description                                                          | Example                                  |
| --------- | -------- | -------------------------------------------------------------------- | ---------------------------------------- |
| `SDE`     | Yes      | Path to extracted Intel SDE directory                                | `/home/user/bf-sde-9.13.4`               |
| `BSP`     | Yes      | Path to BSP `.tgz` file                                              | `/home/user/bf-reference-bsp-9.13.4.tgz` |
| `ARCH`    | No       | Tofino architecture: `tf1` or `tf2` (default: `tf2`)                 | `tf2`                                    |
| `PROFILE` | No       | Path to p4studio profile (default: `profiles/tofino2-hardware.yaml`) | `profiles/tofino2-hardware.yaml`         |

### Available Profiles

| Profile                          | Architecture | Use Case                |
| -------------------------------- | ------------ | ----------------------- |
| `profiles/tofino2-hardware.yaml` | Tofino 2     | Real Tofino 2 hardware  |
| `profiles/tofino2-model.yaml`    | Tofino 2     | Tofino 2 software model |
| `profiles/tofino-hardware.yaml`  | Tofino 1     | Real Tofino 1 hardware  |
| `profiles/tofino-model.yaml`     | Tofino 1     | Tofino 1 software model |

### Quick Setup (All-in-One)

If you have all prerequisites ready, you can run most of the setup with one make target:

**Tofino 2 (default):**

```bash
git clone https://github.com/stano45/p4containerflow-tofino2
cd p4containerflow-tofino2
make setup-hw SDE=/path/to/bf-sde-9.13.4 BSP=/path/to/bf-reference-bsp-9.13.4.tgz
```

**Tofino 1:**

```bash
git clone https://github.com/stano45/p4containerflow-tofino2
cd p4containerflow-tofino2
make setup-hw SDE=/path/to/bf-sde-9.13.4 BSP=/path/to/bf-reference-bsp-9.13.4.tgz \
    ARCH=tf1 PROFILE=profiles/tofino-hardware.yaml
```

This runs all setup steps in sequence:

1. Initializes the open-p4studio submodule
2. Extracts SDE packages
3. Sets up RDC (proprietary driver files)
4. Configures the profile with BSP path
5. Extracts BSP to pkgsrc/bf-platforms
6. Builds open-p4studio with the profile
7. Generates the environment script

**Note:** This is a lengthy process (30+ minutes on a fast machine, 1+ hour on the Tofino switch itself).

After completion:

```bash
# Source the environment (required in every new terminal)
source ~/setup-open-p4studio.bash

# Build the P4 program
make build

# Run on hardware
make switch
```

For a detailed breakdown of what each build step does under the hood, see the [Hardware Setup](docs/technical_report.md#hardware-setup) section of the technical report.

### Building the P4 Program

After sourcing the environment:

**Tofino 2 (default):**

```bash
make build
```

**Tofino 1:**

```bash
make build ARCH=tf1
```

Build output is placed in `build/t2na_load_balancer/` (or `build/tna_load_balancer/` for tf1).

### Running on Hardware

#### 1. Load Kernel Modules

For hardware operation, kernel modules must be loaded:

```bash
make load-kmods
```

Or use the helper script:

```bash
./scripts/load_kernel_modules.sh
```

#### 2. Run the Switch

**Tofino 2 (default):**

```bash
make switch
```

**Tofino 1:**

```bash
make switch ARCH=tf1
```

#### 3. Start the Controller

In a separate terminal (with environment sourced):

```bash
make controller
```

### Running Tests (Hardware)

#### Dataplane Tests

Tests the switch running on real hardware. Requires:

- Switch running (`make switch` in another terminal)
- Controller NOT running

**Tofino 2 (default):**

```bash
make test-hardware
```

**Tofino 1:**

```bash
make test-hardware ARCH=tf1
```

#### Controller Tests

Tests the controller's HTTP API. Requires:

- Switch running (`make switch` in another terminal)
- Controller running (`make controller` in another terminal)

```bash
make test-hardware-controller
```

These tests verify:
- Controller HTTP API health and reachability
- Node migration endpoint (valid and invalid requests)
- Cleanup endpoint (functionality and idempotency)
- Error handling and edge cases
- Response times

**Note:** Tests may modify controller state. Restart the controller afterwards to restore the configuration.

### Clean Targets

```bash
# Clean P4 build output
make clean-build

# Clean SDE build (requires rebuild with build-profile)
make clean-sde

# Rebuild SDE from scratch
make rebuild-sde
```

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
make load-kmods
# or
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
