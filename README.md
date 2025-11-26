# p4containerflow-tofino2

P4-based L3/TCP load balancer for Intel Tofino 2 (T2NA). The repo contains:

- A P4_16 program targeting T2NA that performs L3 forwarding, connection-consistent load balancing via an ActionSelector, and optional SNAT for server-to-client traffic on a service port.
- A lightweight Python control plane (bfrt_grpc) that programs the switch tables and exposes a tiny HTTP API for live node migration.
- PTF-based tests for dataplane behavior and example controller workflows.

- [p4containerflow-tofino2](#p4containerflow-tofino2)
  - [Repository structure](#repository-structure)
  - [Development setup](#development-setup)
  - [Rewriting a p4 program from v1model to t2na](#rewriting-a-p4-program-from-v1model-to-t2na)
  - [Writing a control plane](#writing-a-control-plane)
  - [Build and run](#build-and-run)
  - [Testing](#testing)
  - [Troubleshooting](#troubleshooting)
    - [run_switchd](#run_switchd)
    - [bfshell](#bfshell)

## Repository structure

- `load_balancer/`
  - `t2na_load_balancer.p4`: P4 program for T2NA. Uses an ActionSelector to spread flows across server members and supports optional SNAT for server→client traffic.
  - `t2na_load_balancer_custom.py`: PTF tests for variants and scenarios (even distribution, bidirectional, dynamic changes).
- `controller/`
  - `controller.py`: Flask app + gRPC control-plane wiring. Reads `controller_config.json`, programs tables via `bf_switch_controller.py`, and exposes `/migrateNode`.
  - `bf_switch_controller.py`: bfrt_grpc-based helper for table writes (selector groups, action members, SNAT, L3 forwarding).
  - `node_manager.py`: Orchestrates initial table population and runtime node migration.
  - `internal_types.py`, `utils.py`, `p4runtime_lib/`: Types and helper utilities.
  - `requirements.txt`: Python deps for the controller.
  - `run.sh`: Convenience launcher for SDE environments (sets PYTHONPATH to SDE libs).
- `test/`
  - `t2na_load_balancer_dataplane.py`: End-to-end PTF tests for dataplane behavior (ECMP-like selection, SNAT, forwarding, dynamic updates via table mod).
  - `t2na_load_balancer_controller.py`: Controller-driven test that calls HTTP endpoints; note some endpoints are currently disabled in `controller.py` (see Testing section).
- `scripts/`
  - `load_kernel_modules.sh`: Helper to load bf kernel modules on the switch.
  - `run_p4testgen.sh`: Example p4testgen invocation for generating PTF tests.
- `profiles/`
  - `tofino2-hardware.yaml`: Reference P4Studio profile used to build with SDE 9.13.4 targeting Tofino 2 hardware.
- `diagrams/`: Diagram scripts (requires the `diagrams` Python package if you want to render).
- `Makefile`: Convenience targets that assume an SDE-style directory layout (see Build and run).

## Development setup

### Prerequisites

The Tofino 2 switch (for simplicity, we will refer to it as "the switch" from this point on) is running Ubuntu 22.04.04 LTS. This OS was already installed when we started using the switch, so we will not describe this process here. Refer to the Intel Tofino documentation for instructions.

The switch and the development machine are both connected to the same network, therefore we can simply use SSH to connect to the switch.

**Python Requirements**: The open-p4studio build system requires Python 3.11 or earlier (Python 3.12+ removed the `distutils` module). On Ubuntu 22.04, Python 3.10 is the default and works fine.

### Installing open-p4studio

This project now uses [open-p4studio](https://github.com/p4lang/open-p4studio), the open-source release of Intel P4 Studio SDE. **This is a required dependency and must be installed before running any other commands.**

#### Quick Setup

Clone this repository and run the automated setup:

```bash
git clone <your-repo-url>
cd p4containerflow-tofino2
make setup
```

This will:

1. Initialize the `open-p4studio` submodule
2. Install the P4 Studio SDE for Tofino 2 and 2M only (using the custom profile `p4studio-tofino2.yaml`)
3. Create a setup script at `~/setup-open-p4studio.bash`

After the setup completes, source the environment script:

```bash
source ~/setup-open-p4studio.bash
```

**Important**: You need to source this script in every new terminal session where you want to use the SDE tools. Consider adding it to your `~/.bashrc` or `~/.zshrc`:

```bash
echo "source ~/setup-open-p4studio.bash" >> ~/.zshrc
```

#### Manual Setup Steps

If you prefer manual control, you can run the individual steps:

```bash
# 1. Initialize submodule
make init-submodule

# 2. Install P4 Studio with testing profile
make install-p4studio

# 3. Create setup environment script
make setup-env
```

### Hardware-Specific Setup

For running on actual Tofino 2 hardware (not the model), you will need additional components from Intel:

- **BSP (Board Support Package)**: Contact Intel (intel.tofino.contact@intel.com) to obtain the BSP for your hardware
- **ASIC-specific Serdes drivers**: Available from Intel RDC (Resource & Documentation Center) for authorized users
- **Kernel modules**: The required kernel modules may not load automatically. Use the provided [script](scripts/load_kernel_modules.sh) and consider adding it as a startup service.

For reference, our original hardware setup used SDE version `9.13.4`. The configuration file for that build is available in [p4studio-profile.yaml](scripts/p4studio-profile.yaml).

### Customizing the Installation

The `make setup` command uses a custom profile that installs only Tofino 2 and 2M support (see `p4studio-tofino2.yaml`). If you need a different configuration (e.g., hardware-only, additional Tofino versions, or additional features), you can:

1. Use the interactive installer:

```bash
cd open-p4studio
./install.sh
```

2. Or create/use a custom profile:

```bash
cd open-p4studio
./p4studio/p4studio profile create my-profile.yaml
# Edit my-profile.yaml as needed
./p4studio/p4studio profile apply my-profile.yaml
```

Refer to the [open-p4studio README](open-p4studio/README.md) for detailed customization options.

## Rewriting a p4 program from v1model to t2na

The load balancer program we are looking to deploy was written for the V1Model architecture. The Tofino 2 switch runs the Tofino 2 Native Architecture (T2NA), which has notable differences to V1Model, mainly the pipeline setup and available externs.

In order to rewrite the program, we did the following:

1. Adjusted the pipeline to T2NA ordering: (IngressParser -> Ingress -> IngressDeparser -> EgressParser -> Egress -> EgressDeparser).
2. Adjusted parsers to properly parse tofino-specific metadata.
3. Updated the hashing method to the T2NA version.
4. Rewrote some multi-step calculations to ensure the program compiles.
5. Modeled load balancing using an ActionSelector and per-member action that rewrites the IPv4 destination; a separate forwarding table selects the egress port.

## Writing a control plane

The control plane in `controller/` uses the Intel bfrt_grpc Python client to program the Tofino pipeline and exposes a tiny HTTP API.

- Entry point: `controller/controller.py` (Flask app)
- Configuration: `controller/controller_config.json`
  - `addr`: bfrt_grpc address of the switch (e.g., `0.0.0.0:50052`)
  - `name`: P4 program name loaded on the device (must match the pipeline config), e.g., `t2na_load_balancer`
  - `load_balancer_ip`: virtual IP clients connect to (matched by the `node_selector` table)
  - `service_port`: TCP port for SNAT on server→client traffic
  - `nodes`: initial nodes with `ipv4`, `sw_port`, and optional `is_lb_node` indicating candidates for load balancing
- Tables programmed (see `bf_switch_controller.py`):
  - `SwitchIngress.client_snat`: SNAT server→client traffic on the given TCP service port to the load balancer IP.
  - `SwitchIngress.action_selector_ap` and `SwitchIngress.action_selector`: members and group for ActionSelector-based load balancing.
  - `SwitchIngress.node_selector`: maps `load_balancer_ip` to a selector group.
  - `SwitchIngress.forward`: L3 forwarding from IPv4 dst to egress port.

HTTP API

- `POST /migrateNode`
  - Body: `{ "old_ipv4": "10.0.0.2", "new_ipv4": "10.0.0.4" }`
  - Effect: updates the action member to point to `new_ipv4` without changing group membership (connection-aware migration).
  - Example: `controller/cr.sh`.

Notes

- Additional endpoints referenced in `test/t2na_load_balancer_controller.py` (`/add_node`, `/update_node`) are commented out in `controller/controller.py`. That test will not pass unless you re-enable or adapt those endpoints.
- The launcher `controller/run.sh` expects a working SDE installation and sets PYTHONPATH to include bfrt_grpc and related Python packages.

## Build and run

There are two common ways to use this repo with Intel SDE:

1. Build and run under your SDE workspace

- Copy or symlink the program folder into your SDE examples path (e.g., `.../pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer`).
- Use your SDE's `p4studio` with a profile similar to `profiles/tofino2-hardware.yaml` (Tofino2, hardware target) to build the program.
- Start the program on hardware:
  - `./run_switchd.sh --arch tf2 -p t2na_load_balancer`
  - Optionally start `./run_bfshell.sh` in another terminal.
- The provided `Makefile` assumes this SDE directory layout and wrappers (e.g., `run_switchd.sh`, `run_p4_tests.sh`) are available on PATH. Targets:
  - `make build` (calls `./p4studio/p4studio build t2na_load_balancer`)
  - `make switch` (starts switchd with `--arch tf2 -p t2na_load_balancer`)
  - `make test-dataplane`, `make test-controller` (invoke `run_p4_tests.sh` with paths under SDE pkgsrc). These paths assume the SDE example layout.
  - `make link-p4studio SDE=/path/to/sde` (creates a symlink to this project in the SDE examples directory)
  - `make build-profile SDE=/path/to/sde PROFILE=profiles/tofino2-hardware.yaml` (applies a P4Studio profile; `PROFILE` defaults to `profiles/tofino2-hardware.yaml`)

2. Use only the control plane from this repo

- Ensure switchd is running with the compiled `t2na_load_balancer` pipeline bound.
- Install Python deps for the controller:
  - `pip install -r controller/requirements.txt`
- Launch the controller (SDE-specific env expected):
  - `cd controller && ./run.sh`
- Adjust `controller/controller_config.json` to match your device and topology. Then use `cr.sh` or `curl` to call `/migrateNode`.

## Testing

Dataplane tests (PTF)

- `test/t2na_load_balancer_dataplane.py` contains tests for:
  - L3 forwarding (`forward` table)
  - Load balancing via ActionSelector (even distribution across members)
  - Bidirectional flows with SNAT (server→client)
  - Dynamic updates (modifying selector members while preserving flow affinity)
- Run them via SDE’s `run_p4_tests.sh`, for example: `./run_p4_tests.sh --arch=tf2 --target=hw -p t2na_load_balancer -t <path-to-tests>`.
- On real hardware, packet generation is via PTF and may rely on virtual or test interfaces; many stock example tests in SDE aren’t meant for direct hardware ports. The tests here try to use `get_sw_ports()` to map ports, but availability depends on your environment.

Controller test

- `test/t2na_load_balancer_controller.py` exercises HTTP endpoints to adjust membership over time. Some endpoints in the test are currently disabled in the controller (see notes above), so expect failures unless you enable those routes or adapt the test.

## Troubleshooting

### Setup and Installation Issues

#### Python version too new (3.12+)

The build requires Python 3.11 or earlier. Use your system's package manager to ensure Python 3.10 or 3.11 is installed and set as the default `python3`.

### run_switchd

- If there is an error connecting to the device e.g. something like `/dev/fpga0` not found, you might have to load the required kernel modules using [load_kernel_modules.sh](scripts/load_kernel_modules.sh).

### bfshell

- If you cannot access the program nodes in `bfrt_python`, just do `CTRL + C` and re-run it.
- If `bfrt_python` doesn’t show your program, the pipeline may not be bound or the P4 name in `controller_config.json` doesn’t match the loaded pipeline.

Additional notes

- Dockerfile under `controller/` is currently out of date for this layout (it refers to a `switch_controller.py` filename that does not exist; use `bf_switch_controller.py`). Prefer running the controller directly on a host with SDE libs available.
- The P4 program currently assumes IPv4 and TCP only and does not perform L2 MAC rewrites. Ensure your environment handles L2 appropriately or extend the program with MAC rewrite where needed.
