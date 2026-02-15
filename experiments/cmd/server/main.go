package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

var (
	listenAddr = flag.String("signaling-addr", ":8080", "HTTP address for WebSocket + health")
	metricsAddr = flag.String("metrics-addr", ":8081", "HTTP address for metrics")
	dataFPS    = flag.Int("fps", 30, "Data frames per second sent to each client")
)

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
}

func newServer() *server {
	return &server{
		clients:   make(map[uint64]*websocket.Conn),
		startTime: time.Now(),
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

	// gorilla/websocket requires serialised writes
	var writeMu sync.Mutex
	writeMsg := func(data []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		return conn.WriteMessage(websocket.TextMessage, data)
	}

	done := make(chan struct{})

	// Writer: periodic data frames
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
			case <-ticker.C:
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

	// Reader: echo client pings
	for {
		_, raw, err := conn.ReadMessage()
		if err != nil {
			break
		}
		s.bytesRecv.Add(uint64(len(raw)))

		var cm clientMsg
		if err := json.Unmarshal(raw, &cm); err != nil {
			continue
		}

		echo := echoMsg{
			Seq:      cm.Seq,
			ClientTs: cm.Ts,
			ServerTs: time.Now().UnixNano(),
		}
		data, _ := json.Marshal(echo)
		if err := writeMsg(data); err != nil {
			break
		}
		s.bytesSent.Add(uint64(len(data)))
	}

	close(done)
	conn.Close()
	s.removeClient(clientID)
	log.Printf("[client-%d] disconnected", clientID)
}

type metricsResponse struct {
	ConnectedClients int     `json:"connected_clients"`
	TotalClients     int64   `json:"total_clients"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
	BytesSent        uint64  `json:"bytes_sent"`
	BytesReceived    uint64  `json:"bytes_received"`
}

func (s *server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(metricsResponse{
		ConnectedClients: s.connectedCount(),
		TotalClients:     s.totalClients.Load(),
		UptimeSeconds:    time.Since(s.startTime).Seconds(),
		BytesSent:        s.bytesSent.Load(),
		BytesReceived:    s.bytesRecv.Load(),
	})
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprint(w, `{"status":"ok"}`)
}

func main() {
	flag.Parse()
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

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
