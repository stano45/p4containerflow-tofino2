init-submodule:
	@echo "Initializing open-p4studio submodule..."
	git submodule update --init --recursive

check-python:
	@echo "Checking Python version..."
	@python3 -c "import sys; v = sys.version_info; exit(0 if v.major == 3 and v.minor <= 11 else 1)" || \
		(echo "ERROR: Python 3.11 or earlier is required (found $$(python3 --version))"; \
		 echo "Python 3.12+ removed distutils which is required by open-p4studio."; \
		 exit 1)
	@echo "Python version OK: $$(python3 --version)"

install-p4studio: init-submodule check-python
	@echo "Installing open-p4studio for Tofino 2 and 2M..."
	cd open-p4studio && ./p4studio/p4studio profile apply ../p4studio-tofino2.yaml

setup-env: install-p4studio
	@echo "Creating setup script..."
	cd open-p4studio && ./create-setup-script.sh > ~/setup-open-p4studio.bash
	@echo "Setup script created at ~/setup-open-p4studio.bash"
	@echo "Add 'source ~/setup-open-p4studio.bash' to your shell profile or run it manually."

setup: setup-env
	@echo ""
	@echo "✓ Initial setup complete!"
	@echo "  Run: source ~/setup-open-p4studio.bash"
	@echo "  Then you can use the other make targets."

build:
	./open-p4studio/p4studio build t2na_load_balancer

model: build
	./run_tofino_model.sh --arch tf2 -p t2na_load_balancer

switch:
	./run_switchd.sh --arch tf2 -p t2na_load_balancer

link-p4studio:
	@# SDE path can be passed as argument: make link-p4studio SDE=/path/to/sde
	@# Or set via environment variable SDE
	@if [ -z "$(SDE)" ]; then \
	  echo "SDE path is not set. Usage: make link-p4studio SDE=/path/to/sde"; \
	  echo "Or set the SDE environment variable."; \
	  exit 1; \
	fi; \
	PKGSRCDIR="$(SDE)/pkgsrc/p4-examples/p4_16_programs"; \
	mkdir -p "$$PKGSRCDIR"; \
	ln -sfn "$$PWD" "$$PKGSRCDIR/t2na_load_balancer" && echo "symlink created: $$PKGSRCDIR/t2na_load_balancer"

build-profile:
	@# Usage: make build-profile SDE=/path/to/sde PROFILE=profiles/tofino2-hardware.yaml
	@if [ -z "$(SDE)" ]; then \
	  echo "SDE path is not set. Usage: make build-profile SDE=/path/to/sde PROFILE=path/to/profile.yaml"; \
	  exit 1; \
	fi; \
	if [ -z "$(PROFILE)" ]; then \
	  echo "PROFILE path is not set. Usage: make build-profile SDE=/path/to/sde PROFILE=path/to/profile.yaml"; \
	  exit 1; \
	fi; \
	PROFILE_PATH="$$PWD/$(PROFILE)"; \
	echo "Applying profile $$PROFILE_PATH using $(SDE)/p4studio/p4studio"; \
	cd "$(SDE)" && ./p4studio/p4studio profile apply "$$PROFILE_PATH"

controller:
	cd ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/controller && ./run.sh

test-dataplane:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_dataplane -p t2na_load_balancer

test-controller:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_controller -p t2na_load_balancer

.PHONY: init-submodule check-python install-p4studio setup-env setup build model switch controller test-dataplane test-controller
