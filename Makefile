# SDE path can be overridden on command line: make link-p4studio SDE=/home/stan/sde
SDE ?=
# Profile path can be overridden: make build-profile PROFILE=profiles/tofino2-hardware.yaml
PROFILE ?= profiles/tofino2-hardware.yaml

build:
	./p4studio/p4studio build t2na_load_balancer

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

.PHONY: build model switch test-dataplane test-controller link-p4studio build-profile
