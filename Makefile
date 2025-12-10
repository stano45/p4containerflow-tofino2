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

PROGRAM_NAME_tf1 = tna_load_balancer
PROGRAM_NAME_tf2 = t2na_load_balancer
CHIP_FAMILY_tf1 = tofino
CHIP_FAMILY_tf2 = tofino2
P4_TARGET_tf1 = tofino
P4_TARGET_tf2 = tofino2
P4_ARCH_tf1 = tna
P4_ARCH_tf2 = t2na
BUILD_DIR_tf1 = build/$(PROGRAM_NAME_tf1)
BUILD_DIR_tf2 = build/$(PROGRAM_NAME_tf2)
CONF_FILE_tf1 = load_balancer/tna_load_balancer.conf
CONF_FILE_tf2 = load_balancer/t2na_load_balancer.conf

PROGRAM_NAME := $(PROGRAM_NAME_$(ARCH))
CHIP_FAMILY := $(CHIP_FAMILY_$(ARCH))
P4_TARGET := $(P4_TARGET_$(ARCH))
P4_ARCH := $(P4_ARCH_$(ARCH))
BUILD_DIR := $(BUILD_DIR_$(ARCH))
CONF_FILE := $(CONF_FILE_$(ARCH))

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

extract-bsp:
	@if [ -z "$(BSP)" ]; then \
		echo "ERROR: BSP is not set."; \
		echo ""; \
		echo "Usage: make extract-bsp BSP=/path/to/bf-reference-bsp-X.Y.Z"; \
		echo "       or BSP=/path/to/bf-reference-bsp-X.Y.Z.tgz"; \
		exit 1; \
	fi
	@echo "=== Extracting BSP to pkgsrc/bf-platforms ==="
	@mkdir -p open-p4studio/pkgsrc
	@if [ -f "$(BSP)" ]; then \
		echo "BSP is a tarball, extracting wrapper first..."; \
		TMPDIR=$$(mktemp -d) && \
		tar -xzf "$(BSP)" -C "$$TMPDIR" && \
		BSP_DIR=$$(find "$$TMPDIR" -name "bf-reference-bsp-*" -type d | head -1) && \
		if [ -z "$$BSP_DIR" ]; then \
			echo "ERROR: Could not find bf-reference-bsp directory in tarball"; \
			rm -rf "$$TMPDIR"; \
			exit 1; \
		fi && \
		echo "Extracting bf-platforms from $$BSP_DIR/packages..." && \
		PLATFORM_TGZ=$$(find "$$BSP_DIR/packages" -name "bf-platforms-*.tgz" | head -1) && \
		if [ -z "$$PLATFORM_TGZ" ]; then \
			echo "ERROR: Could not find bf-platforms tarball in $$BSP_DIR/packages"; \
			rm -rf "$$TMPDIR"; \
			exit 1; \
		fi && \
		tar -xzf "$$PLATFORM_TGZ" -C open-p4studio/pkgsrc && \
		EXTRACTED_DIR=$$(find open-p4studio/pkgsrc -maxdepth 1 -name "bf-platforms-*" -type d | head -1) && \
		if [ -n "$$EXTRACTED_DIR" ] && [ "$$EXTRACTED_DIR" != "open-p4studio/pkgsrc/bf-platforms" ]; then \
			echo "Renaming $$EXTRACTED_DIR to open-p4studio/pkgsrc/bf-platforms"; \
			rm -rf open-p4studio/pkgsrc/bf-platforms; \
			mv "$$EXTRACTED_DIR" open-p4studio/pkgsrc/bf-platforms; \
		fi && \
		rm -rf "$$TMPDIR"; \
	elif [ -d "$(BSP)" ]; then \
		echo "BSP is a directory, extracting from packages..."; \
		PLATFORM_TGZ=$$(find "$(BSP)/packages" -name "bf-platforms-*.tgz" | head -1) && \
		if [ -z "$$PLATFORM_TGZ" ]; then \
			echo "ERROR: Could not find bf-platforms tarball in $(BSP)/packages"; \
			exit 1; \
		fi && \
		tar -xzf "$$PLATFORM_TGZ" -C open-p4studio/pkgsrc && \
		EXTRACTED_DIR=$$(find open-p4studio/pkgsrc -maxdepth 1 -name "bf-platforms-*" -type d | head -1) && \
		if [ -n "$$EXTRACTED_DIR" ] && [ "$$EXTRACTED_DIR" != "open-p4studio/pkgsrc/bf-platforms" ]; then \
			echo "Renaming $$EXTRACTED_DIR to open-p4studio/pkgsrc/bf-platforms"; \
			rm -rf open-p4studio/pkgsrc/bf-platforms; \
			mv "$$EXTRACTED_DIR" open-p4studio/pkgsrc/bf-platforms; \
		fi; \
	else \
		echo "ERROR: BSP path does not exist: $(BSP)"; \
		exit 1; \
	fi
	@echo "Done. BSP extracted successfully."

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
	@if [ -n "$(BSP)" ]; then \
		echo "=== Updating profile with BSP path ==="; \
		$(MAKE) config-profile BSP="$(BSP)" PROFILE="$(PROFILE)"; \
		echo "=== Extracting BSP ==="; \
		$(MAKE) extract-bsp BSP="$(BSP)"; \
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
# Convenience Targets
# -----------------------------------------------------------------------------

