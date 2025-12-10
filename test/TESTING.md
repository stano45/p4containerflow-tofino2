# Testing Guide

This directory contains test scripts for the P4 load balancer on Tofino hardware and model.

## Test Files

### 1. `t2na_load_balancer_dataplane.py`
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

### 2. `t2na_load_balancer_controller.py`
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

### 3. `hardware_test.py`
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

### 4. `hardware_controller_test.py` ⭐ NEW
Controller integration tests for real Tofino hardware.

**Environment:** Real Tofino hardware  
**Requirements:**
- Switch running (`make switch`)
- Controller running (`make controller`)

**Run:**
```bash
make test-hardware-controller ARCH=tf1  # or tf2
```

**Tests:**
1. Controller health check (HTTP API)
2. Initial configuration verification (table entries)
3. Node migration API test
4. Table state consistency verification
5. Cleanup API test

**Key differences from `hardware_test.py`:**
- Tests controller API endpoints instead of direct bfrt_grpc manipulation
- Requires controller to be running
- No packet generation (real hardware)
- Verifies controller behavior through table state inspection

**Important:** The cleanup test clears all table entries. Restart the controller afterwards:
```bash
make controller
```

## Test Matrix

| Test File | Environment | Controller Required | Packet Testing | Purpose |
|-----------|-------------|---------------------|----------------|---------|
| `t2na_load_balancer_dataplane.py` | Model | ❌ No | ✅ PTF | Dataplane functionality |
| `t2na_load_balancer_controller.py` | Model | ✅ Yes | ✅ PTF | Controller + traffic |
| `hardware_test.py` | Hardware | ❌ No | ❌ No | Low-level table ops |
| `hardware_controller_test.py` | Hardware | ✅ Yes | ❌ No | Controller API + state |

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
6. Run controller tests: `make test-hardware-controller ARCH=tf1`

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
Stop the controller before running tests that require exclusive access (`hardware_test.py`, dataplane tests).

### Connection refused to controller
Make sure the controller is running on port 5000:
```bash
make controller
```

### Test hangs waiting for packets
- For model tests: ensure model and switch daemon are running
- For hardware tests: verify kernel modules are loaded (`make load-kmods`)
