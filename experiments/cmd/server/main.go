package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

var (
	listenAddr  = flag.String("signaling-addr", ":8080", "HTTP address for WebSocket + health")
	metricsAddr = flag.String("metrics-addr", ":8081", "HTTP address for metrics")
	dataFPS     = flag.Int("fps", 30, "Data frames per second sent to each client")
)

// quiesced is toggled by SIGUSR2. When true, the writer goroutines skip
// sending data frames, letting the kernel TCP send queue drain before a
// CRIU checkpoint. After restore, cr_hw.sh sends SIGUSR2 again to resume.
var quiesced atomic.Bool

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

type clientMsg struct {
	Seq int   `json:"seq"`
	Ts  int64 `json:"ts"`
}

type echoMsg struct {
	Seq      int   `json:"seq"`
	ClientTs int64 `json:"client_ts"`
	ServerTs int64 `json:"server_ts"`
}

type dataMsg struct {
	Seq     int    `json:"seq"`
	Ts      int64  `json:"ts"`
	Size    int    `json:"size"`
	Padding string `json:"padding,omitempty"`
}

type server struct {
	mu           sync.RWMutex
	clients      map[uint64]*websocket.Conn
	nextClientID uint64
	startTime    time.Time
	totalClients atomic.Int64
	bytesSent    atomic.Uint64
	bytesRecv    atomic.Uint64
	cpu          *cpuTracker
}

func newServer() *server {
	return &server{
		clients:   make(map[uint64]*websocket.Conn),
		startTime: time.Now(),
		cpu:       newCPUTracker(),
	}
}

func (s *server) addClient(conn *websocket.Conn) uint64 {
	s.mu.Lock()
	id := s.nextClientID
	s.nextClientID++
	s.clients[id] = conn
	s.mu.Unlock()
	s.totalClients.Add(1)
	return id
}

func (s *server) removeClient(id uint64) {
	s.mu.Lock()
	delete(s.clients, id)
	s.mu.Unlock()
}

func (s *server) connectedCount() int {
	s.mu.RLock()
	n := len(s.clients)
	s.mu.RUnlock()
	return n
}

func (s *server) handleWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}

	clientID := s.addClient(conn)
	log.Printf("[client-%d] connected", clientID)

	// Echo responses are sent via a channel to avoid blocking the reader.
	// The reader must never block on a write (shared write mutex) because
	// after a CRIU migration the TCP send buffer may fill up while the
	// kernel re-establishes the path.  If the reader blocks, it can't
	// consume incoming ACK-carrying data, creating a deadlock.
	echoCh := make(chan []byte, 64)

	done := make(chan struct{})

	// gorilla/websocket requires serialised writes
	var writeMu sync.Mutex
	writeMsg := func(data []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return conn.WriteMessage(websocket.TextMessage, data)
	}

	// Writer: periodic data frames + echo responses
	go func() {
		frameDuration := time.Second / time.Duration(*dataFPS)
		ticker := time.NewTicker(frameDuration)
		defer ticker.Stop()

		seq := 0
		paddingBuf := make([]byte, 512)
		for i := range paddingBuf {
			paddingBuf[i] = 'x'
		}
		paddingStr := string(paddingBuf)

		for {
			select {
			case <-done:
				return

			case echoData := <-echoCh:
				if err := writeMsg(echoData); err != nil {
					return
				}
				s.bytesSent.Add(uint64(len(echoData)))

			case <-ticker.C:
				if quiesced.Load() {
					continue
				}

				// Drain any pending echo responses before writing data
				// so latency measurements stay fresh.
			drainEchoes:
				for {
					select {
					case echoData := <-echoCh:
						if err := writeMsg(echoData); err != nil {
							return
						}
						s.bytesSent.Add(uint64(len(echoData)))
					default:
						break drainEchoes
					}
				}

				msg := dataMsg{
					Seq:     seq,
					Ts:      time.Now().UnixNano(),
					Size:    512,
					Padding: paddingStr,
				}
				data, _ := json.Marshal(msg)
				if err := writeMsg(data); err != nil {
					return
				}
				s.bytesSent.Add(uint64(len(data)))
				seq++
			}
		}
	}()

	// Reader: echo client pings via channel (never blocks on write)
	for {
		_, raw, err := conn.ReadMessage()
		if err != nil {
			break
		}
		s.bytesRecv.Add(uint64(len(raw)))

		// When quiesced, still read (keeps the connection alive) but
		// don't write echo responses — lets the kernel flush the TCP
		// send buffer completely before the checkpoint.
		if quiesced.Load() {
			continue
		}

		var cm clientMsg
		if err := json.Unmarshal(raw, &cm); err != nil {
			continue
		}

		// Drop stale pings buffered during a CRIU freeze so the backlog
		// doesn't delay fresh echo measurements after restore.
		if cm.Ts > 0 {
			ageMs := float64(time.Now().UnixNano()-cm.Ts) / 1e6
			if ageMs > 1000 {
				continue
			}
		}

		echo := echoMsg{
			Seq:      cm.Seq,
			ClientTs: cm.Ts,
			ServerTs: time.Now().UnixNano(),
		}
		data, _ := json.Marshal(echo)

		// Non-blocking send: if the echo channel is full, drop the
		// echo rather than blocking the reader.  This keeps the reader
		// draining the TCP receive buffer so the kernel can process
		// incoming ACKs, which is critical for TCP recovery after
		// migration.
		select {
		case echoCh <- data:
		default:
			// echo dropped (write backpressure)
		}
	}

	close(done)
	conn.Close()
	s.removeClient(clientID)
	log.Printf("[client-%d] disconnected", clientID)
}

