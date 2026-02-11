# =============================================================================
# experiments/Makefile — WebRTC container migration experiment
# =============================================================================
#
# Targets mirror the p4containerflow/examples/redis pattern:
#
#   make all            Build images + set up experiment
#   make build-server   Build the WebRTC server container image
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
        collector plot clean

all: build-server build-loadgen build controller

# ---------------------------------------------------------------------------
# Container Image Builds
# ---------------------------------------------------------------------------

build-server:
	sudo podman build -t webrtc-server -f cmd/server/Containerfile .

build-loadgen:
	sudo podman build -t webrtc-loadgen -f cmd/loadgen/Containerfile .

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
		-server-metrics http://10.0.2.2:8081/metrics \
		-controller http://127.0.0.1:5000 \
		-ping-hosts 10.0.2.2,10.0.3.3 \
		-containers webrtc-server \
		-output results/metrics.csv

plot:
	cd analysis && pip install -q -r requirements.txt && \
		python3 plot_metrics.py --csv ../results/metrics.csv --output-dir ../results/

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean:
	./clean.sh