setup-model: init-submodule build-profile setup-env
	@echo ""
	@echo "============================================================"
	@echo " Model Setup Complete!"
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
	@echo "   3. Run on model:"
	@echo "      make model ARCH=$(ARCH)"
	@echo ""
	@echo "============================================================"

setup-hw: init-submodule extract-sde setup-rdc config-profile extract-bsp build-profile setup-env
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

P4_PROGRAM = load_balancer/t2na_load_balancer.p4
P4C = $(SDE_INSTALL)/bin/p4c-barefoot
P4C_FLAGS = --target $(P4_TARGET) \
			--arch $(P4_ARCH) \
            --p4runtime-files $(BUILD_DIR)/p4info.txt \
            --bf-rt-schema $(BUILD_DIR)/bf-rt.json \
            -o $(BUILD_DIR)

BFAS = $(SDE_INSTALL)/bin/bfas

build:
	@echo "=== Building $(PROGRAM_NAME) (ARCH=$(ARCH)) ==="
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	@if [ ! -f "$(P4C)" ]; then \
		echo "ERROR: p4c-barefoot not found at $(P4C)"; \
		exit 1; \
	fi
	@if [ ! -d "load_balancer/common" ]; then \
		echo "Creating symlink to common P4 includes..."; \
		ln -sf ../open-p4studio/pkgsrc/p4-examples/p4_16_programs/common load_balancer/common; \
	fi
	@mkdir -p $(BUILD_DIR)
	@echo "Compiling $(P4_PROGRAM) for $(CHIP_FAMILY) ($(P4_ARCH))..."
	$(P4C) $(P4C_FLAGS) $(P4_PROGRAM)
	@echo "Running assembler to generate $(CHIP_FAMILY).bin..."
	@BFA_FILE=$$(find $(BUILD_DIR)/pipe -name "*.bfa" | head -1); \
	if [ -n "$$BFA_FILE" ] && [ -f "$(BFAS)" ]; then \
		mkdir -p $(BUILD_DIR)/pipe/logs; \
		$(BFAS) $$BFA_FILE -o $(BUILD_DIR)/pipe; \
		mv $(BUILD_DIR)/pipe/tofino.bin $(BUILD_DIR)/pipe/$(CHIP_FAMILY).bin 2>/dev/null || true; \
		echo "Assembly complete: $(BUILD_DIR)/pipe/$(CHIP_FAMILY).bin"; \
	else \
		echo "WARNING: bfas not found or no .bfa file, skipping assembly"; \
	fi
	@echo "Build complete: $(BUILD_DIR)"

clean-build:
	@echo "=== Cleaning build directory ==="
	rm -rf build/

clean-sde:
	@echo "=== Cleaning SDE build directory ==="
	rm -rf open-p4studio/build/

