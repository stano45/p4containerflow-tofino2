# Testing Guide

This directory contains test scripts for the P4 load balancer on Tofino hardware and model.

## Test Files

### 1. `test_model_dataplane.py`
PTF-based dataplane tests for the Tofino model.

**Environment:** Tofino model (simulation)  
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

### 2. `test_model_controller.py`
PTF-based controller integration tests for the Tofino model.

**Environment:** Tofino model (simulation)  
**Requirements:**
- Model running (`make model`)
- Switch daemon running (`make switch`)
- Controller running (`make controller`)

**Run:**
```bash
make test-controller
```

**Tests:**
- Controller API endpoints
- Traffic distribution with packet generation
- Node migration with traffic verification

**Note:** Some endpoints tested may be disabled in `controller/controller.py`.

### 3. `test_hardware_dataplane.py`
Low-level hardware tests using bfrt_grpc directly.

**Environment:** Real Tofino hardware  
**Requirements:**
- Switch running (`make switch`)
- Controller NOT running (test takes ownership)

**Run:**
```bash
make test-hardware ARCH=tf1  # or tf2
```

**Tests:**
1. gRPC connection test
2. Table access test (verify tables exist)
3. Table write/read test
4. Load balancer setup test (full configuration)
5. Cleanup test (remove all entries)

**Note:** This test uses bfrt_grpc to directly manipulate tables, similar to what the controller does.

### 4. `test_hardware_controller.py`
Pytest-based HTTP API tests for the controller (works on both model and hardware).

**Environment:** Any (tests controller HTTP API only)  
**Requirements:**
- Switch running (`make switch`)
- Controller running (`make controller`)
- uv installed

**Run:**
```bash
# Run all controller API tests
make test-hardware-controller

# Or run directly with pytest options
cd test && uv run pytest test_hardware_controller.py -v

# Run specific test class
cd test && uv run pytest test_hardware_controller.py -v -k "TestMigrateNodeValid"

# Run with custom controller URL
cd test && uv run pytest test_hardware_controller.py -v --controller-url http://10.0.0.1:5000
```

**Test Classes:**
- `TestControllerHealth` - Reachability and basic endpoint checks
- `TestMigrateNodeValid` - Valid migration requests
- `TestMigrateNodeInvalid` - Invalid requests and edge cases
- `TestInvalidEndpoints` - 404 handling
- `TestResponseTimes` - Performance verification
- `TestCleanup` - Cleanup functionality (runs last)

**Key features:**
- Uses pytest with uv for dependency management
- Tests controller HTTP API only (no gRPC/switch connection)
- No SDE environment required
- Comprehensive edge case and error handling tests

**Important:** Cleanup tests clear controller state. Restart the controller afterwards:
```bash
make controller
```

## Test Matrix

| Test File | Environment | Controller Required | Packet Testing | Purpose |
|-----------|-------------|---------------------|----------------|---------|
| `test_model_dataplane.py` | Model | ❌ No | ✅ PTF | Dataplane functionality |
| `test_model_controller.py` | Model | ✅ Yes | ✅ PTF | Controller + traffic |
| `test_hardware_dataplane.py` | Hardware | ❌ No | ❌ No | Low-level table ops |
| `test_hardware_controller.py` | Any | ✅ Yes | ❌ No | HTTP API tests |

## Typical Test Workflow

### Development (Model)
1. Build: `make build ARCH=tf2`
2. Start model: `make model ARCH=tf2` (terminal 1)
3. Start switch: `make switch ARCH=tf2` (terminal 2)
4. Run dataplane tests: `make test-dataplane ARCH=tf2`
5. Start controller: `make controller` (terminal 3)
6. Run controller tests: `make test-controller`

### Deployment (Hardware)
1. Build: `make build ARCH=tf1`
2. Load kernel modules: `make load-kmods`
3. Start switch: `make switch ARCH=tf1` (terminal 1)
4. Run hardware tests: `make test-hardware ARCH=tf1`
5. Start controller: `make controller` (terminal 2)
6. Run controller API tests: `make test-hardware-controller`

## Troubleshooting

### ImportError: bfrt_grpc module not found
Source the SDE environment:
```bash
source ~/setup-open-p4studio.bash
```

### ImportError: requests module not found
Install the requests library:
```bash
pip install requests
```

### Controller already owns program
Stop the controller before running tests that require exclusive access (`test_hardware_dataplane.py`, dataplane tests).

### Connection refused to controller
Make sure the controller is running on port 5000:
```bash
make controller
```

### Test hangs waiting for packets
- For model tests: ensure model and switch daemon are running
- For hardware tests: verify kernel modules are loaded (`make load-kmods`)
