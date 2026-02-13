// Package main implements a metrics collector for the WebRTC container
// migration experiment. Runs ON lakewood (the source node).
//
// Before migration: all probes are local (podman exec, nsenter, podman stats).
// After migration: server metrics/stats are fetched from loveland via the
// direct 25G link (ssh 192.168.10.3) — sub-millisecond latency, no detour
// through the university network.
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
	remoteDirectIP = flag.String("remote-direct-ip", "", "Direct-link IP of the target node (e.g. 192.168.10.3)")
	metricsPort    = flag.Int("metrics-port", 8081, "Server /metrics port inside the container")
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
	ActivePeers   int     `json:"active_peers"`
	TotalPeers    int     `json:"total_peers"`
	BytesSent     int64   `json:"bytes_sent"`
	BytesReceived int64   `json:"bytes_received"`
	FramesSent    int64   `json:"frames_sent"`
	KeyframesSent int64   `json:"keyframes_sent"`
	UptimeSeconds float64 `json:"uptime_seconds"`
	AvgBitrateBps float64 `json:"avg_bitrate_bps"`
}

type ContainerStats struct {
	CPUPercent string
	MemUsage   string
	MemPercent string
}

// ---------------------------------------------------------------------------
// Local probes (before migration — everything is on this machine)
// ---------------------------------------------------------------------------

// findServerContainer returns the name and PID of the running server container.
func findServerContainer(names []string) (string, string) {
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
// Remote probes (after migration — server is on the other node, reached via
// the direct 25G link, e.g. ssh 192.168.10.3)
// ---------------------------------------------------------------------------

func fetchMetricsRemote(directIP string, names []string, port int) ServerMetrics {
	var sm ServerMetrics
	if directIP == "" {
		return sm
	}
	nameList := strings.Join(names, " ")
	script := fmt.Sprintf(`
SPID=0
for N in %s; do
  P=$(sudo podman inspect --format '{{.State.Pid}}' "$N" 2>/dev/null) && [ -n "$P" ] && [ "$P" != "0" ] && SPID=$P && break
done
[ "$SPID" = "0" ] && echo '{}' && exit 0
sudo nsenter -t "$SPID" -n curl -sf --max-time 1 http://localhost:%d/metrics 2>/dev/null || echo '{}'
`, nameList, port)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ssh",
		"-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=2",
		directIP, script).Output()
	if err != nil {
		return sm
	}
	_ = json.Unmarshal([]byte(strings.TrimSpace(string(out))), &sm)
	return sm
}

func fetchStatsRemote(directIP string, names []string) ContainerStats {
	if directIP == "" {
		return ContainerStats{}
	}
	nameList := strings.Join(names, " ")
	script := fmt.Sprintf(`
for N in %s; do
  sudo podman stats --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' "$N" 2>/dev/null && exit 0
done
echo '||'
`, nameList)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ssh",
		"-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=2",
		directIP, script).Output()
	if err != nil {
		return ContainerStats{}
	}
	return parseStats(string(out))
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
	// Parse "time=X.XX ms"
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

func checkMigrationFlag(path string) bool {
	_, err := os.Stat(path)
	return err == nil
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
	header := []string{
		"timestamp", "timestamp_unix_milli", "elapsed_s",
		"active_peers", "total_peers", "bytes_sent", "bytes_received",
		"frames_sent", "keyframes_sent", "uptime_s", "avg_bitrate_bps",
	}
	for _, h := range pingTargets {
		header = append(header, fmt.Sprintf("ping_ms_%s", h))
	}
	// Use first server name for column headers
	label := "server"
	if len(srvNames) > 0 {
		label = srvNames[0]
	}
	header = append(header, fmt.Sprintf("cpu_%s", label), fmt.Sprintf("mem_%s", label), fmt.Sprintf("mem_pct_%s", label))
	header = append(header, "migration_event")
	_ = w.Write(header)
	w.Flush()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sigCh; log.Println("Shutting down..."); cancel() }()

	startTime := time.Now()
	migrated := false
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	log.Printf("Collector started: remote=%s ping=%v server-names=%v interval=%s",
		*remoteDirectIP, pingTargets, srvNames, *interval)

	for {
		select {
		case <-ctx.Done():
			log.Println("Collector stopped.")
			return

		case t := <-ticker.C:
			elapsed := t.Sub(startTime).Seconds()

			var sm ServerMetrics
			var cs ContainerStats

			if !migrated {
				// Local probes — server is on this machine
				name, pid := findServerContainer(srvNames)
				sm = fetchMetricsLocal(pid, *metricsPort)
				cs = fetchStatsLocal(name)
			} else {
				// Remote probes — server migrated to the other node
				sm = fetchMetricsRemote(*remoteDirectIP, srvNames, *metricsPort)
				cs = fetchStatsRemote(*remoteDirectIP, srvNames)
			}

			// Pings — always from local loadgen container
			_, loadgenPID := findServerContainer([]string{*loadgenName})

			// Build row
			row := []string{
				t.Format(time.RFC3339Nano),
				fmt.Sprintf("%d", t.UnixMilli()),
				fmt.Sprintf("%.3f", elapsed),
				itoa(sm.ActivePeers), itoa(sm.TotalPeers),
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
			if !migrated && checkMigrationFlag(*migrationFlg) {
				migrated = true
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
