package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

var (
	serverURL   = flag.String("server", "http://localhost:8080", "Server base URL")
	numConns    = flag.Int("connections", 4, "Number of concurrent WebSocket connections")
	pingMs      = flag.Int("ping-interval-ms", 100, "Ping interval in milliseconds")
	reportIval  = flag.Duration("interval", time.Second, "Metrics reporting interval (stdout)")
	testDur     = flag.Duration("duration", 0, "Test duration (0 = until interrupted)")
	metricsPort = flag.Int("metrics-port", 9090, "HTTP port for /metrics endpoint")
	rampUp      = flag.Duration("ramp-up", 200*time.Millisecond, "Delay between connecting each peer")
)

type conn struct {
	id  int
	ws  *websocket.Conn
	mu  sync.Mutex
	seq int

	bytesRecv atomic.Uint64
	bytesSent atomic.Uint64
	msgsRecv  atomic.Uint64
	msgsSent  atomic.Uint64
	connected atomic.Bool

	rttMu      sync.Mutex
	rttSamples []float64
	lastRTT    float64
	jitterSum  float64
	jitterN    int
}

func (c *conn) sendPing() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.ws == nil {
		return fmt.Errorf("not connected")
	}

	msg := struct {
		Seq int   `json:"seq"`
		Ts  int64 `json:"ts"`
	}{
		Seq: c.seq,
		Ts:  time.Now().UnixNano(),
	}
	c.seq++

	data, _ := json.Marshal(msg)
	if err := c.ws.WriteMessage(websocket.TextMessage, data); err != nil {
		return err
	}
	c.bytesSent.Add(uint64(len(data)))
	c.msgsSent.Add(1)
	return nil
}

type aggregatedMetrics struct {
	ConnectedClients int     `json:"connected_clients"`
	TotalClients     int     `json:"total_clients"`
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

var (
	conns           []*conn
	connsMu         sync.RWMutex
	connectionDrops atomic.Int64
)

func computeMetrics() aggregatedMetrics {
	connsMu.RLock()
	defer connsMu.RUnlock()

	m := aggregatedMetrics{
		TotalClients:    len(conns),
		ConnectionDrops: connectionDrops.Load(),
	}

	var allRTT []float64
	var totalJitter float64
	var jitterCount int
	for _, c := range conns {
		if c.connected.Load() {
			m.ConnectedClients++
		}
		m.BytesSent += c.bytesSent.Load()
		m.BytesReceived += c.bytesRecv.Load()

		c.rttMu.Lock()
		allRTT = append(allRTT, c.rttSamples...)
		totalJitter += c.jitterSum
		jitterCount += c.jitterN
		c.rttMu.Unlock()
	}

	if jitterCount > 0 {
		m.JitterMs = totalJitter / float64(jitterCount)
	}

	if len(allRTT) > 0 {
		sort.Float64s(allRTT)
		var sum float64
		for _, v := range allRTT {
			sum += v
		}
		m.AvgRttMs = sum / float64(len(allRTT))
		m.P50RttMs = percentile(allRTT, 50)
		m.P95RttMs = percentile(allRTT, 95)
		m.P99RttMs = percentile(allRTT, 99)
		m.MaxRttMs = allRTT[len(allRTT)-1]
	}

	return m
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := (p / 100.0) * float64(len(sorted)-1)
	lower := int(math.Floor(idx))
	upper := int(math.Ceil(idx))
	if lower == upper || upper >= len(sorted) {
		return sorted[lower]
	}
	frac := idx - float64(lower)
	return sorted[lower]*(1-frac) + sorted[upper]*frac
}

func connectWS(ctx context.Context, id int, serverURL string) (*conn, error) {
	wsURL := "ws" + serverURL[4:] + "/ws"
	dialer := websocket.Dialer{
		HandshakeTimeout: 5 * time.Second,
	}
	ws, _, err := dialer.DialContext(ctx, wsURL, nil)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", wsURL, err)
	}

	c := &conn{
		id: id,
		ws: ws,
	}
	c.connected.Store(true)
	return c, nil
}

func connectWithRetry(ctx context.Context, id int, serverURL string) *conn {
	backoff := 500 * time.Millisecond
	maxBackoff := 3 * time.Second
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		c, err := connectWS(ctx, id, serverURL)
		if err == nil {
			return c
		}
		log.Printf("[conn-%d] connect failed: %v (retrying in %s)", id, err, backoff)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

func readLoop(ctx context.Context, c *conn) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		_, raw, err := c.ws.ReadMessage()
		if err != nil {
			if c.connected.Load() {
				c.connected.Store(false)
				connectionDrops.Add(1)
				log.Printf("[conn-%d] disconnected: %v", c.id, err)
			}
			return
		}
		c.bytesRecv.Add(uint64(len(raw)))
		c.msgsRecv.Add(1)

		var echo struct {
			Seq      int   `json:"seq"`
			ClientTs int64 `json:"client_ts"`
			ServerTs int64 `json:"server_ts"`
		}
		if err := json.Unmarshal(raw, &echo); err == nil && echo.ClientTs > 0 {
			rtt := float64(time.Now().UnixNano()-echo.ClientTs) / 1e6
			if rtt >= 0 && rtt < 60000 {
				c.rttMu.Lock()
				if c.lastRTT > 0 {
					c.jitterSum += math.Abs(rtt - c.lastRTT)
					c.jitterN++
				}
				c.lastRTT = rtt
				c.rttSamples = append(c.rttSamples, rtt)
				if len(c.rttSamples) > 1000 {
					c.rttSamples = c.rttSamples[len(c.rttSamples)-1000:]
				}
				c.rttMu.Unlock()
			}
		}
	}
}

