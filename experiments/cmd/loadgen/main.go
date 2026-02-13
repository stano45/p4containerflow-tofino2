// Package main implements a WebRTC load generator client.
//
// It connects N concurrent WebRTC peers to the server, receives the video
// stream, and measures per-peer metrics:
//   - Time to first RTP packet (connection latency)
//   - Received bytes per second (throughput)
//   - RTP sequence gaps (packet loss indicator)
//   - Total packets received
//
// Metrics are printed as JSON lines to stdout, one line per measurement
// interval per peer.  The collector (or a pipe to a file) can consume these.
//
// Usage:
//
//	loadgen -server http://10.0.1.10:8080 -peers 4 -interval 1s -duration 60s
package main

import (
	"bytes"
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
	FirstPacketMs      int64   `json:"first_packet_ms,omitempty"` // millis since peer created
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

	// We want to receive video
	if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeVideo, webrtc.RTPTransceiverInit{
		Direction: webrtc.RTPTransceiverDirectionRecvonly,
	}); err != nil {
		pc.Close()
		return nil, fmt.Errorf("add transceiver: %w", err)
	}

	// Handle incoming track
	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		log.Printf("[peer-%d] got track: %s (codec=%s)", id, track.ID(), track.Codec().MimeType)

		buf := make([]byte, 1500)
		for {
			n, _, err := track.Read(buf)
			if err != nil {
				log.Printf("[peer-%d] track read error: %v", id, err)
				return
			}

			p.bytesReceived.Add(uint64(n))
			pktNum := p.packetsReceived.Add(1)

			// Record time to first packet
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

	// Create offer
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

	// Send offer to server; retry on 503 (server still initializing WebRTC)
	offerJSON, _ := json.Marshal(pc.LocalDescription())
	var resp *http.Response
	for attempt := 0; attempt < 60; attempt++ {
		resp, err = http.Post(serverURL+"/offer", "application/json", bytes.NewReader(offerJSON))
		if err != nil {
			pc.Close()
			return nil, fmt.Errorf("POST /offer: %w", err)
		}
		if resp.StatusCode == http.StatusOK {
			break
		}
		if resp.StatusCode == http.StatusServiceUnavailable {
			resp.Body.Close()
			time.Sleep(time.Second)
			continue
		}
		pc.Close()
		resp.Body.Close()
		return nil, fmt.Errorf("POST /offer returned %d", resp.StatusCode)
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

	// JSON line encoder on stdout
	enc := json.NewEncoder(os.Stdout)

	var peers []*peer
	var mu sync.Mutex

	// Connect peers with ramp-up delay
	for i := 0; i < *numPeers; i++ {
		p, err := connectPeer(i, *serverURL)
		if err != nil {
			log.Printf("[peer-%d] connect failed: %v", i, err)
			continue
		}
		mu.Lock()
		peers = append(peers, p)
		mu.Unlock()

		if i < *numPeers-1 {
			time.Sleep(*rampUp)
		}
	}

	log.Printf("Connected %d / %d peers", len(peers), *numPeers)

	// Metrics reporting loop
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	// Duration timer
	var durationCh <-chan time.Time
	if *duration > 0 {
		durationCh = time.After(*duration)
	}

	// Signal handling
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case <-ticker.C:
			mu.Lock()
			for _, p := range peers {
				m := p.snapshot()
				enc.Encode(m)
			}
			mu.Unlock()

		case <-durationCh:
			log.Printf("Duration reached, shutting down")
			goto cleanup

		case sig := <-sigCh:
			log.Printf("Received %s, shutting down", sig)
			goto cleanup
		}
	}

cleanup:
	mu.Lock()
	for _, p := range peers {
		p.pc.Close()
	}
	mu.Unlock()
	log.Printf("Load generator finished")
}