type cpuTracker struct {
	mu          sync.Mutex
	lastUser    uint64
	lastSystem  uint64
	lastWall    time.Time
	cpuPercent  float64
}

func newCPUTracker() *cpuTracker {
	ct := &cpuTracker{lastWall: time.Now()}
	ct.lastUser, ct.lastSystem = readProcCPU()
	return ct
}

func readProcCPU() (user, sys uint64) {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0, 0
	}
	// Fields: pid (comm) state ... field14=utime field15=stime (1-indexed)
	// Skip past the (comm) field which may contain spaces
	i := 0
	for i < len(data) && data[i] != ')' {
		i++
	}
	if i >= len(data) {
		return 0, 0
	}
	fields := string(data[i+2:]) // skip ") "
	var vals []uint64
	var cur uint64
	inNum := false
	for _, b := range fields {
		if b >= '0' && b <= '9' {
			cur = cur*10 + uint64(b-'0')
			inNum = true
		} else if inNum {
			vals = append(vals, cur)
			cur = 0
			inNum = false
			if len(vals) >= 13 {
				break
			}
		}
	}
	if inNum && len(vals) < 13 {
		vals = append(vals, cur)
	}
	// After (comm) state: field index 0=ppid ... 11=utime(idx11) 12=stime(idx12)
	if len(vals) >= 13 {
		return vals[11], vals[12]
	}
	return 0, 0
}

func (ct *cpuTracker) sample() float64 {
	ct.mu.Lock()
	defer ct.mu.Unlock()
	now := time.Now()
	user, sys := readProcCPU()
	dt := now.Sub(ct.lastWall).Seconds()
	if dt > 0 && (user+sys) >= (ct.lastUser+ct.lastSystem) {
		// Clock ticks to seconds: typically 100 ticks/sec on Linux
		ticksUsed := float64((user + sys) - (ct.lastUser + ct.lastSystem))
		ct.cpuPercent = (ticksUsed / 100.0 / dt) * 100.0
	}
	ct.lastUser = user
	ct.lastSystem = sys
	ct.lastWall = now
	return ct.cpuPercent
}

type metricsResponse struct {
	ConnectedClients int     `json:"connected_clients"`
	TotalClients     int64   `json:"total_clients"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
	BytesSent        uint64  `json:"bytes_sent"`
	BytesReceived    uint64  `json:"bytes_received"`
	CPUPercent       float64 `json:"cpu_percent"`
	MemoryMB         float64 `json:"memory_mb"`
}

func (s *server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(metricsResponse{
		ConnectedClients: s.connectedCount(),
		TotalClients:     s.totalClients.Load(),
		UptimeSeconds:    time.Since(s.startTime).Seconds(),
		BytesSent:        s.bytesSent.Load(),
		BytesReceived:    s.bytesRecv.Load(),
		CPUPercent:       s.cpu.sample(),
		MemoryMB:         float64(m.Sys) / 1024 / 1024,
	})
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprint(w, `{"status":"ok"}`)
}

func main() {
	flag.Parse()
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	// SIGUSR2 toggles quiesce mode for pre-checkpoint send-queue drain
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGUSR2)
	go func() {
		for range sigCh {
			prev := quiesced.Load()
			quiesced.Store(!prev)
			if !prev {
				log.Println("SIGUSR2: quiesced — data frames paused (send queue draining)")
			} else {
				log.Println("SIGUSR2: resumed — data frames active")
			}
		}
	}()

	s := newServer()

	sigMux := http.NewServeMux()
	sigMux.HandleFunc("/ws", s.handleWS)
	sigMux.HandleFunc("/health", s.handleHealth)

	metMux := http.NewServeMux()
	metMux.HandleFunc("/metrics", s.handleMetrics)
	metMux.HandleFunc("/health", s.handleHealth)

	log.Printf("Stream server starting — ws=%s  metrics=%s  fps=%d",
		*listenAddr, *metricsAddr, *dataFPS)

	go func() {
		log.Fatal(http.ListenAndServe(*metricsAddr, metMux))
	}()

	log.Fatal(http.ListenAndServe(*listenAddr, sigMux))
}
