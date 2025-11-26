build:
	./p4studio/p4studio build t2na_load_balancer

model: build
	./run_tofino_model.sh --arch tf2 -p t2na_load_balancer

switch:
	./run_switchd.sh --arch tf2 -p t2na_load_balancer

link-p4studio:
	@# Use SDE env var only (SDE should point to the SDE root directory)
	@if [ -z "$${SDE:-}" ]; then \
	  echo "Environment variable SDE is not set. Set SDE to your SDE root (e.g. /opt/bf-sde)"; \
	  exit 1; \
	fi; \
	PKGSRCDIR="$${SDE}/pkgsrc/p4-examples/p4_16_programs"; \
	mkdir -p "$$PKGSRCDIR"; \
	ln -sfn "$$PWD" "$$PKGSRCDIR/t2na_load_balancer" && echo "symlink created: $$PKGSRCDIR/t2na_load_balancer"

controller:
	cd ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/controller && ./run.sh

test-dataplane:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_dataplane -p t2na_load_balancer

test-controller:
	./run_p4_tests.sh --arch tf2 -t ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/tests -s t2na_load_balancer_controller -p t2na_load_balancer

.PHONY: build model switch test-dataplane test-controller link-p4studio