rebuild-sde: clean-sde build-profile
	@echo "=== SDE rebuild complete ==="
	@echo "Don't forget to copy bfas:"
	@echo "  cp open-p4studio/build/pkgsrc/p4-compilers/p4c/bfas \$$SDE_INSTALL/bin/bfas"

install: build
	@echo "=== Installing $(PROGRAM_NAME) (ARCH=$(ARCH)) to SDE ==="
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	@INSTALL_DIR="$(SDE_INSTALL)/share/p4/targets/$(CHIP_FAMILY)/$(PROGRAM_NAME)"; \
	echo "Installing to $$INSTALL_DIR"; \
	sudo rm -rf "$$INSTALL_DIR"; \
	sudo mkdir -p "$$INSTALL_DIR"; \
	sudo cp -r $(BUILD_DIR)/* "$$INSTALL_DIR/"; \
	sudo cp $(CONF_FILE) "$(SDE_INSTALL)/share/p4/targets/$(CHIP_FAMILY)/$(PROGRAM_NAME).conf"; \
	echo "Installation complete"

model: install
	@echo "=== Running Tofino model (ARCH=$(ARCH)) ==="
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	@cd open-p4studio && sudo -E ./run_tofino_model.sh --arch $(ARCH) -p $(PROGRAM_NAME)

switch:
	@echo "=== Running switchd on hardware (ARCH=$(ARCH)) ==="
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	@cd open-p4studio && sudo -E ./run_switchd.sh --arch $(ARCH) -p $(PROGRAM_NAME)

load-kmods:
	@echo "=== Loading Barefoot kernel modules (manual helper) ==="
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	@BIN_DIR="$(SDE_INSTALL)/bin"; \
	INSTALL_DIR="$(SDE_INSTALL)"; \
	for MOD in bf_kdrv bf_kpkt bf_knet; do \
		LOAD_SCRIPT="$$BIN_DIR/$${MOD}_mod_load"; \
		if [ ! -x "$$LOAD_SCRIPT" ]; then \
			echo "WARNING: $$LOAD_SCRIPT not found, skipping."; \
			continue; \
		fi; \
		echo "Loading $$MOD using $$LOAD_SCRIPT $$INSTALL_DIR"; \
		sudo "$$LOAD_SCRIPT" "$$INSTALL_DIR" || true; \
	done

# -----------------------------------------------------------------------------
# Test Targets
# -----------------------------------------------------------------------------

test-dataplane: install
	@echo "=== Running dataplane tests ==="
	@cd open-p4studio && sudo -E ./run_p4_tests.sh --arch $(ARCH) \
		-t ../test \
		-s test_model_dataplane \
		-p $(PROGRAM_NAME)

test-controller: install
	@echo "=== Running controller tests ==="
	@cd open-p4studio && sudo -E ./run_p4_tests.sh --arch $(ARCH) \
		-t ../test \
		-s test_model_controller \
		-p $(PROGRAM_NAME)

# Hardware test - tests the switch running on real hardware
# Requires: switch running (make switch), controller NOT running
test-hardware:
	@echo "=== Running hardware dataplane tests (ARCH=$(ARCH)) ==="
	@echo "NOTE: Requires switch running, controller NOT running"
	@if [ -z "$(SDE_INSTALL)" ]; then \
		echo "ERROR: SDE_INSTALL not set. Source ~/setup-open-p4studio.bash first"; \
		exit 1; \
	fi
	cd test && uv run --system pytest test_hardware_dataplane.py -v --arch $(ARCH)

# Hardware controller test - tests the controller HTTP API
# Requires: switch running (make switch), controller running (make controller)
test-hardware-controller:
	@echo "=== Running hardware controller API tests (pytest) ==="
	@echo "NOTE: Requires switch AND controller running"
	cd test && uv run --system pytest test_hardware_controller.py -v

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
        extract-sde setup-rdc config-profile extract-bsp build-profile setup-model setup-hw \
	build install model switch load-kmods clean-build \
        test-dataplane test-controller test-hardware test-hardware-controller \
        controller clean help