func pingLoop(ctx context.Context, c *conn) {
	interval := time.Duration(*pingMs) * time.Millisecond
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if !c.connected.Load() {
				return
			}
			if err := c.sendPing(); err != nil {
				if c.connected.Load() {
					c.connected.Store(false)
					connectionDrops.Add(1)
					log.Printf("[conn-%d] ping failed: %v", c.id, err)
				}
				return
			}
		}
	}
}

type peerMetrics struct {
	PeerID             int     `json:"peer_id"`
	TimestampUnixMilli int64   `json:"timestamp_unix_milli"`
	BytesReceived      uint64  `json:"bytes_received"`
	PacketsReceived    uint64  `json:"packets_received"`
	Connected          bool    `json:"connected"`
	BytesPerSecond     float64 `json:"bytes_per_second"`
	RttMs              float64 `json:"rtt_ms"`
}

func snapshotConn(c *conn, prevBytes *uint64, prevTime *time.Time) peerMetrics {
	now := time.Now()
	totalBytes := c.bytesRecv.Load()
	dt := now.Sub(*prevTime).Seconds()

	var bps float64
	if dt > 0 {
		bps = float64(totalBytes-*prevBytes) / dt
	}

	c.rttMu.Lock()
	rtt := c.lastRTT
	c.rttMu.Unlock()

	m := peerMetrics{
		PeerID:             c.id,
		TimestampUnixMilli: now.UnixMilli(),
		BytesReceived:      totalBytes,
		PacketsReceived:    c.msgsRecv.Load(),
		Connected:          c.connected.Load(),
		BytesPerSecond:     bps,
		RttMs:              rtt,
	}

	*prevBytes = totalBytes
	*prevTime = now
	return m
}

func main() {
	flag.Parse()
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	log.Printf("Load generator: server=%s connections=%d ping=%dms interval=%s",
		*serverURL, *numConns, *pingMs, *reportIval)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	quitCh := make(chan os.Signal, 1)
	signal.Notify(quitCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-quitCh
		log.Println("Shutting down...")
		cancel()
	}()

	go func() {
		mux := http.NewServeMux()
		mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(computeMetrics())
		})
		mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"status":"ok"}`)
		})
		addr := fmt.Sprintf(":%d", *metricsPort)
		log.Printf("Metrics endpoint on %s", addr)
		if err := http.ListenAndServe(addr, mux); err != nil {
			log.Printf("Metrics server error: %v", err)
		}
	}()

	conns = make([]*conn, *numConns)
	for i := 0; i < *numConns; i++ {
		c := connectWithRetry(ctx, i, *serverURL)
		if c == nil {
			break
		}
		connsMu.Lock()
		conns[i] = c
		connsMu.Unlock()
		log.Printf("[conn-%d] connected", i)

		go readLoop(ctx, c)
		go pingLoop(ctx, c)

		if i < *numConns-1 {
			time.Sleep(*rampUp)
		}
	}

	connectedCount := 0
	for _, c := range conns {
		if c != nil {
			connectedCount++
		}
	}
	log.Printf("Connected %d / %d clients", connectedCount, *numConns)

	enc := json.NewEncoder(os.Stdout)
	ticker := time.NewTicker(*reportIval)
	defer ticker.Stop()

	prevBytes := make([]uint64, *numConns)
	prevTimes := make([]time.Time, *numConns)
	for i := range prevTimes {
		prevTimes[i] = time.Now()
	}

	var durationCh <-chan time.Time
	if *testDur > 0 {
		durationCh = time.After(*testDur)
	}

	for {
		select {
		case <-ctx.Done():
			goto cleanup
		case <-ticker.C:
			connsMu.RLock()
			for i, c := range conns {
				if c != nil {
					m := snapshotConn(c, &prevBytes[i], &prevTimes[i])
					enc.Encode(m)
				}
			}
			connsMu.RUnlock()
		case <-durationCh:
			log.Printf("Duration reached, shutting down")
			goto cleanup
		}
	}

cleanup:
	connsMu.RLock()
	for _, c := range conns {
		if c != nil && c.ws != nil {
			c.ws.Close()
		}
	}
	connsMu.RUnlock()
	log.Printf("Load generator finished")
}
