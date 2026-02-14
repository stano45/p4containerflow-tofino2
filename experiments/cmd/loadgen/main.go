// Package main implements a WebRTC load generator client.
//
// It connects N concurrent WebRTC peers to the server, receives the video
// stream, and measures per-peer metrics:
//   - Time to first RTP packet (connection latency)
//   - Received bytes per second (throughput)
//   - RTP sequence gaps (packet loss indicator)
//   - Total packets received
//
// The loadgen retries connections at startup until the server is reachable,
// and automatically reconnects peers that disconnect (e.g. after migration).
// Sending SIGUSR1 forces immediate reconnection of all peers.
//
// Usage:
//
//	loadgen -server http://10.0.1.10:8080 -peers 4 -interval 1s -duration 60s
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/pion/webrtc/v4"
)

// ---------------------------------------------------------------------------
// Flags
// ---------------------------------------------------------------------------

var (
	serverURL = flag.String("server", "http://localhost:8080", "WebRTC signaling server URL")
	numPeers  = flag.Int("peers", 4, "Number of concurrent WebRTC peers")
	interval  = flag.Duration("interval", time.Second, "Metrics reporting interval")
	duration  = flag.Duration("duration", 0, "Test duration (0 = until interrupted)")
	rampUp    = flag.Duration("ramp-up", 200*time.Millisecond, "Delay between connecting each peer")
)

// ---------------------------------------------------------------------------
// Per-peer metrics
// ---------------------------------------------------------------------------

type peerMetrics struct {
	PeerID             int     `json:"peer_id"`
	TimestampUnixMilli int64   `json:"timestamp_unix_milli"`
	BytesReceived      uint64  `json:"bytes_received"`
	PacketsReceived    uint64  `json:"packets_received"`
	SequenceGaps       uint64  `json:"sequence_gaps"`
	Connected          bool    `json:"connected"`
	FirstPacketMs      int64   `json:"first_packet_ms,omitempty"`
	BytesPerSecond     float64 `json:"bytes_per_second"`
}

type peer struct {
	id    int
	pc    *webrtc.PeerConnection
	start time.Time

	bytesReceived   atomic.Uint64
	packetsReceived atomic.Uint64
	sequenceGaps    atomic.Uint64
	connected       atomic.Bool
	firstPacketMs   atomic.Int64

	prevBytes uint64
	prevTime  time.Time

	lastSeqNum uint16
	seqInited  bool
}

// ---------------------------------------------------------------------------
// Connect a single peer
// ---------------------------------------------------------------------------

func connectPeer(id int, serverURL string) (*peer, error) {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		return nil, fmt.Errorf("create peer connection: %w", err)
	}

	p := &peer{
		id:       id,
		pc:       pc,
		start:    time.Now(),
		prevTime: time.Now(),
	}

	if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeVideo, webrtc.RTPTransceiverInit{
		Direction: webrtc.RTPTransceiverDirectionRecvonly,
	}); err != nil {
		pc.Close()
		return nil, fmt.Errorf("add transceiver: %w", err)
	}

	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("[peer-%d] got track: %s (codec=%s)", id, track.ID(), track.Codec().MimeType)
		buf := make([]byte, 1500)
		for {
			n, _, err := track.Read(buf)
			if err != nil {
				return
			}
			p.bytesReceived.Add(uint64(n))
			pktNum := p.packetsReceived.Add(1)
			if pktNum == 1 {
				p.firstPacketMs.Store(time.Since(p.start).Milliseconds())
			}
		}
	})

	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		log.Printf("[peer-%d] state=%s", id, state.String())
		switch state {
		case webrtc.PeerConnectionStateConnected:
			p.connected.Store(true)
		case webrtc.PeerConnectionStateDisconnected,
			webrtc.PeerConnectionStateFailed,
			webrtc.PeerConnectionStateClosed:
			p.connected.Store(false)
		}
	})

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		pc.Close()
		return nil, fmt.Errorf("create offer: %w", err)
	}

	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		pc.Close()
		return nil, fmt.Errorf("set local desc: %w", err)
	}
	<-gatherComplete

	offerJSON, _ := json.Marshal(pc.LocalDescription())
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post(serverURL+"/offer", "application/json", bytes.NewReader(offerJSON))
	if err != nil {
		pc.Close()
		return nil, fmt.Errorf("POST /offer: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		pc.Close()
		return nil, fmt.Errorf("POST /offer returned %d", resp.StatusCode)
	}

	var answer webrtc.SessionDescription
	if err := json.NewDecoder(resp.Body).Decode(&answer); err != nil {
		pc.Close()
		return nil, fmt.Errorf("decode answer: %w", err)
	}

	if err := pc.SetRemoteDescription(answer); err != nil {
		pc.Close()
		return nil, fmt.Errorf("set remote desc: %w", err)
	}

	return p, nil
}

