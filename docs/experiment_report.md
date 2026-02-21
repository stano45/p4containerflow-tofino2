# P4ContainerFlow: Experiment Report

This document describes how we benchmarked live container migration through a P4-programmable switch. It covers the testbed, the workload, the migration procedure, metrics collection, and results from a 25-migration run.

- [P4ContainerFlow: Experiment Report](#p4containerflow-experiment-report)
  - [Testbed](#testbed)
  - [Workload](#workload)
    - [Server](#server)
    - [Load Generator](#load-generator)
    - [Design Tradeoffs](#design-tradeoffs)
  - [Migration Procedure](#migration-procedure)
    - [Phase 1: Checkpoint](#phase-1-checkpoint)
    - [Phase 2: Transfer](#phase-2-transfer)
    - [Phase 3: Restore](#phase-3-restore)
    - [Phase 4: Switch Update](#phase-4-switch-update)
    - [What Counts as Downtime](#what-counts-as-downtime)
  - [Metrics Collection](#metrics-collection)
    - [Plotting](#plotting)
  - [Results](#results)
    - [Steady-State Performance](#steady-state-performance)
    - [Migration Timing](#migration-timing)
    - [Connection Recovery](#connection-recovery)
    - [Observations and Limitations](#observations-and-limitations)
  - [Experiment Automation](#experiment-automation)
    - [Reproducing](#reproducing)

## Testbed

```
    Control Machine                    Container Subnet 192.168.12.0/24
    (orchestrator)
         |                     +--- macvlan-shim .100 (loadgen data path)
         | SSH                 |
         |          +----------+----------+              +-----------------+
         +--------->|      lakewood       |  25G direct  |    loveland     |
         |          |  stream-server .2   |<============>|  (restore tgt)  |
         |          |  loadgen (native)   | Mellanox DAC |                 |
         |          +--------|------------+  checkpoint  +--------|--------+
         |                   | NFP 25G                            | NFP 25G
         |                   |                                    |
         |          +--------+--------------------------------+---+--------+
         +--------->|              Tofino Switch (Wedge100BF-32X)          |
                    |  bf_switchd + P4 load balancer                       |
                    |  Controller API :5000                                |
                    |  Port 2/0 (D_P 140) <---> lakewood                  |
                    |  Port 3/0 (D_P 148) <---> loveland                  |
                    +---------------------------------------------------------+

    Data path:    loadgen --> macvlan-shim --> P4 switch --> server container
    Metrics path: collector --> SSH tunnel --> loadgen :9090 + server :8081
    Checkpoint:   source --> socat (25G direct) --> target
```

The experiment runs on a three-node testbed. A Wedge100BF-32X switch running Tofino 1 sits between two Dell R740 servers (lakewood and loveland), each with 20 cores and 192 GB of RAM. The servers connect to the switch via Netronome NFP 25G NICs. They also have a direct 25G DAC link between them using Mellanox ConnectX NICs, which is used exclusively for checkpoint transfer during migration and never carries application traffic. The switch runs `bf_switchd` with the P4 load balancer program and the Python controller, which exposes an HTTP API on port 5000. The entire experiment is orchestrated from a separate control machine via SSH.

The container subnet is `192.168.12.0/24`. The server container runs at `192.168.12.2` with a fixed MAC address (`02:42:c0:a8:0c:02`). A macvlan-shim interface on lakewood (`192.168.12.100`) gives the load generator host-level access to the container without traversing an SSH tunnel for the data path. The virtual IP served by the load balancer is `192.168.12.10`, though the current experiment uses same-IP migration and the load generator connects directly to `192.168.12.2`.

## Workload

The workload consists of a WebSocket server and a load generator, both written in Go.

### Server

The server (`cmd/server/main.go`) is a statically compiled Go binary packaged in an Alpine-based container image. It listens on port 8080 for WebSocket connections and port 8081 for metrics. Each connected client gets a writer goroutine that sends data frames at a configurable rate (default 30 fps). Each frame carries a 512-byte padding payload, a sequence number, and a nanosecond timestamp. When a client sends a ping (a JSON message with a sequence number and timestamp), the server echoes it back with the original client timestamp and a server timestamp, which is how we measure round-trip time.

The server supports a quiesce mode toggled by `SIGUSR2`. When quiesced, the writer goroutines stop generating data frames and the echo handler stops writing responses. This allows the TCP send queue to drain before a CRIU checkpoint. After restore, `cr_hw.sh` sends `SIGUSR2` again to resume normal operation. The writer goroutines tolerate up to 30 consecutive write errors before giving up, which prevents a brief migration outage from killing client connections. Echoes are routed through a buffered channel (capacity 64) so the reader goroutine never blocks on writes, avoiding deadlocks when the TCP send buffer fills during migration.

The server also drops stale pings. If a client message's timestamp is more than 1000 ms old (which happens when pings accumulate during a CRIU freeze), it is silently discarded. This keeps post-migration RTT measurements from being skewed by messages that piled up during the freeze.

The metrics endpoint reports connected clients, total clients (lifetime), uptime, bytes sent and received, CPU usage (sampled from `/proc/self/stat`), and memory usage (from Go's runtime memory stats).

### Load Generator

The load generator (`cmd/loadgen/main.go`) opens a configurable number of concurrent WebSocket connections (default 4) to the server. Each connection runs two goroutines: a ping loop that sends timestamped messages at 100 ms intervals, and a read loop that processes responses. When an echo arrives, the load generator computes the round-trip time from the embedded client timestamp and records it in a per-connection sample buffer. Jitter is computed as the mean absolute difference between consecutive RTT samples. RTT values above a configurable cap (default 1000 ms) are discarded, since they represent stale echoes from a migration freeze rather than real network latency.

The load generator exposes an HTTP metrics endpoint on port 9090 that returns aggregated statistics: connected clients, RTT percentiles (p50, p95, p99, max), average RTT, jitter, bytes sent and received, and cumulative connection drops. The percentile computation uses linear interpolation on sorted samples and resets the sample buffer on each scrape, so each metrics response represents only the most recent collection interval.

Connections are opened with a 200 ms delay between each to avoid hammering the server at startup. If a connection drops, the load generator logs it, increments the drop counter, and the connection goroutines exit. Reconnection is not handled within the existing goroutines; the initial connect-with-retry loop uses exponential backoff (500 ms to 3 seconds) for the startup phase only.

### Design Tradeoffs

The workload is simple on purpose. It does not simulate a realistic application with complex state or database dependencies. The server's in-memory state is small (a few goroutines, a map of client connections, byte counters) and its checkpoint is only 6 to 7 MB. We want to measure the migration mechanism itself, not application performance under load. A heavier workload would increase checkpoint size and restore time, but the migration procedure and switch update latency would stay the same.

The 30 fps data frame rate was chosen to keep the TCP send queue reasonably active without overwhelming the CRIU checkpoint with unsent data. Higher frame rates increase the risk of CRIU restore failures: if the send queue contains data at checkpoint time, CRIU must replay that data on restore, and if the peer's TCP state has drifted (which can happen after multiple rapid migrations), the replayed data may be rejected with EPIPE. The quiesce-and-drain sequence before checkpoint is specifically designed to avoid this failure mode. The `cr_hw.sh` script waits up to 2 seconds for the send queue to drain by polling `ss -tn` inside the container's network namespace.

The choice to use same-IP migration (the container keeps `192.168.12.2` on both hosts, and only the switch's forward table changes) instead of VIP-based migration (where the container gets a new IP and the action selector member is updated) matters for the experiment design. Same-IP migration preserves TCP connections transparently because the client never sees an address change. It requires only a forward table update on the switch (`/updateForward`), which takes 26 to 31 ms, rather than an action profile member update plus forwarding changes (`/migrateNode`). The tradeoff is that same-IP migration requires deleting the `client_snat` entry (otherwise the SNAT rule rewrites the source address and the client's TCP stack rejects the packet), which means the VIP-based load balancing path is disabled during the experiment.

## Migration Procedure

```
  Migration Timeline (single migration, typical ~5.8s downtime)

  t=0                                                          t=6.1s
  |                                                              |
  |  Checkpoint       Transfer     Restore            Switch     |
  |  (CRIU dump)      (socat)      (CRIU restore +    (gRPC      |
  |                                 macvlan fix)       update)   |
  |<--- 1550 ms --->|<- 430 ms ->|<---- 3270 ms ---->|<30ms>|   |
  |                 |            |                    |      |   |
  |  SIGUSR2        |  source    |  source rm'd      | /update  |
  |  quiesce        |  listens   |  macvlan recreated | Forward  |
  |  drain TCP      |  target    |  SIGUSR2 resume   |      |   |
  |  podman ckpt    |  pulls     |  grat ARP         |      |   |
  |                 |            |                    |      |   |
  |=== container frozen (TCP unresponsive) ===========|======|   |
  |                                                          |   |
  |<----------- client-visible downtime (5.8s) ------------>||   |
  |                                                   ready  |   |
  |                                                          |   |
  |                                              post-switch |   |
  |                                              housekeeping|   |
```

Each migration is performed by `cr_hw.sh`, which runs on the source node and communicates with the target node over the direct Mellanox link. The script runs in `CR_RUN_LOCAL=1` mode, meaning it executes locally on the source rather than over SSH, to minimize latency in the checkpoint phase.

### Phase 1: Checkpoint

The script first queries the container's image ID (needed to verify the image exists on the target) and starts preparing the target node in the background (creating the checkpoint directory, verifying the image). It then quiesces the server by sending `SIGUSR2`, which pauses data frame generation. It waits for the TCP send queue to drain by polling `ss` inside the container's network namespace, checking every 100 ms for up to 2 seconds. Once the queue is empty, it runs `podman container checkpoint --export /tmp/checkpoints/checkpoint.tar --compress none --keep --tcp-established`. The `--compress none` flag skips compression to save time at the cost of a larger file. The `--keep` flag leaves the container in a stopped state (rather than removing it) so its state can be inspected if something goes wrong. The `--tcp-established` flag is required: it tells CRIU to dump the full TCP socket state, including sequence numbers, window sizes, and connection parameters, so that connections can be restored on the other host. CRIU is also configured with `skip-in-flight` (via `/etc/criu/default.conf`) to handle half-open connections gracefully.

### Phase 2: Transfer

The checkpoint tarball is transferred from the source to the target over the direct 25G Mellanox link using `socat`. The source listens on a random port and the target connects to pull the file. Transfer of a 6 to 7 MB checkpoint takes approximately 420 to 450 ms. Using the direct link instead of going through the switch avoids interference with the data plane and provides full 25G bandwidth. The script verifies the received file size matches the source (unless `CR_SKIP_VERIFY=1` is set for speed).

### Phase 3: Restore

Before restoring, the source container is removed to prevent its macvlan interface from responding to ARP broadcasts on the old host. If the source container were left alive, the client's ARP re-resolution (which happens every 20 to 30 seconds) could pick up the old MAC, breaking the data path.

The restore runs `podman container restore --import /tmp/checkpoints/checkpoint.tar --keep --tcp-established --ignore-static-mac`. After CRIU restores the process tree and TCP sockets, the macvlan network interface must be recreated. CRIU restores the container's network namespace from the checkpoint, but the macvlan references the source host's NIC index, which is wrong on the target. The script enters the container's network namespace via `nsenter`, deletes the stale `eth0`, creates a new macvlan on the target's NIC with the same fixed MAC address, moves it into the namespace, assigns the IP, and brings it up. It also flushes the route cache so TCP sockets perform a fresh lookup through the new interface, and pre-populates an ARP entry for the macvlan-shim to avoid resolution delay on the first retransmission. Finally, it sends a gratuitous ARP to update upstream caches.

The server is then resumed by sending `SIGUSR2` again to toggle quiesce mode off.

If the restore fails, the script captures the CRIU restore log and provides a diagnosis. The most common failure mode is a "send queue data: Broken pipe" error, which means TCP data was still queued at checkpoint time and could not be replayed after restore because the peer rejected it. The script's output explains this and suggests reducing `--fps` or checkpoint frequency.

### Phase 4: Switch Update

The script calls the controller's `/updateForward` endpoint with the server IP, the target switch port, and the MAC address. This modifies the `forward` and `arp_forward` tables so subsequent packets are routed to the correct physical port. The HTTP call is attempted first from the source node and falls back to calling the switch directly if the source cannot reach the controller. This phase consistently takes 26 to 31 ms.

### What Counts as Downtime

Client-visible downtime begins at the checkpoint (when the container is frozen and TCP connections stop responding) and ends when the switch table is updated and the container is reachable on the new host. This is captured by the `time_to_ready_ms` metric, which ranged from 5.7 to 6.2 seconds across the 25 migrations. The total script runtime (`total_ms`) is slightly longer because it includes post-switch housekeeping like writing timing files and signaling the collector.

## Metrics Collection

```
  Metrics Pipeline

  +-------------+        +-------------+
  | server      |        | loadgen     |
  | :8081       |        | :9090       |
  | /metrics    |        | /metrics    |
  +------+------+        +------+------+
         |                       |
    macvlan-shim            localhost
    192.168.12.2            lakewood
         |                       |
  +------+-----------------------+------+
  |         SSH tunnel (lakewood)       |
  |  -L 18081:192.168.12.2:8081        |
  |  -L 19090:localhost:9090           |
  +------------------+------------------+
                     |
          +----------v-----------+
          |  collector (local)   |
          |  scrape every 1s     |
          |  write metrics.csv   |
          |  check migration_flag|
          +----------+-----------+
                     |
          +----------v-----------+
          |  plot_metrics.py     |
          |  12 PDF plots        |
          +----------------------+
```

Metrics come from three sources: the server's built-in metrics endpoint, the load generator's metrics endpoint, and a standalone collector binary.

The collector (`cmd/collector/main.go`) runs on the control machine and scrapes both endpoints once per second through an SSH tunnel. The tunnel forwards `localhost:19090` to lakewood's load generator port (9090) and `localhost:18081` to the server's metrics port (8081, via the macvlan-shim). So metrics go through SSH, but the actual data path (load generator to server) does not. The control machine is not on the container subnet, so the tunnel is needed only for scraping.

Each scrape produces one row in a CSV file with 19 columns: timestamp, elapsed seconds, connected clients (server-side and load-generator-side), bytes sent and received, uptime, RTT percentiles, jitter, connection drops, CPU usage, memory, and a migration event flag. The migration flag is set by the migration script, which touches a flag file that the collector checks on each tick. When the flag file exists, the collector records `migration_event=1` and deletes the file.

### Plotting

The `analysis/plot_metrics.py` script reads the CSV and migration timing files and generates visualizations in both PDF (vector) and PNG (for Markdown rendering). It scans the results directory for all `migration_timing_*.txt` files and overlays migration events on time-series plots as vertical shaded spans. All plots shown in the Results section below are generated by this script. The full set of outputs for a 25-migration run is: `ws_rtt`, `ws_jitter`, `throughput`, `connection_health`, `migration_timing`, `migration_bars`, `container_resources`, `rtt_by_location`, `phase_variability`, `downtime_cdf`, `ensemble_rtt_recovery`, and `ensemble_throughput_recovery` (each as `.pdf` and `.png`).

## Results

The results discussed here come from experiment run `run_20260218_222451`, which performed 25 CRIU migrations alternating between lakewood and loveland with 30-second intervals. Plots are not checked into git. To generate them locally, run:

```bash
cd experiments
pip install -r analysis/requirements.txt
python3 analysis/plot_metrics.py \
  --csv results/run_20260218_222451/metrics.csv \
  --migration-flag results/run_20260218_222451 \
  --output-dir results/run_20260218_222451
```

### Steady-State Performance

During the 30-second warm-up phase with four concurrent WebSocket connections and no migrations, latency was stable and low. Average round-trip times settled between 0.31 and 0.41 ms. The median (p50) hovered around 0.32 ms. The 95th percentile was typically between 0.42 and 0.69 ms, with occasional spikes to 0.7 ms. Jitter remained consistently low at 0.04 to 0.08 ms, with rare spikes to 0.16 ms that correlate with operating system scheduling jitter rather than network effects. Per-connection throughput was approximately 116 KB/s (about 71 KB/s of server data frames plus the echo and ping overhead). Server CPU usage was 1 to 2% and memory was stable at 7 to 11 MB.

![Application-Layer RTT](../experiments/results/run_20260218_222451/ws_rtt.png)

The RTT plot above shows p50, p95, and p99 latency on a log scale over the full experiment duration. Each migration event is visible as a vertical shaded band where no echo responses are received (gaps in the trace). Between migrations, RTT drops back to sub-millisecond levels right away.

![Application-Layer Jitter](../experiments/results/run_20260218_222451/ws_jitter.png)

Jitter follows the same pattern: near-zero during steady state with brief spikes at each migration boundary.

![Server Throughput](../experiments/results/run_20260218_222451/throughput.png)

Throughput drops to zero during each migration (the container is frozen) and recovers to its steady-state level of roughly 70 KB/s within seconds of restore.

### Migration Timing

Across the 25 migrations, total script runtime ranged from 6128 ms (migration 20, loveland to lakewood) to 6606 ms (migration 25, lakewood to loveland). The per-phase breakdown was consistent across all runs:

![Migration Phase Breakdown](../experiments/results/run_20260218_222451/migration_timing.png)

Checkpoint time averaged 1568 ms (standard deviation 113 ms), including the quiesce signal, send queue drain, and CRIU dump. Transfer averaged 431 ms (standard deviation 14 ms). The checkpoint tarball grew from 6.5 MB (migration 1) to 7.8 MB (migration 15) and then fluctuated around 7.2 to 7.5 MB for the remaining migrations. The growth reflects accumulation of WebSocket session state (goroutine stacks, connection maps, buffer allocations). Transfer speed over the direct 25G link was approximately 15 to 17 MB/s, which is far below the link's theoretical capacity. The bottleneck is the `socat` TCP overhead and small file size rather than the link itself.

Restore averaged 3273 ms (standard deviation 194 ms), making up roughly 53% of total time. CRIU must reconstruct the process tree, restore file descriptors, re-establish TCP sockets with their exact sequence numbers, map memory pages, and recreate the macvlan interface. The variance in restore time comes primarily from the macvlan recreation and gratuitous ARP steps, which involve kernel operations with non-deterministic timing.

Switch table update averaged 30 ms (standard deviation 2 ms) across all 25 migrations. This represents the time for the controller to receive the HTTP POST, execute two gRPC calls to `switchd` (one for `forward`, one for `arp_forward`), and return. The gRPC calls modify TCAM entries on the ASIC, which happens at wire speed.

![Per-Migration Bars](../experiments/results/run_20260218_222451/migration_bars.png)

The per-migration bar chart above shows that the phase breakdown is consistent across all 25 migrations, with restore always dominating. Odd-numbered migrations (lakewood to loveland) and even-numbered migrations (loveland to lakewood) show no systematic difference in timing.

Client-visible downtime (`time_to_ready_ms`) ranged from 5695 to 6192 ms. This is the interval from checkpoint start to switch table update completion, after which packets are routed to the restored container on the new host.

![Downtime CDF](../experiments/results/run_20260218_222451/downtime_cdf.png)

The downtime CDF shows that 50% of migrations complete in under 5804 ms, 95% in under 6200 ms, and 99% in under 6389 ms. The mean is 5857 ms with a standard deviation of 223 ms.

### Connection Recovery

All four WebSocket clients observed a connection drop during each migration. The metrics CSV shows `connected_clients` dropping from 4 to 0 during the restore phase and recovering to 4 within one to two collection intervals (1 to 2 seconds) after the migration completes. The load generator's connection drop counter increments by 4 per migration. Recovery happens once the load generator detects the broken connection (via a failed read or write), closes the old WebSocket, and opens a new one. The server tolerates up to 30 consecutive write errors before dropping a client, so the server-side goroutine does not exit during a brief freeze.

![Connection Health](../experiments/results/run_20260218_222451/connection_health.png)

The connection health plot shows the drop-and-recovery pattern across all 25 migrations. Each vertical dip corresponds to a migration event where all 4 connections briefly show as frozen, then recover.

![Ensemble RTT Recovery](../experiments/results/run_20260218_222451/ensemble_rtt_recovery.png)

The ensemble recovery plot aligns all 25 migrations at t=0 and overlays their RTT recovery profiles. The interquartile range (p25 to p75) is shown as a shaded band. RTT returns to sub-millisecond levels within approximately 5.9 seconds of migration start on average, which corresponds to the time-to-ready metric.

![RTT by Location](../experiments/results/run_20260218_222451/rtt_by_location.png)

The RTT-by-location plot segments latency by which server (lakewood or loveland) is currently hosting the container. There is no measurable difference in steady-state RTT between the two hosts, which makes sense given the symmetric topology.

No migrations failed in this run. All 25 completed with TCP connections surviving the checkpoint/restore cycle. The `skip-in-flight` CRIU option combined with the send-queue drain procedure avoids the most common failure: EPIPE on restore because of stale data in the send queue.

![Phase Variability](../experiments/results/run_20260218_222451/phase_variability.png)

![Ensemble Throughput Recovery](../experiments/results/run_20260218_222451/ensemble_throughput_recovery.png)

![Container Resources](../experiments/results/run_20260218_222451/container_resources.png)

### Observations and Limitations

The restore phase is the bottleneck. It could potentially be improved with lazy page restoration (CRIU's `--lazy-pages` mode, which defers memory page transfers), pre-staging the checkpoint on the target (starting the transfer before the container is fully stopped), or using a faster serialization format than the uncompressed tarball.

The 30-second interval between migrations is conservative. Shorter intervals would increase the risk of send-queue-related restore failures and would not allow the system to reach steady state between events. The `--fps 30` setting is a tradeoff: lower rates would reduce send queue risk but would also make the workload less representative of a streaming application.

The checkpoint size growth over 25 migrations is modest (6.5 to 7.8 MB) and plateaus after roughly 15 migrations. A longer-lived server with more complex state (caches, connection history, session data) would produce larger checkpoints and proportionally longer transfer and restore times. We intentionally keep this small so the numbers reflect the migration mechanism, not the application.

The direct Mellanox link for checkpoint transfer is specific to this testbed. In a real deployment, the transfer would go through the data center network, and the latency would depend on checkpoint size and available bandwidth. The 420 ms we see for a 7 MB checkpoint gives a rough baseline for a dedicated path. On a shared or slower network it would be worse.

## Experiment Automation

```
  Experiment Timeline (run_20260218_222451)

  |  Setup  | Warm-up |  M1  | wait |  M2  | wait | ... |  M25 | wait  | Collect |
  |         |  30s    |      | 30s  |      | 30s  |     |      | 30s   |         |
  |<------->|<------->|<---->|<---->|<---->|<---->|     |<---->|<----->|<------->|
  0s                  30s          ~68s         ~106s        ~1020s        ~1050s

  Setup: SSH checks, build images, start switchd + controller, create containers
  M1..M25: alternating lakewood-->loveland and loveland-->lakewood
  Collect: stop loadgen + collector, copy logs, generate plots
```

The whole experiment is driven by `run_experiment.sh`, which accepts three parameters: `--steady-state` (seconds to wait before the first migration, default 15), `--post-migration` (seconds between migrations and after the last one, default 30), and `--migrations` (number of CRIU migrations to perform, default 1). The run that produced these results used `--steady-state 30 --post-migration 30 --migrations 25`.

The script handles everything from SSH connectivity checks to final plot generation. It syncs experiment scripts and controller code to the lab nodes via rsync, builds and deploys the server container image (including cross-node sync from lakewood to loveland via `podman save`/`scp`/`podman load`), cross-compiles the load generator for linux/amd64 and deploys it to lakewood, starts `switchd` and the controller on the switch if they are not already running, and sets up the macvlan networks and containers on both servers. After the warm-up and migration phases, it collects logs, copies migration timing files back to the control machine, and runs the plotting pipeline.

Each migration is executed by SSHing to the source node with agent forwarding enabled (so the source can reach the switch to update the forwarding table) and running `cr_hw.sh` locally on that node. The migration direction alternates automatically: odd-numbered migrations go lakewood to loveland, even-numbered ones go loveland to lakewood. Results are written to a timestamped directory under `experiments/results/`, making it easy to compare runs.

### Reproducing

To reproduce the experiment on the same testbed:

```bash
cd experiments
./run_experiment.sh --steady-state 30 --post-migration 30 --migrations 25
```

This requires SSH access to lakewood, loveland, and the tofino switch, with the connection details configured in `config_hw.env`. The switch must have `switchd` running with the P4 load balancer program loaded. Podman, CRIU, and crun must be installed on both servers (the script will install CRIU and crun on loveland automatically if they are missing). The control machine needs Go 1.24+ (to build the collector and load generator), Python 3 with matplotlib/pandas/seaborn (for plotting), and Rust (optional, for the checkpoint editing tool).
