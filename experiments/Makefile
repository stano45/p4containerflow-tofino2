# =============================================================================
# experiments/Makefile — Container migration experiment
# =============================================================================
#
# Targets mirror the p4containerflow/examples/redis pattern:
#
#   make all            Build images + set up experiment
#   make build-server   Build the stream server container image
#   make build-loadgen  Build the load generator container image
#   make build          Run build.sh (create networks, pods, containers)
#   make controller     Start the P4 controller (in parent project)
#   make migrate        Run CRIU migration (SOURCE=x TARGET=y)
#   make collector      Run the metrics collector
#   make plot           Generate charts from CSV
#   make clean          Teardown everything
#
# =============================================================================

.PHONY: all build-server build-loadgen build controller migrate \
        collector plot clean \
        hw-build hw-migrate hw-collector hw-run hw-clean

all: build-server build-loadgen build controller

# ---------------------------------------------------------------------------
# Container Image Builds
# ---------------------------------------------------------------------------

build-server:
	sudo podman build -t stream-server -f cmd/server/Containerfile .

build-loadgen:
	sudo podman build -t stream-client -f cmd/loadgen/Containerfile .

# ---------------------------------------------------------------------------
# Experiment Lifecycle
# ---------------------------------------------------------------------------

build: clean
	./build.sh

controller:
	sleep 2 && cd .. && make controller

migrate:
	@if [ "$(SOURCE)" = "" ] || [ "$(TARGET)" = "" ]; then \
		echo "Usage: make migrate SOURCE=x TARGET=y"; \
		echo "  Example: make migrate SOURCE=2 TARGET=3"; \
	else \
		./cr.sh $(SOURCE) $(TARGET); \
	fi

# ---------------------------------------------------------------------------
# Metrics & Analysis
# ---------------------------------------------------------------------------

collector:
	go run ./cmd/collector/ \
		-server-names stream-server \
		-loadgen-container stream-client \
		-metrics-port 8081 \
		-ping-hosts 10.0.2.2,10.0.3.3 \
		-output results/metrics.csv

plot:
	cd analysis && uv run plot_metrics.py --csv ../results/metrics.csv --output-dir ../results/

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean:
	./clean.sh

# ---------------------------------------------------------------------------
# Hardware (Multi-Node) Targets
# ---------------------------------------------------------------------------

hw-build: hw-clean
	./build_hw.sh

hw-migrate:
	./cr_hw.sh

hw-collector:
	go run ./cmd/collector/ \
		-server-names stream-server,h3 \
		-loadgen-container stream-client \
		-metrics-port 8081 \
		-loadgen-metrics-port 9090 \
		-ping-hosts 192.168.12.2 \
		-migration-flag /tmp/migration_event \
		-output results/metrics.csv

hw-run:
	./run_experiment.sh

hw-clean:
	./clean_hw.sh
