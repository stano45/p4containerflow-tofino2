// Package main implements a metrics collector for the WebRTC container
// migration experiment. Runs ON lakewood (the source node) as root.
//
// Metrics collection strategy (works across multiple migrations):
//   1. Server metrics: Try local container first. If not found, fetch from
//      the remote node via a persistent SSH multiplexed connection.
//   2. Container stats: Try local podman stats first. If not found, fetch
//      from the remote node via the same SSH connection.
//   3. Pings: Always from the loadgen container's netns (local).
//
// SSH multiplexing: a ControlMaster connection is established at startup
// and reused for all subsequent SSH commands, reducing per-command overhead
// from ~1-2s to ~50ms.
package main

import (
	"bufio"
	"context"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// CLI flags
// ---------------------------------------------------------------------------

var (
	remoteDirectIP = flag.String("remote-direct-ip", "", "Direct-link IP of the remote node (e.g. 192.168.10.3)")
	remoteSSHUser  = flag.String("remote-ssh-user", "", "SSH user for the remote node")
	metricsPort    = flag.Int("metrics-port", 8081, "Server /metrics port inside the container")
	serverIPs      = flag.String("server-ips", "", "Comma-separated container IPs to try for metrics (e.g. 192.168.12.2,192.168.12.3)")
	pingHosts      = flag.String("ping-hosts", "", "Comma-separated IPs to ping from loadgen netns")
	serverNames    = flag.String("server-names", "webrtc-server,h3", "Container names to try for the server")
	loadgenName    = flag.String("loadgen-container", "webrtc-loadgen", "Loadgen container name (for pings)")
	migrationFlg   = flag.String("migration-flag", "/tmp/migration_event", "Migration event flag file")
	outputFile     = flag.String("output", "metrics.csv", "CSV output path")
	interval       = flag.Duration("interval", 1*time.Second, "Collection interval")
)

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

type ServerMetrics struct {
	ActivePeers    int     `json:"active_peers"`
	ConnectedPeers int     `json:"connected_peers"`
	TotalPeers     int64   `json:"total_peers"`
	BytesSent      int64   `json:"bytes_sent"`
	BytesReceived  int64   `json:"bytes_received"`
	FramesSent     int64   `json:"frames_sent"`
	KeyframesSent  int64   `json:"keyframes_sent"`
	UptimeSeconds  float64 `json:"uptime_seconds"`
	AvgBitrateBps  float64 `json:"avg_bitrate_bps"`
}

type ContainerStats struct {
	CPUPercent string
	MemUsage   string
	MemPercent string
}

// ---------------------------------------------------------------------------
// SSH Multiplexing
// ---------------------------------------------------------------------------

// sshMux manages a persistent SSH ControlMaster connection.
type sshMux struct {
	user       string
	host       string
	socketPath string
}

func newSSHMux(user, host string) *sshMux {
	return &sshMux{
		user:       user,
		host:       host,
		socketPath: fmt.Sprintf("/tmp/collector-ssh-mux-%s-%s", user, host),
	}
}

// start establishes the ControlMaster background connection.
func (m *sshMux) start() error {
	// Clean up any stale socket
	os.Remove(m.socketPath)

	target := m.host
	if m.user != "" {
		target = m.user + "@" + m.host
	}
	cmd := exec.Command("ssh",
		"-o", "BatchMode=yes",
		"-o", "StrictHostKeyChecking=no",
		"-o", "ConnectTimeout=5",
		"-o", fmt.Sprintf("ControlPath=%s", m.socketPath),
		"-o", "ControlMaster=yes",
		"-o", "ControlPersist=yes",
		"-N", // no command, just hold connection
		target,
	)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("ssh mux start: %w", err)
	}
	// Wait a moment for the socket to appear
	for i := 0; i < 20; i++ {
		time.Sleep(100 * time.Millisecond)
		if _, err := os.Stat(m.socketPath); err == nil {
			log.Printf("SSH mux established to %s (socket=%s)", target, m.socketPath)
			return nil
		}
	}
	return fmt.Errorf("ssh mux socket never appeared at %s", m.socketPath)
}

// run executes a command over the multiplexed connection.
func (m *sshMux) run(ctx context.Context, script string) ([]byte, error) {
	target := m.host
	if m.user != "" {
		target = m.user + "@" + m.host
	}
	cmd := exec.CommandContext(ctx, "ssh",
		"-o", "BatchMode=yes",
		"-o", "StrictHostKeyChecking=no",
		"-o", fmt.Sprintf("ControlPath=%s", m.socketPath),
		target, script,
	)
	return cmd.Output()
}

