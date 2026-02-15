package main

import (
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

var (
	remoteDirectIP     = flag.String("remote-direct-ip", "", "Direct-link IP of the remote node")
	remoteSSHUser      = flag.String("remote-ssh-user", "", "SSH user for the remote node")
	metricsPort        = flag.Int("metrics-port", 8081, "Server /metrics port inside the container")
	loadgenMetricsPort = flag.Int("loadgen-metrics-port", 9090, "Loadgen /metrics port inside the container")
	pingHosts          = flag.String("ping-hosts", "", "Comma-separated IPs to ping from loadgen netns")
	serverNames        = flag.String("server-names", "stream-server,h3", "Container names to try for the server")
	loadgenName        = flag.String("loadgen-container", "stream-client", "Loadgen container name")
	migrationFlg       = flag.String("migration-flag", "/tmp/migration_event", "Migration event flag file")
	outputFile         = flag.String("output", "metrics.csv", "CSV output path")
	interval           = flag.Duration("interval", 1*time.Second, "Collection interval")
)

type ServerMetrics struct {
	ConnectedClients int     `json:"connected_clients"`
	TotalClients     int64   `json:"total_clients"`
	BytesSent        uint64  `json:"bytes_sent"`
	BytesReceived    uint64  `json:"bytes_received"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
}

type LoadgenMetrics struct {
	ConnectedClients int     `json:"connected_clients"`
	AvgRttMs         float64 `json:"avg_rtt_ms"`
	P50RttMs         float64 `json:"p50_rtt_ms"`
	P95RttMs         float64 `json:"p95_rtt_ms"`
	P99RttMs         float64 `json:"p99_rtt_ms"`
	MaxRttMs         float64 `json:"max_rtt_ms"`
	JitterMs         float64 `json:"jitter_ms"`
	BytesSent        uint64  `json:"bytes_sent"`
	BytesReceived    uint64  `json:"bytes_received"`
	ConnectionDrops  int64   `json:"connection_drops"`
}

type ContainerStats struct {
	CPUPercent string
	MemUsage   string
	MemPercent string
}

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

func (m *sshMux) start() error {
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
		"-N",
		target,
	)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("ssh mux start: %w", err)
	}
	for i := 0; i < 20; i++ {
		time.Sleep(100 * time.Millisecond)
		if _, err := os.Stat(m.socketPath); err == nil {
			log.Printf("SSH mux established to %s (socket=%s)", target, m.socketPath)
			return nil
		}
	}
	return fmt.Errorf("ssh mux socket never appeared at %s", m.socketPath)
}

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

func fetchLoadgenMetrics(loadgenPID string, port int) LoadgenMetrics {
	var lm LoadgenMetrics
	if loadgenPID == "" {
		return lm
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "sudo", "nsenter", "-t", loadgenPID, "-n",
		"curl", "-sf", "--max-time", "1",
		fmt.Sprintf("http://localhost:%d/metrics", port)).Output()
	if err != nil {
		return lm
	}
	_ = json.Unmarshal(out, &lm)
	return lm
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

func itoa(i int) string      { return strconv.Itoa(i) }
func u64toa(i uint64) string { return strconv.FormatUint(i, 10) }

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

	label := "server"
	if len(srvNames) > 0 {
		label = srvNames[0]
	}
	header := []string{
		"timestamp", "timestamp_unix_milli", "elapsed_s",
		"connected_clients", "total_clients", "bytes_sent", "bytes_received",
		"uptime_s",
		"lg_connected_clients",
		"ws_rtt_avg_ms", "ws_rtt_p50_ms", "ws_rtt_p95_ms", "ws_rtt_p99_ms", "ws_rtt_max_ms",
		"ws_jitter_ms", "connection_drops",
	}
	for _, h := range pingTargets {
		header = append(header, fmt.Sprintf("ping_ms_%s", h))
	}
	header = append(header, fmt.Sprintf("cpu_%s", label), fmt.Sprintf("mem_%s", label), fmt.Sprintf("mem_pct_%s", label))
	header = append(header, "migration_event")
	_ = w.Write(header)
	w.Flush()

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

	log.Printf("Collector started: remote=%s remote-user=%s mux=%v ping=%v server-names=%v loadgen=%s interval=%s",
		*remoteDirectIP, *remoteSSHUser, mux != nil, pingTargets, srvNames, *loadgenName, *interval)

	for {
		select {
		case <-ctx.Done():
			log.Println("Collector stopped.")
			return

		case t := <-ticker.C:
			elapsed := t.Sub(startTime).Seconds()

			var sm ServerMetrics
			var cs ContainerStats
			name, pid := findLocalContainer(srvNames)
			if pid != "" {
				sm = fetchMetricsLocal(pid, *metricsPort)
				cs = fetchStatsLocal(name)
			}

			if !metricsValid(&sm) && mux != nil {
				r := fetchRemoteBoth(mux, srvNames, *metricsPort)
				sm = r.Metrics
				cs = r.Stats
			}

			_, loadgenPID := findLocalContainer([]string{*loadgenName})
			lm := fetchLoadgenMetrics(loadgenPID, *loadgenMetricsPort)

			row := []string{
				t.Format(time.RFC3339Nano),
				fmt.Sprintf("%d", t.UnixMilli()),
				fmt.Sprintf("%.3f", elapsed),
				itoa(sm.ConnectedClients), fmt.Sprintf("%d", sm.TotalClients),
				u64toa(sm.BytesSent), u64toa(sm.BytesReceived),
				fmt.Sprintf("%.1f", sm.UptimeSeconds),
				itoa(lm.ConnectedClients),
				fmt.Sprintf("%.3f", lm.AvgRttMs),
				fmt.Sprintf("%.3f", lm.P50RttMs),
				fmt.Sprintf("%.3f", lm.P95RttMs),
				fmt.Sprintf("%.3f", lm.P99RttMs),
				fmt.Sprintf("%.3f", lm.MaxRttMs),
				fmt.Sprintf("%.3f", lm.JitterMs),
				fmt.Sprintf("%d", lm.ConnectionDrops),
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
