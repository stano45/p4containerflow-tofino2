# =============================================================================
# p4containerflow-tofino2 Makefile
# =============================================================================
#
# This Makefile automates building and running P4 programs on Tofino hardware
# using the open-p4studio (open-source Intel P4 Studio SDE).
#
# REQUIRED VARIABLES (for hardware setup):
#   SDE      Path to Intel bf-sde-X.Y.Z directory (proprietary, download from Intel)
#   BSP      Path to BSP .tgz file (e.g., bf-reference-bsp-9.13.4.tgz)
#
# OPTIONAL VARIABLES:
#   ARCH     Tofino architecture: tf1 (Tofino 1) or tf2 (Tofino 2) [default: tf2]
#   PROFILE  Path to p4studio profile YAML [default: profiles/tofino2-hardware.yaml]
#
# QUICK START (hardware):
#   make setup-hw SDE=/path/to/bf-sde-9.13.4 BSP=/path/to/bf-reference-bsp-9.13.4.tgz
#   source ~/setup-open-p4studio.bash
#   make build
#   make switch
#
# =============================================================================

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------

SDE ?=
BSP ?=
ARCH ?= tf2
PROFILE ?= profiles/tofino2-hardware.yaml

# Validate ARCH is tf1 or tf2
ifneq ($(filter $(ARCH),tf1 tf2),$(ARCH))
$(error ARCH must be 'tf1' or 'tf2', got '$(ARCH)')
endif

# -----------------------------------------------------------------------------
# Submodule and Environment Setup
# -----------------------------------------------------------------------------

init-submodule:
	@echo "=== Initializing open-p4studio submodule ==="
	git submodule update --init --recursive
	@echo "Done."

check-python:
	@echo "=== Checking Python version ==="
	@python3 -c "import sys; v = sys.version_info; exit(0 if v.major == 3 and v.minor <= 11 else 1)" || \
		(echo "ERROR: Python 3.11 or earlier is required (found $$(python3 --version))"; \
		 echo "Python 3.12+ removed distutils which is required by open-p4studio."; \
		 exit 1)
	@echo "Python version OK: $$(python3 --version)"

setup-env:
	@echo "=== Creating environment setup script ==="
	cd open-p4studio && ./create-setup-script.sh > ~/setup-open-p4studio.bash
	@echo ""
	@echo "Setup script created at: ~/setup-open-p4studio.bash"
	@echo ""
	@echo "To activate the environment, run:"
	@echo "  source ~/setup-open-p4studio.bash"
	@echo ""
	@echo "To make it permanent, add to your shell profile:"
	@echo "  echo 'source ~/setup-open-p4studio.bash' >> ~/.bashrc"

# -----------------------------------------------------------------------------
# Hardware Setup Targets
# -----------------------------------------------------------------------------

extract-sde:
	@if [ -z "$(SDE)" ]; then \
		echo "ERROR: SDE is not set."; \
		echo ""; \
		echo "Usage: make extract-sde SDE=/path/to/bf-sde-X.Y.Z"; \
		echo ""; \
		echo "The SDE directory should contain extract_all.sh and the Intel"; \
		echo "proprietary packages. Download from Intel Resource & Design Center."; \
		exit 1; \
	fi
	@if [ ! -d "$(SDE)" ]; then \
		echo "ERROR: SDE directory does not exist: $(SDE)"; \
		exit 1; \
	fi
	@if [ ! -f "$(SDE)/extract_all.sh" ]; then \
		echo "ERROR: extract_all.sh not found in $(SDE)"; \
		echo "Make sure SDE points to the bf-sde-X.Y.Z directory."; \
		exit 1; \
	fi
	@echo "=== Extracting SDE packages ==="
	cd "$(SDE)" && ./extract_all.sh
	@echo "Done."

setup-rdc:
	@if [ -z "$(SDE)" ]; then \
		echo "ERROR: SDE is not set."; \
		echo ""; \
		echo "Usage: make setup-rdc SDE=/path/to/bf-sde-X.Y.Z"; \
		exit 1; \
	fi
	@if [ ! -d "$(SDE)" ]; then \
		echo "ERROR: SDE directory does not exist: $(SDE)"; \
		exit 1; \
	fi
	@if [ ! -f "open-p4studio/hw/rdc_setup.sh" ]; then \
		echo "ERROR: open-p4studio/hw/rdc_setup.sh not found."; \
		echo "Run 'make init-submodule' first."; \
		exit 1; \
	fi
	@echo "=== Setting up RDC (proprietary driver files) ==="
	@# Extract version from SDE path (e.g., bf-sde-9.13.4 -> 9.13.4)
	@VERSION=$$(basename "$(SDE)" | sed 's/bf-sde-//'); \
	RDC_BFD="$(SDE)/bf-drivers-$$VERSION"; \
	OS_BFD="$$PWD/open-p4studio/pkgsrc/bf-drivers"; \
	echo "SDE Version: $$VERSION"; \
	echo "RDC_BFD: $$RDC_BFD"; \
	echo "OS_BFD: $$OS_BFD"; \
	if [ ! -d "$$RDC_BFD" ]; then \
		echo "ERROR: bf-drivers directory not found: $$RDC_BFD"; \
		echo "Run 'make extract-sde' first to extract SDE packages."; \
		exit 1; \
	fi; \
	echo ""; \
	echo "Updating rdc_setup.sh with paths..."; \
	sed -i "s|^RDC_BFD=.*|RDC_BFD=\"$$RDC_BFD\"|" open-p4studio/hw/rdc_setup.sh; \
	sed -i "s|^OS_BFD=.*|OS_BFD=\"$$OS_BFD\"|" open-p4studio/hw/rdc_setup.sh; \
	echo "Running rdc_setup to copy proprietary files..."; \
	echo ""; \
	cd open-p4studio/hw && bash -c "source rdc_setup.sh && rdc_setup"
	@echo ""
	@echo "Done."