// close tears down the ControlMaster.
func (m *sshMux) close() {
	target := m.host
	if m.user != "" {
		target = m.user + "@" + m.host
	}
	exec.Command("ssh",
		"-o", fmt.Sprintf("ControlPath=%s", m.socketPath),
		"-O", "exit",
		target,
	).Run()
	os.Remove(m.socketPath)
}

// ---------------------------------------------------------------------------
// Local probes — server container is on this machine
// ---------------------------------------------------------------------------

func findLocalContainer(names []string) (string, string) {
	for _, n := range names {
		out, err := exec.Command("sudo", "podman", "inspect", "--format", "{{.State.Pid}}", n).Output()
		if err == nil {
			pid := strings.TrimSpace(string(out))
			if pid != "" && pid != "0" {
				return n, pid
			}
		}
	}
	return "", ""
}

func fetchMetricsLocal(pid string, port int) ServerMetrics {
	var sm ServerMetrics
	if pid == "" {
		return sm
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "sudo", "nsenter", "-t", pid, "-n",
		"curl", "-sf", "--max-time", "1",
		fmt.Sprintf("http://localhost:%d/metrics", port)).Output()
	if err != nil {
		return sm
	}
	_ = json.Unmarshal(out, &sm)
	return sm
}

func fetchStatsLocal(name string) ContainerStats {
	if name == "" {
		return ContainerStats{}
	}
	out, err := exec.Command("sudo", "podman", "stats", "--no-stream",
		"--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}", name).Output()
	if err != nil {
		return ContainerStats{}
	}
	return parseStats(string(out))
}

// ---------------------------------------------------------------------------
// Remote probes via SSH multiplexed connection — fetches BOTH metrics and
// stats in a single SSH round-trip to minimize latency.
// ---------------------------------------------------------------------------

type remoteResult struct {
	Metrics ServerMetrics
	Stats   ContainerStats
}

func fetchRemoteBoth(mux *sshMux, names []string, port int) remoteResult {
	var result remoteResult
	if mux == nil {
		return result
	}
	nameList := strings.Join(names, " ")
	// Single script: find the server container, fetch metrics via nsenter/curl,
	// and podman stats, print both separated by a delimiter.
	script := fmt.Sprintf(`
SPID=0; SNAME=""
for N in %s; do
  P=$(sudo podman inspect --format '{{.State.Pid}}' "$N" 2>/dev/null) && [ -n "$P" ] && [ "$P" != "0" ] && SPID=$P && SNAME=$N && break
done
if [ "$SPID" = "0" ]; then
  echo '{}'
  echo '---'
  echo '||'
else
  sudo nsenter -t "$SPID" -n curl -sf --max-time 1 http://localhost:%d/metrics 2>/dev/null || echo '{}'
  echo '---'
  sudo podman stats --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' "$SNAME" 2>/dev/null || echo '||'
fi
`, nameList, port)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := mux.run(ctx, script)
	if err != nil {
		return result
	}

	parts := strings.SplitN(string(out), "---\n", 2)
	if len(parts) >= 1 {
		_ = json.Unmarshal([]byte(strings.TrimSpace(parts[0])), &result.Metrics)
	}
	if len(parts) >= 2 {
		result.Stats = parseStats(parts[1])
	}
	return result
}

// ---------------------------------------------------------------------------
// Ping — always from the loadgen container's network namespace (local)
// ---------------------------------------------------------------------------

func pingOnce(loadgenPID, host string) float64 {
	if loadgenPID == "" {
		return -1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "sudo", "nsenter", "-t", loadgenPID, "-n",
		"ping", "-c", "1", "-W", "1", host).Output()
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(out), "\n") {
		if idx := strings.Index(line, "time="); idx >= 0 {
			s := line[idx+5:]
			if end := strings.IndexByte(s, ' '); end > 0 {
				s = s[:end]
			}
			if v, err := strconv.ParseFloat(s, 64); err == nil {
				return v
			}
		}
	}
	return -1
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func parseStats(raw string) ContainerStats {
	parts := strings.Split(strings.TrimSpace(raw), "|")
	if len(parts) < 3 {
		return ContainerStats{}
	}
	return ContainerStats{
		CPUPercent: strings.TrimSpace(parts[0]),
		MemUsage:   strings.TrimSpace(parts[1]),
		MemPercent: strings.TrimSpace(parts[2]),
	}
}

func checkAndClearMigrationFlag(path string) bool {
	if _, err := os.Stat(path); err != nil {
		return false
	}
	_ = os.Remove(path)
	return true
}

func metricsValid(sm *ServerMetrics) bool {
	return sm != nil && (sm.UptimeSeconds > 0 || sm.BytesSent > 0)
}

func splitNonEmpty(s, sep string) []string {
	if s == "" {
		return nil
	}
	var result []string
	for _, p := range strings.Split(s, sep) {
		p = strings.TrimSpace(p)
		if p != "" {
			result = append(result, p)
		}
	}
	return result
}