// connectPeerWithRetry keeps retrying until success or context cancelled.
func connectPeerWithRetry(ctx context.Context, id int, serverURL string) *peer {
	backoff := 500 * time.Millisecond
	maxBackoff := 3 * time.Second
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		p, err := connectPeer(id, serverURL)
		if err == nil {
			return p
		}
		log.Printf("[peer-%d] connect failed: %v (retrying in %s)", id, err, backoff)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		backoff = backoff * 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

// snapshot returns current metrics and resets the per-interval counters.
func (p *peer) snapshot() peerMetrics {
	now := time.Now()
	totalBytes := p.bytesReceived.Load()
	dt := now.Sub(p.prevTime).Seconds()

	var bps float64
	if dt > 0 {
		bps = float64(totalBytes-p.prevBytes) / dt
	}

	m := peerMetrics{
		PeerID:             p.id,
		TimestampUnixMilli: now.UnixMilli(),
		BytesReceived:      totalBytes,
		PacketsReceived:    p.packetsReceived.Load(),
		SequenceGaps:       p.sequenceGaps.Load(),
		Connected:          p.connected.Load(),
		BytesPerSecond:     bps,
	}

	if fp := p.firstPacketMs.Load(); fp > 0 {
		m.FirstPacketMs = fp
	}

	p.prevBytes = totalBytes
	p.prevTime = now

	return m
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	flag.Parse()
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	log.Printf("Load generator: server=%s peers=%d interval=%s duration=%s",
		*serverURL, *numPeers, *interval, *duration)

	enc := json.NewEncoder(os.Stdout)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Signal handling: SIGINT/SIGTERM to quit
	quitCh := make(chan os.Signal, 1)
	signal.Notify(quitCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-quitCh
		log.Println("Shutting down...")
		cancel()
	}()

	// SIGUSR1 forces immediate reconnection of all peers
	forceReconnect := make(chan struct{}, 1)
	usr1Ch := make(chan os.Signal, 1)
	signal.Notify(usr1Ch, syscall.SIGUSR1)
	go func() {
		for range usr1Ch {
			log.Println("SIGUSR1 received — forcing reconnection of all peers")
			select {
			case forceReconnect <- struct{}{}:
			default:
			}
		}
	}()

	peers := make([]*peer, *numPeers)
	var mu sync.Mutex

	// Connect peers with retry (blocks until server reachable or cancelled)
	for i := 0; i < *numPeers; i++ {
		p := connectPeerWithRetry(ctx, i, *serverURL)
		if p == nil {
			break // cancelled
		}
		mu.Lock()
		peers[i] = p
		mu.Unlock()
		log.Printf("[peer-%d] connected", i)
		if i < *numPeers-1 {
			time.Sleep(*rampUp)
		}
	}
	connectedCount := 0
	for _, p := range peers {
		if p != nil {
			connectedCount++
		}
	}
	log.Printf("Connected %d / %d peers", connectedCount, *numPeers)

	// reconnectAll closes all peers and reconnects them. Called from
	// the reconnector goroutine when SIGUSR1 is received or health
	// check detects the server moved.
	reconnectAll := func() {
		mu.Lock()
		// Close all existing peer connections
		for i, p := range peers {
			if p != nil {
				p.pc.Close()
				peers[i] = nil
			}
		}
		mu.Unlock()

		// Reconnect each peer (not holding the lock)
		for i := 0; i < *numPeers; i++ {
			newP := connectPeerWithRetry(ctx, i, *serverURL)
			if newP == nil {
				break // context cancelled
			}
			mu.Lock()
			peers[i] = newP
			mu.Unlock()
			log.Printf("[peer-%d] reconnected", i)
			if i < *numPeers-1 {
				time.Sleep(*rampUp)
			}
		}
	}

	// Reconnector goroutine: responds to SIGUSR1 or health-check failures
	go func() {
		healthClient := &http.Client{Timeout: 2 * time.Second}
		healthTicker := time.NewTicker(2 * time.Second)
		defer healthTicker.Stop()

		for {
			select {
			case <-ctx.Done():
				return

			case <-forceReconnect:
				log.Println("Reconnecting all peers (forced)")
				reconnectAll()

			case <-healthTicker.C:
				// Check if any peer is connected
				mu.Lock()
				anyConnected := false
				for _, p := range peers {
					if p != nil && p.connected.Load() {
						anyConnected = true
						break
					}
				}
				mu.Unlock()

				if anyConnected {
					// Also verify the server is still reachable
					resp, err := healthClient.Get(*serverURL + "/health")
					if err == nil {
						resp.Body.Close()
						if resp.StatusCode == http.StatusOK {
							continue // healthy
						}
					}
					// Server unreachable while peers think they're connected
					log.Printf("Health check failed — server unreachable, reconnecting")
					reconnectAll()
				} else {
					// No peers connected — check if any peer objects exist
					mu.Lock()
					hasPeers := false
					for _, p := range peers {
						if p != nil {
							hasPeers = true
							break
						}
					}
					mu.Unlock()
					if hasPeers {
						// Peers exist but none connected — try reconnection
						log.Printf("No peers connected — attempting reconnection")
						reconnectAll()
					}
				}
			}
		}
	}()

	// Metrics reporting loop
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	var durationCh <-chan time.Time
	if *duration > 0 {
		durationCh = time.After(*duration)
	}

	for {
		select {
		case <-ctx.Done():
			goto cleanup
		case <-ticker.C:
			mu.Lock()
			for _, p := range peers {
				if p != nil {
					m := p.snapshot()
					enc.Encode(m)
				}
			}
			mu.Unlock()
		case <-durationCh:
			log.Printf("Duration reached, shutting down")
			goto cleanup
		}
	}

cleanup:
	mu.Lock()
	for _, p := range peers {
		if p != nil {
			p.pc.Close()
		}
	}
	mu.Unlock()
	log.Printf("Load generator finished")
}
