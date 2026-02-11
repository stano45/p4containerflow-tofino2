# Testing Guide

This directory contains test scripts for the P4 load balancer.

## Directory Structure

```
test/
├── model/                    # PTF-based tests (tofino-model)
│   ├── test_dataplane.py     # Dataplane functionality tests
│   └── test_controller.py    # Controller integration tests
├── hardware/                 # Pytest-based tests (real hardware)
│   ├── test_dataplane.py     # Low-level table operations
│   ├── test_controller.py    # HTTP API tests
│   ├── conftest.py           # Pytest fixtures
│   ├── pyproject.toml        # Dependencies
│   └── run.sh                # Test runner
└── TESTING.md
```

## Model Tests (PTF-based)

### `test/model/test_dataplane.py`
PTF-based dataplane tests for the Tofino model.

**Requirements:**
- Model running (`make model`)
- Switch daemon running (`make switch`)
- Controller NOT running

**Run:**
```bash
make test-dataplane ARCH=tf2  # or tf1
```

**Tests:**
- L3 forwarding
- Load balancing via ActionSelector
- Bidirectional flows with SNAT
- Dynamic member updates

### `test/model/test_controller.py`
PTF-based controller integration tests with packet generation.

**Requirements:**
- Model running (`make model`)
- Switch daemon running (`make switch`)
- Controller running (`make controller`)

**Run:**
```bash
make test-controller ARCH=tf2
```

**Tests:**
- Controller API endpoints
- Traffic distribution with packet generation
- Node migration with traffic verification

## Hardware Tests (Pytest-based)

### `test/hardware/test_dataplane.py`
Pytest-based low-level hardware tests using bfrt_grpc directly.

**Requirements:**
- Switch running (`make switch`)
- Controller NOT running (test takes ownership)
- SDE environment sourced (`source ~/setup-open-p4studio.bash`)

**Run:**
```bash
make test-hardware ARCH=tf1  # or tf2

# Or directly:
cd test/hardware && ./run.sh dataplane -v
cd test/hardware && ./run.sh dataplane -k "TestTableAccess"
```

**Test Classes:**
- `TestConnection` - gRPC connection and program binding
- `TestTableAccess` - Verify P4 tables exist and are accessible
- `TestTableWriteRead` - Table entry write/read/delete operations
- `TestLoadBalancerSetup` - Full load balancer configuration
- `TestPortTableAccess` - Verify $PORT BF-RT table is accessible
- `TestPortConfiguration` - Port add/read/modify/delete via $PORT table
- `TestCleanup` - Remove all table entries

### `test/hardware/test_controller.py`
Pytest-based HTTP API tests for the controller.

**Requirements:**
- Switch running (`make switch`)
- Controller running (`make controller`)

**Run:**
```bash
make test-hardware-controller

# Or directly:
cd test/hardware && ./run.sh controller -v
cd test/hardware && ./run.sh controller -k "TestMigrateNodeValid"
```

**Test Classes:**
- `TestControllerHealth` - Reachability and basic endpoint checks
- `TestPortSetupConfig` - Validate port_setup config structure
- `TestMigrateNodeValid` - Valid migration requests (auto-reinitializes)
- `TestMigrateNodeInvalid` - Invalid requests and edge cases
- `TestInvalidEndpoints` - 404 handling
- `TestResponseTimes` - Performance verification (auto-reinitializes)
- `TestCleanupAndReinitialize` - Cleanup, reinitialize, and state verification

## Test Matrix

| Test | Location | Environment | Controller | Packets |
|------|----------|-------------|------------|---------|
| Model dataplane | `test/model/test_dataplane.py` | Model | ❌ | ✅ PTF |
| Model controller | `test/model/test_controller.py` | Model | ✅ | ✅ PTF |
| Hardware dataplane | `test/hardware/test_dataplane.py` | Hardware | ❌ | ❌ |
| Hardware controller | `test/hardware/test_controller.py` | Any | ✅ | ❌ |

## Idempotency

All test suites are idempotent -- they can be run repeatedly without restarting
the controller or switch. This is achieved via the `/reinitialize` endpoint,
which restores the controller to its initial state (cleanup + re-insert all
table entries from the original config).

- **Hardware controller tests** call `/reinitialize` at session start and
  before each migration test via an `autouse` fixture.
- **Model controller tests** call `/reinitialize` in `setUp()` and `tearDown()`.
- **Hardware dataplane tests** release the gRPC connection on teardown so the
  controller can bind afterward.

You can safely run `make test-hardware && make test-hardware-controller` or
repeat any test suite multiple times.

## Typical Workflow

### Development (Model)
```bash
make model ARCH=tf2      # Terminal 1
make switch ARCH=tf2     # Terminal 2
make test-dataplane ARCH=tf2

make controller          # Terminal 3
make test-controller ARCH=tf2
```

### Deployment (Hardware)
```bash
source ~/setup-open-p4studio.bash
make switch ARCH=tf1     # Terminal 1
make test-hardware ARCH=tf1

make controller          # Terminal 2
make test-hardware-controller
```

## Troubleshooting

### ImportError: bfrt_grpc module not found
```bash
source ~/setup-open-p4studio.bash
```

### Controller already owns program
Stop the controller before running `test-hardware`.

### Connection refused to controller
```bash
make controller
```