func itoa(i int) string     { return strconv.Itoa(i) }
func i64toa(i int64) string { return strconv.FormatInt(i, 10) }

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	flag.Parse()

	f, err := os.Create(*outputFile)
	if err != nil {
		log.Fatalf("Cannot create output file: %v", err)
	}
	defer f.Close()
	w := csv.NewWriter(f)
	defer w.Flush()

	pingTargets := splitNonEmpty(*pingHosts, ",")
	srvNames := splitNonEmpty(*serverNames, ",")

	// CSV header
	label := "server"
	if len(srvNames) > 0 {
		label = srvNames[0]
	}
	header := []string{
		"timestamp", "timestamp_unix_milli", "elapsed_s",
		"active_peers", "total_peers", "bytes_sent", "bytes_received",
		"frames_sent", "keyframes_sent", "uptime_s", "avg_bitrate_bps",
	}
	for _, h := range pingTargets {
		header = append(header, fmt.Sprintf("ping_ms_%s", h))
	}
	header = append(header, fmt.Sprintf("cpu_%s", label), fmt.Sprintf("mem_%s", label), fmt.Sprintf("mem_pct_%s", label))
	header = append(header, "migration_event")
	_ = w.Write(header)
	w.Flush()

	// Set up SSH multiplexed connection for remote probes
	var mux *sshMux
	if *remoteDirectIP != "" && *remoteSSHUser != "" {
		mux = newSSHMux(*remoteSSHUser, *remoteDirectIP)
		if err := mux.start(); err != nil {
			log.Printf("WARNING: SSH mux failed: %v (remote probes will be slow)", err)
			mux = nil
		} else {
			defer mux.close()
		}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sigCh; log.Println("Shutting down..."); cancel() }()

	startTime := time.Now()
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	log.Printf("Collector started: remote=%s remote-user=%s mux=%v ping=%v server-names=%v interval=%s",
		*remoteDirectIP, *remoteSSHUser, mux != nil, pingTargets, srvNames, *interval)

	for {
		select {
		case <-ctx.Done():
			log.Println("Collector stopped.")
			return

		case t := <-ticker.C:
			elapsed := t.Sub(startTime).Seconds()

			// --- Server metrics + container stats ---
			// 1. Try local container (fast, no network)
			var sm ServerMetrics
			var cs ContainerStats
			name, pid := findLocalContainer(srvNames)
			if pid != "" {
				sm = fetchMetricsLocal(pid, *metricsPort)
				cs = fetchStatsLocal(name)
			}

			// 2. If local failed, fetch both metrics+stats from remote in one SSH call
			if !metricsValid(&sm) && mux != nil {
				r := fetchRemoteBoth(mux, srvNames, *metricsPort)
				sm = r.Metrics
				cs = r.Stats
			}

			// --- Pings ---
			_, loadgenPID := findLocalContainer([]string{*loadgenName})

			// --- Normalize peers ---
			activePeers := sm.ActivePeers
			if activePeers == 0 {
				activePeers = sm.ConnectedPeers
			}
			totalPeers := sm.TotalPeers
			if totalPeers == 0 && sm.ConnectedPeers > 0 {
				totalPeers = int64(sm.ConnectedPeers)
			}

			// Build row
			row := []string{
				t.Format(time.RFC3339Nano),
				fmt.Sprintf("%d", t.UnixMilli()),
				fmt.Sprintf("%.3f", elapsed),
				itoa(activePeers), itoa(int(totalPeers)),
				i64toa(sm.BytesSent), i64toa(sm.BytesReceived),
				i64toa(sm.FramesSent), i64toa(sm.KeyframesSent),
				fmt.Sprintf("%.1f", sm.UptimeSeconds),
				fmt.Sprintf("%.0f", sm.AvgBitrateBps),
			}
			for _, h := range pingTargets {
				rtt := pingOnce(loadgenPID, h)
				row = append(row, fmt.Sprintf("%.3f", rtt))
			}
			row = append(row, cs.CPUPercent, cs.MemUsage, cs.MemPercent)

			migEvent := "0"
			if checkAndClearMigrationFlag(*migrationFlg) {
				migEvent = "1"
				log.Println("Migration event detected")
			}
			row = append(row, migEvent)

			_ = w.Write(row)
			w.Flush()
		}
	}
}

// readMigrationTiming parses a key=value file (unused here but kept for compat).
func readMigrationTiming(path string) map[string]string {
	m := make(map[string]string)
	f, err := os.Open(path)
	if err != nil {
		return m
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if parts := strings.SplitN(line, "=", 2); len(parts) == 2 {
			m[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
		}
	}
	return m
}
