build:
	./p4studio/p4studio build t2na_load_balancer

model: build
	./run_tofino_model.sh --arch tf2 -p t2na_load_balancer

switch:
	./run_switchd.sh --arch tf2 -p t2na_load_balancer

controller:
	cd ./pkgsrc/p4-examples/p4_16_programs/t2na_load_balancer/controller && ./run.sh

test:
	./run_p4_tests.sh --arch tf2 -s t2na_load_balancer_custom -p t2na_load_balancer

.PHONY: build model switch controller test
