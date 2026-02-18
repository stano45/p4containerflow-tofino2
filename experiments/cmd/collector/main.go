package main

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

var (
	serverMetricsURL = flag.String("server-metrics-url", "", "HTTP URL for server /metrics")
	loadgenURL       = flag.String("loadgen-url", "", "HTTP URL for loadgen /metrics")
	migrationFlg     = flag.String("migration-flag", "/tmp/collector_migration_flag", "File whose presence marks a migration event")
	outputFile       = flag.String("output", "metrics.csv", "CSV output path")
	interval         = flag.Duration("interval", 1*time.Second, "Collection interval")

	httpClient = &http.Client{Timeout: 2 * time.Second}
)

type ServerMetrics struct {
	ConnectedClients int     `json:"connected_clients"`
	TotalClients     int64   `json:"total_clients"`
	BytesSent        uint64  `json:"bytes_sent"`
	BytesReceived    uint64  `json:"bytes_received"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
	CPUPercent       float64 `json:"cpu_percent"`
	MemoryMB         float64 `json:"memory_mb"`
}

type LoadgenMetrics struct {
	ConnectedClients int     `json:"connected_clients"`
	AvgRttMs         float64 `json:"avg_rtt_ms"`
	P50RttMs         float64 `json:"p50_rtt_ms"`
	P95RttMs         float64 `json:"p95_rtt_ms"`
	P99RttMs         float64 `json:"p99_rtt_ms"`
	MaxRttMs         float64 `json:"max_rtt_ms"`
	JitterMs         float64 `json:"jitter_ms"`
	ConnectionDrops  int64   `json:"connection_drops"`
}

func fetchJSON[T any](url string) T {
	var v T
	resp, err := httpClient.Get(url)
	if err != nil {
		return v
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return v
	}
	_ = json.Unmarshal(body, &v)
	return v
}

func main() {
	flag.Parse()
	if *serverMetricsURL == "" || *loadgenURL == "" {
		log.Fatal("-server-metrics-url and -loadgen-url are required")
	}

	f, err := os.Create(*outputFile)
	if err != nil {
		log.Fatalf("Cannot create output file: %v", err)
	}
	defer f.Close()
	w := csv.NewWriter(f)
	defer w.Flush()

	header := []string{
		"timestamp", "timestamp_unix_milli", "elapsed_s",
		"connected_clients", "total_clients", "bytes_sent", "bytes_received", "uptime_s",
		"lg_connected_clients",
		"ws_rtt_avg_ms", "ws_rtt_p50_ms", "ws_rtt_p95_ms", "ws_rtt_p99_ms", "ws_rtt_max_ms",
		"ws_jitter_ms", "connection_drops",
		"cpu_percent", "memory_mb",
		"migration_event",
	}
	_ = w.Write(header)
	w.Flush()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sigCh; log.Println("Shutting down..."); cancel() }()

	startTime := time.Now()
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	log.Printf("Collector: server=%s loadgen=%s interval=%s", *serverMetricsURL, *loadgenURL, *interval)

	for {
		select {
		case <-ctx.Done():
			log.Println("Collector stopped.")
			return
		case t := <-ticker.C:
			sm := fetchJSON[ServerMetrics](*serverMetricsURL + "/metrics")
			lm := fetchJSON[LoadgenMetrics](*loadgenURL + "/metrics")

			migEvent := "0"
			if _, err := os.Stat(*migrationFlg); err == nil {
				_ = os.Remove(*migrationFlg)
				migEvent = "1"
				log.Println("Migration event detected")
			}

			row := []string{
				t.Format(time.RFC3339Nano),
				fmt.Sprintf("%d", t.UnixMilli()),
				fmt.Sprintf("%.3f", t.Sub(startTime).Seconds()),
				strconv.Itoa(sm.ConnectedClients), fmt.Sprintf("%d", sm.TotalClients),
				strconv.FormatUint(sm.BytesSent, 10), strconv.FormatUint(sm.BytesReceived, 10),
				fmt.Sprintf("%.1f", sm.UptimeSeconds),
				strconv.Itoa(lm.ConnectedClients),
				fmt.Sprintf("%.3f", lm.AvgRttMs),
				fmt.Sprintf("%.3f", lm.P50RttMs),
				fmt.Sprintf("%.3f", lm.P95RttMs),
				fmt.Sprintf("%.3f", lm.P99RttMs),
				fmt.Sprintf("%.3f", lm.MaxRttMs),
				fmt.Sprintf("%.3f", lm.JitterMs),
				fmt.Sprintf("%d", lm.ConnectionDrops),
				fmt.Sprintf("%.2f", sm.CPUPercent),
				fmt.Sprintf("%.2f", sm.MemoryMB),
				migEvent,
			}
			_ = w.Write(row)
			w.Flush()
		}
	}
}