link-p4studio:
	@echo "=== Creating symlink in open-p4studio ==="
	@if [ ! -d "open-p4studio/pkgsrc/p4-examples" ]; then \
		echo "ERROR: open-p4studio/pkgsrc/p4-examples not found."; \
		echo "Run 'make init-submodule' first."; \
		exit 1; \
	fi
	@PKGSRCDIR="$$PWD/open-p4studio/pkgsrc/p4-examples/p4_16_programs"; \
	mkdir -p "$$PKGSRCDIR"; \
	ln -sfn "$$PWD" "$$PKGSRCDIR/t2na_load_balancer"; \
	echo "Symlink created: $$PKGSRCDIR/t2na_load_balancer -> $$PWD"

config-profile:
	@if [ -z "$(BSP)" ]; then \
		echo "ERROR: BSP is not set."; \
		echo ""; \
		echo "Usage: make config-profile BSP=/path/to/bf-reference-bsp-X.Y.Z.tgz"; \
		echo ""; \
		echo "The BSP (Board Support Package) file is required for hardware builds."; \
		echo "Download from Intel Resource & Design Center."; \
		exit 1; \
	fi
	@if [ ! -f "$(BSP)" ]; then \
		echo "ERROR: BSP file does not exist: $(BSP)"; \
		exit 1; \
	fi
	@if [ ! -f "$(PROFILE)" ]; then \
		echo "ERROR: Profile file does not exist: $(PROFILE)"; \
		exit 1; \
	fi
	@echo "=== Configuring profile with BSP path ==="
	@echo "Profile: $(PROFILE)"
	@echo "BSP: $(BSP)"
	@sed -i "s|bsp-path:.*|bsp-path: $(BSP)|" "$(PROFILE)"
	@echo "Done. Profile updated with BSP path."

build-profile: check-python
	@if [ ! -f "$(PROFILE)" ]; then \
		echo "ERROR: Profile file does not exist: $(PROFILE)"; \
		exit 1; \
	fi
	@if [ ! -f "open-p4studio/p4studio/p4studio" ]; then \
		echo "ERROR: open-p4studio/p4studio/p4studio not found."; \
		echo "Run 'make init-submodule' first."; \
		exit 1; \
	fi
	@echo "=== Applying p4studio profile ==="
	@echo "Profile: $(PROFILE)"
	@PROFILE_PATH="$$PWD/$(PROFILE)"; \
	cd open-p4studio && ./p4studio/p4studio profile apply "$$PROFILE_PATH"
	@echo ""
	@echo "Done. Profile applied successfully."
	@echo ""
	@echo "IMPORTANT: Run 'make setup-env' and source the environment script"
	@echo "before running build or switch targets."

# -----------------------------------------------------------------------------
# Convenience Target: Full Hardware Setup
# -----------------------------------------------------------------------------

setup-hw: init-submodule extract-sde setup-rdc link-p4studio config-profile build-profile setup-env
	@echo ""
	@echo "============================================================"
	@echo " Hardware Setup Complete!"
	@echo "============================================================"
	@echo ""
	@echo " Next steps:"
	@echo ""
	@echo "   1. Source the environment:"
	@echo "      source ~/setup-open-p4studio.bash"
	@echo ""
	@echo "   2. Build the P4 program:"
	@echo "      make build"
	@echo ""
	@echo "   3. Run on hardware:"
	@echo "      make switch ARCH=$(ARCH)"
	@echo ""
	@echo "============================================================"

# -----------------------------------------------------------------------------
# Build and Run Targets
# -----------------------------------------------------------------------------

build:
	@echo "=== Building t2na_load_balancer ==="
	@if [ ! -f "open-p4studio/p4studio" ]; then \
		echo "Using p4studio from PATH..."; \
		p4studio build t2na_load_balancer; \
	else \
		./open-p4studio/p4studio build t2na_load_balancer; \
	fi

model: build
	@echo "=== Running Tofino model (ARCH=$(ARCH)) ==="
	run_tofino_model.sh --arch $(ARCH) -p t2na_load_balancer

switch:
	@echo "=== Running switchd on hardware (ARCH=$(ARCH)) ==="
	run_switchd.sh --arch $(ARCH) -p t2na_load_balancer

# -----------------------------------------------------------------------------
# Test Targets
# -----------------------------------------------------------------------------

test-dataplane:
	@echo "=== Running dataplane tests (ARCH=$(ARCH)) ==="
	run_p4_tests.sh --arch $(ARCH) \
		-t ./test \
		-s t2na_load_balancer_dataplane \
		-p t2na_load_balancer

test-controller:
	@echo "=== Running controller tests (ARCH=$(ARCH)) ==="
	run_p4_tests.sh --arch $(ARCH) \
		-t ./test \
		-s t2na_load_balancer_controller \
		-p t2na_load_balancer

# -----------------------------------------------------------------------------
# Controller
# -----------------------------------------------------------------------------

controller:
	@echo "=== Starting controller ==="
	cd ./controller && ./run.sh

# -----------------------------------------------------------------------------
# Phony Targets
# -----------------------------------------------------------------------------

.PHONY: init-submodule check-python setup-env \
        extract-sde setup-rdc link-p4studio config-profile build-profile setup-hw \
        build model switch \
        test-dataplane test-controller \
        controller
