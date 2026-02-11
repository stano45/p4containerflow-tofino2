// Package main implements a metrics collector for the WebRTC container
// migration experiment. It periodically polls the server's /metrics endpoint,
// pings target hosts, gathers container stats, and writes everything to CSV.
//
// Multi-node support:
//   - Uses VIP for /metrics so the URL doesn't change after migration
//   - Supports SSH-based podman stats for containers on remote nodes
//   - Watches a migration flag file and records the event in the CSV
package main

import (
	"bufio"
	"context"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
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
	serverMetricsURL = flag.String("server-metrics", "", "Server /metrics URL (use VIP for multi-node)")
	pingHosts        = flag.String("ping-hosts", "", "Comma-separated IPs to ping each interval")
	containers       = flag.String("containers", "", "Comma-separated container names for podman stats")
	sshHost          = flag.String("ssh-host", "", "SSH target for remote podman stats after migration (e.g. stan@loveland)")
	migrationFlag    = flag.String("migration-flag", "/tmp/migration_event", "Migration event flag file path")
	outputFile       = flag.String("output", "results/metrics.csv", "CSV output path")
	interval         = flag.Duration("interval", 1*time.Second, "Collection interval")
)

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

// ServerMetrics represents the JSON response from the WebRTC server /metrics.
type ServerMetrics struct {
	ActivePeers    int     `json:"active_peers"`
	TotalPeers     int     `json:"total_peers"`
	BytesSent      int64   `json:"bytes_sent"`
	BytesReceived  int64   `json:"bytes_received"`
	FramesSent     int64   `json:"frames_sent"`
	KeyframesSent  int64   `json:"keyframes_sent"`
	UptimeSeconds  float64 `json:"uptime_seconds"`
	AvgBitrateBps  float64 `json:"avg_bitrate_bps"`
}

// ContainerStats holds resource usage from podman stats.
type ContainerStats struct {
	CPUPercent string
	MemUsage   string
	MemPercent string
	NetIO      string
	BlockIO    string
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

func main() {
	flag.Parse()

	if *serverMetricsURL == "" {
		log.Fatal("-server-metrics is required")
	}

	// Set up CSV writer
	f, err := os.Create(*outputFile)
	if err != nil {
		log.Fatalf("Cannot create output file: %v", err)
	}
	defer f.Close()

	w := csv.NewWriter(f)
	defer w.Flush()

	// CSV header
	header := []string{
		"timestamp", "elapsed_s",
		// Server metrics
		"active_peers", "total_peers", "bytes_sent", "bytes_received",
		"frames_sent", "keyframes_sent", "uptime_s", "avg_bitrate_bps",
		// Ping RTTs (one column per host)
	}

	pingTargets := splitNonEmpty(*pingHosts, ",")
	for _, h := range pingTargets {
		header = append(header, fmt.Sprintf("ping_ms_%s", h))
	}

	// Container stats columns
	containerNames := splitNonEmpty(*containers, ",")
	for _, c := range containerNames {
		header = append(header, fmt.Sprintf("cpu_%s", c))
		header = append(header, fmt.Sprintf("mem_%s", c))
		header = append(header, fmt.Sprintf("mem_pct_%s", c))
	}

	// Migration event column
	header = append(header, "migration_event")
	_ = w.Write(header)
	w.Flush()

	// Graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("Shutting down collector...")
		cancel()
	}()

	httpClient := &http.Client{Timeout: 3 * time.Second}
	startTime := time.Now()
	migrated := false
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	log.Printf("Collector started: metrics=%s ping=%v containers=%v interval=%s",
		*serverMetricsURL, pingTargets, containerNames, *interval)

	for {
		select {
		case <-ctx.Done():
			log.Println("Collector stopped.")
			return
		case t := <-ticker.C:
			elapsed := t.Sub(startTime).Seconds()
			row := []string{
				t.Format(time.RFC3339Nano),
				fmt.Sprintf("%.3f", elapsed),
			}

			// --- Server metrics ---
			sm := fetchServerMetrics(httpClient, *serverMetricsURL)
			row = append(row,
				itoa(sm.ActivePeers), itoa(sm.TotalPeers),
				i64toa(sm.BytesSent), i64toa(sm.BytesReceived),
				i64toa(sm.FramesSent), i64toa(sm.KeyframesSent),
				fmt.Sprintf("%.1f", sm.UptimeSeconds),
				fmt.Sprintf("%.0f", sm.AvgBitrateBps),
			)

			// --- Ping ---
			for _, host := range pingTargets {
				rtt := pingOnce(host)
				row = append(row, fmt.Sprintf("%.3f", rtt))
			}

			// --- Container stats ---
			// After migration, try remote stats if ssh-host is set
			useSSH := migrated && *sshHost != ""
			for _, cname := range containerNames {
				stats := getContainerStats(cname, useSSH)
				row = append(row, stats.CPUPercent, stats.MemUsage, stats.MemPercent)
			}

			// --- Migration event ---
			migEvent := "0"
			if !migrated && checkMigrationFlag(*migrationFlag) {
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

// ---------------------------------------------------------------------------
// Server metrics
// ---------------------------------------------------------------------------

func fetchServerMetrics(client *http.Client, url string) ServerMetrics {
	var sm ServerMetrics
	resp, err := client.Get(url)
	if err != nil {
		return sm
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return sm
	}
	_ = json.Unmarshal(body, &sm)
	return sm
}

// ---------------------------------------------------------------------------
// Ping
// ---------------------------------------------------------------------------

func pingOnce(host string) float64 {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "ping", "-c", "1", "-W", "2", host)
	out, err := cmd.Output()
	if err != nil {
		return -1
	}
	// Parse "time=X.XX ms" from ping output
	for _, line := range strings.Split(string(out), "\n") {
		if idx := strings.Index(line, "time="); idx >= 0 {
			s := line[idx+5:]
			if end := strings.Index(s, " "); end > 0 {
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
// Container stats (local or via SSH)
// ---------------------------------------------------------------------------

func getContainerStats(name string, viaSSH bool) ContainerStats {
	var cmd *exec.Cmd
	format := "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"

	if viaSSH && *sshHost != "" {
		cmd = exec.Command("ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
			*sshHost,
			fmt.Sprintf("sudo podman stats --no-stream --format '%s' %s", format, name))
	} else {
		cmd = exec.Command("sudo", "podman", "stats", "--no-stream",
			"--format", format, name)
	}

	out, err := cmd.Output()
	if err != nil {
		return ContainerStats{}
	}

	parts := strings.Split(strings.TrimSpace(string(out)), "|")
	if len(parts) < 3 {
		return ContainerStats{}
	}
	return ContainerStats{
		CPUPercent: strings.TrimSpace(parts[0]),
		MemUsage:   strings.TrimSpace(parts[1]),
		MemPercent: strings.TrimSpace(parts[2]),
	}
}

// ---------------------------------------------------------------------------
// Migration flag file
// ---------------------------------------------------------------------------

func checkMigrationFlag(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func splitNonEmpty(s, sep string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, sep)
	result := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			result = append(result, p)
		}
	}
	return result
}

func itoa(i int) string    { return strconv.Itoa(i) }
func i64toa(i int64) string { return strconv.FormatInt(i, 10) }
