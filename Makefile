init-submodule:
	@echo "Initializing open-p4studio submodule..."
	git submodule update --init --recursive

check-python:
	@echo "Checking Python version..."
	@python3 -c "import sys; v = sys.version_info; exit(0 if v.major == 3 and v.minor <= 11 else 1)" || \
		(echo "ERROR: Python 3.11 or earlier is required (found $$(python3 --version))"; \
		 echo "Python 3.12+ removed distutils which is required by open-p4studio."; \
		 echo ""; \
		 echo "Recommended solution: Use a virtual environment with Python 3.11"; \
		 echo "  sudo apt install python3.11 python3.11-venv python3.11-dev"; \
		 echo "  python3.11 -m venv ~/p4studio-venv"; \
		 echo "  source ~/p4studio-venv/bin/activate"; \
		 echo "  make setup"; \
		 echo ""; \
		 echo "WARNING: Do NOT change system Python with update-alternatives!"; \
		 echo "This will break system tools like apt."; \
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
	./p4studio/p4studio build t2na_load_balancer

model: build
	./run_tofino_model.sh --arch tf2 -p t2na_load_balancer

switch:
	./run_switchd.sh --arch tf2 -p t2na_load_balancer

controller:
	cd ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/controller && ./run.sh

test-dataplane:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_dataplane -p t2na_load_balancer

test-controller:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_controller -p t2na_load_balancer

.PHONY: init-submodule check-python install-p4studio setup-env setup build model switch controller test-dataplane test-controller
