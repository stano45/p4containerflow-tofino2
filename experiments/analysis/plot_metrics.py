#!/usr/bin/env python3
"""
plot_metrics.py — Generate analysis charts from a live-migration experiment.

Each plot covers exactly one concern:

  connection_health.png  — Client-side TCP connection continuity (THE key result)
  ws_latency.png         — Application-layer RTT percentiles + jitter (client-side)
  throughput.png         — Server data throughput over time
  ping_rtt.png           — Network-layer ICMP latency
  container_resources.png— Container CPU utilisation
  migration_timing.png   — Phase-level migration breakdown
"""

import argparse
import glob
import os
import re
import sys

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import pandas as pd
    import seaborn as sns
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    print("pip install matplotlib pandas numpy seaborn", file=sys.stderr)
    sys.exit(1)

sns.set_theme(style="whitegrid", context="paper", font_scale=1.15)

PHASE_COLORS = {
    "Checkpoint":    "#4CAF50",
    "Transfer":      "#03A9F4",
    "Restore":       "#FF9800",
    "Switch Update": "#9C27B0",
}

PING_LABELS = {
    "192.168.12.2":   "Server (192.168.12.2)",
    "192.168.12.10":  "VIP (192.168.12.10)",
    "192.168.12.100": "Client (192.168.12.100)",
    "192.168.12.3":   "Target (192.168.12.3)",
}

# ── CLI ───────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Plot experiment metrics")
parser.add_argument("--csv", default="results/metrics.csv")
parser.add_argument("--migration-flag", default="/tmp/migration_event",
                    help="File or directory containing migration_timing*.txt")
parser.add_argument("--output-dir", default="results")
parser.add_argument("--show", action="store_true")


# ── Helpers ───────────────────────────────────────────────────────────────

def _save(fig, output_dir, filename, show):
    """Save figure to disk or show interactively."""
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        print(f"  {path}")
    plt.close(fig)


def _draw_migrations(ax, m_times, label=True):
    """Draw vertical dashed lines at each migration start time."""
    colors = sns.color_palette("bright", max(len(m_times), 1))
    for i, t in enumerate(m_times):
        ax.axvline(t, color=colors[i % len(colors)], ls="--", lw=1, alpha=0.7,
                   label=f"Migration {i+1}" if label else None)


def _col(df, *candidates):
    """Return the first column name from *candidates* that exists in df."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _numeric(df, col):
    """Return a numeric Series for *col*, coercing errors to NaN."""
    return pd.to_numeric(df[col], errors="coerce")


# ── Migration event helpers ───────────────────────────────────────────────

def _load_migration_event(path):
    if not os.path.isfile(path):
        return None
    data = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                data[k.strip()] = v.strip()
    return data or None


def load_all_migration_events(path):
    events = []
    if os.path.isfile(path):
        ev = _load_migration_event(path)
        if ev:
            events.append(ev)
        return events
    if os.path.isdir(path):
        files = sorted(
            glob.glob(os.path.join(path, "migration_timing_*.txt")),
            key=lambda p: (
                int(m.group(1))
                if (m := re.match(r"migration_timing_(\d+)\.txt", os.path.basename(p)))
                else 999
            ),
        )
        for f in files:
            ev = _load_migration_event(f)
            if ev:
                events.append(ev)
        if not events:
            single = _load_migration_event(os.path.join(path, "migration_timing.txt"))
            if single:
                events.append(single)
    return events


def _migration_times_sec(df, events):
    ms_list = []
    for ev in events:
        if ev and "migration_start_ns" in ev:
            ms_list.append(int(ev["migration_start_ns"]) // 1_000_000)
    if not ms_list or "timestamp_unix_milli" not in df.columns:
        return []
    t0 = float(df["timestamp_unix_milli"].iloc[0])
    return [(m - t0) / 1000.0 for m in ms_list]


# ═════════════════════════════════════════════════════════════════════════
#  PLOTS — one function per figure, one concept per figure
# ═════════════════════════════════════════════════════════════════════════


def plot_connection_health(df, m_times, output_dir, show):
    """Client-side connection continuity — the central result.

    Top panel : number of active TCP connections as seen by the load generator.
    Bottom panel: cumulative connection drops reported by the load generator.

    Both metrics come from the client (loadgen), which is always running and
    always reachable.  A flat line at the expected count + zero drops proves
    that migrations are transparent.
    """
    fig, axes = plt.subplots(2, 1, figsize=(12, 5.5), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("Client Connection Health", fontweight="bold")
    t = df["t_sec"]

    # ── Top: connected clients ──
    ax = axes[0]
    # Prefer loadgen-reported count (always available).
    # Fall back to server-reported count with gap masking for old CSVs.
    lg_col = _col(df, "lg_connected_clients")
    srv_col = _col(df, "connected_clients")

    if lg_col:
        vals = _numeric(df, lg_col)
        ax.plot(t, vals, lw=1.5, color=sns.color_palette()[0],
                label="Active connections (client)")
        ax.fill_between(t, 0, vals, alpha=0.10, color=sns.color_palette()[0])
    elif srv_col:
        vals = _numeric(df, srv_col).copy()
        vals[vals == 0] = np.nan  # mask collection gaps
        ax.plot(t, vals, lw=1.2, color=sns.color_palette()[0],
                label="Active connections (server-reported, gaps = unreachable)")
        ax.fill_between(t, 0, vals, alpha=0.10, color=sns.color_palette()[0])

    ax.set_ylabel("Active Connections")
    ax.set_ylim(bottom=0)
    ax.legend(loc="lower left", fontsize=9)
    _draw_migrations(ax, m_times)

    # ── Bottom: cumulative connection drops ──
    ax = axes[1]
    drop_col = _col(df, "connection_drops")
    if drop_col:
        drops = _numeric(df, drop_col)
        ax.plot(t, drops, lw=1.5, color="red", label="Cumulative drops")
        ax.fill_between(t, 0, drops, alpha=0.10, color="red")
    ax.set_ylabel("Connection Drops")
    ax.set_xlabel("Time (s)")
    ymax = max(1, drops.max()) if drop_col else 1
    ax.set_ylim(bottom=0, top=ymax * 1.1)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.legend(loc="upper left", fontsize=9)
    _draw_migrations(ax, m_times, label=False)

    plt.tight_layout()
    _save(fig, output_dir, "connection_health.png", show)


def plot_ws_latency(df, m_times, output_dir, show):
    """Application-layer latency measured by the load generator.

    Top panel : RTT percentiles (P50, P95, P99) on a log scale.
    Bottom panel: inter-packet jitter.
    """
    rtt_specs = [
        ("ws_rtt_p50_ms", "P50 (median)", sns.color_palette()[0], "-",  1.6),
        ("ws_rtt_p95_ms", "P95",          sns.color_palette()[1], "--", 1.3),
        ("ws_rtt_p99_ms", "P99",          sns.color_palette()[3], ":",  1.3),
    ]
    present = [(c, l, clr, ls, lw) for c, l, clr, ls, lw in rtt_specs
               if c in df.columns]
    if not present:
        return

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("Application-Layer Latency (client-measured)", fontweight="bold")
    t = df["t_sec"]

    # ── Top: RTT percentiles (log scale) ──
    ax = axes[0]
    for col, lbl, color, ls, lw in present:
        vals = _numeric(df, col).where(lambda x: x > 0)
        ax.plot(t, vals, color=color, ls=ls, lw=lw, label=lbl, alpha=0.9)

    ax.set_ylabel("RTT (ms)")
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, numticks=10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"{v:g}" if v >= 1 else f"{v:.1f}" if v >= 0.1 else f"{v:.2f}"
    ))
    ax.yaxis.set_minor_locator(
        mticker.LogLocator(base=10, subs=np.arange(2, 10) * 0.1, numticks=50))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax.legend(loc="upper right", fontsize=9, framealpha=0.9)
    _draw_migrations(ax, m_times)

    # ── Bottom: jitter ──
    ax = axes[1]
    jitter_col = _col(df, "ws_jitter_ms")
    if jitter_col:
        jitter = _numeric(df, jitter_col).where(lambda x: x > 0)
        ax.plot(t, jitter, lw=1.2, color="#7B1FA2", label="Jitter", alpha=0.85)
        ax.fill_between(t, 0, jitter, alpha=0.08, color="#7B1FA2")

    ax.set_ylabel("Jitter (ms)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    ax.legend(loc="upper right", fontsize=9)
    _draw_migrations(ax, m_times, label=False)

    plt.tight_layout()
    _save(fig, output_dir, "ws_latency.png", show)


def plot_throughput(df, m_times, output_dir, show):
    """Server data throughput derived from bytes_sent."""
    col = _col(df, "bytes_sent")
    if not col:
        return

    fig, ax = plt.subplots(figsize=(12, 4))
    fig.suptitle("Server Throughput", fontweight="bold")
    t = df["t_sec"]

    raw = _numeric(df, col)
    # During migration the server is unreachable and bytes_sent reads as 0.
    # Treat these as missing so diff() doesn't produce false spikes
    # (e.g. 0 → 9,000,000 would look like 9 MB/s throughput).
    raw = raw.where(raw > 0)
    dt = t.diff().fillna(1).clip(lower=0.1)
    rate_kbs = (raw.diff().clip(lower=0) / dt) / 1024
    rate_smooth = rate_kbs.rolling(3, min_periods=1, center=True).mean()
    # Mask zeros during migration (collection gap, not real)
    rate_plot = rate_smooth.copy()
    rate_plot[rate_plot <= 0] = np.nan

    ax.plot(t, rate_plot, lw=1.2, color=sns.color_palette()[1], label="Throughput")
    ax.fill_between(t, 0, rate_plot, alpha=0.12, color=sns.color_palette()[1])
    ax.set_ylabel("Throughput (KB/s)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right", fontsize=9)
    _draw_migrations(ax, m_times)

    plt.tight_layout()
    _save(fig, output_dir, "throughput.png", show)


def plot_ping_rtt(df, m_times, output_dir, show):
    """Network-layer ICMP ping latency to each target host."""
    rtt_cols = [c for c in df.columns
                if c.startswith("ping_rtt_ms_") or c.startswith("ping_ms_")]
    if not rtt_cols:
        return

    valid, unreachable = [], []
    for col in rtt_cols:
        ip = col.replace("ping_rtt_ms_", "").replace("ping_ms_", "").replace("_", ".")
        vals = _numeric(df, col)
        if (vals >= 0).any():
            valid.append((col, ip))
        else:
            unreachable.append(ip)

    if not valid:
        print("  ping_rtt: all hosts unreachable, skipping")
        return

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.suptitle("Network Latency (ICMP Ping)", fontweight="bold")
    t = df["t_sec"]

    palette = sns.color_palette("deep", len(valid))
    for i, (col, ip) in enumerate(valid):
        label = PING_LABELS.get(ip, ip)
        vals = _numeric(df, col).where(lambda x: x >= 0)
        ax.plot(t, vals, lw=1.2, color=palette[i], label=label)

    ax.set_ylabel("RTT (ms)")
    ax.set_xlabel("Time (s)")
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=9)
    _draw_migrations(ax, m_times)

    if unreachable:
        labels = [PING_LABELS.get(h, h) for h in unreachable]
        ax.text(0.99, 0.01, f"Always unreachable: {', '.join(labels)}",
                transform=ax.transAxes, ha="right", va="bottom",
                fontsize=7.5, color="#888", style="italic")

    plt.tight_layout()
    _save(fig, output_dir, "ping_rtt.png", show)


def plot_container_resources(df, m_times, output_dir, show):
    """Container CPU utilisation over time."""
    cpu_cols = [c for c in df.columns
                if c.endswith("_cpu") or (c.startswith("cpu_") and len(c) > 4)]
    if not cpu_cols:
        return

    fig, ax = plt.subplots(figsize=(12, 4))
    fig.suptitle("Container CPU Utilisation", fontweight="bold")
    t = df["t_sec"]

    palette = sns.color_palette("deep", len(cpu_cols))
    for i, col in enumerate(cpu_cols):
        name = col.replace("container_", "").replace("_cpu", "").replace("cpu_", "")
        cpu = df[col].astype(str).str.rstrip("%").str.strip()
        cpu = pd.to_numeric(cpu, errors="coerce")
        ax.plot(t, cpu, lw=1.2, color=palette[i], label=f"{name}")
        ax.fill_between(t, 0, cpu, alpha=0.10, color=palette[i])

    ax.set_ylabel("CPU %")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right", fontsize=9)
    _draw_migrations(ax, m_times)

    plt.tight_layout()
    _save(fig, output_dir, "container_resources.png", show)


def plot_migration_timing(events, output_dir, show):
    """Migration phase breakdown (checkpoint, transfer, restore, switch)."""
    if not events:
        return

    phases = ["Checkpoint", "Transfer", "Restore", "Switch Update"]
    phase_keys = ["checkpoint_ms", "transfer_ms", "restore_ms", "switch_ms"]

    if len(events) == 1:
        ev = events[0]
        try:
            ttr = int(ev.get("time_to_ready_ms", ev["total_ms"]))
            durations = [int(ev.get(k, 0)) for k in phase_keys]
        except (KeyError, ValueError):
            return

        fig, ax = plt.subplots(figsize=(8, 5))
        colors = [PHASE_COLORS[p] for p in phases]
        bars = ax.barh(phases, durations, color=colors, edgecolor="white", lw=0.5)
        ax.set_xlabel("Duration (ms)")
        ax.set_title(f"Migration Phases \u2014 downtime: {ttr} ms", fontweight="bold")
        for bar, v in zip(bars, durations):
            ax.text(bar.get_width() + 2, bar.get_y() + bar.get_height() / 2,
                    f"{v} ms", va="center", fontsize=10)
    else:
        all_dur = {p: [] for p in phases}
        totals = []
        for ev in events:
            try:
                for p, k in zip(phases, phase_keys):
                    all_dur[p].append(int(ev.get(k, 0)))
                totals.append(int(ev.get("time_to_ready_ms", ev.get("total_ms", 0))))
            except (KeyError, ValueError):
                continue
        if not totals:
            return

        means = [np.mean(all_dur[p]) for p in phases]
        stds = [np.std(all_dur[p]) for p in phases]

        fig, ax = plt.subplots(figsize=(10, 5.5))
        colors = [PHASE_COLORS[p] for p in phases]
        y = np.arange(len(phases))
        bars = ax.barh(y, means, xerr=stds, height=0.6, color=colors,
                       capsize=4, ecolor="#555", edgecolor="white", lw=0.5)
        ax.set_yticks(y)
        ax.set_yticklabels(phases)
        ax.set_xlabel("Duration (ms)")

        p50, p95, p99 = np.percentile(totals, [50, 95, 99])
        ax.set_title(
            f"Migration Phases \u2014 {len(events)} migrations "
            f"(mean downtime: {np.mean(totals):.0f} ms)", fontweight="bold")

        for bar, m, s in zip(bars, means, stds):
            ax.text(bar.get_width() + s + 5, bar.get_y() + bar.get_height() / 2,
                    f"{m:.0f} \u00b1 {s:.0f} ms", va="center", fontsize=9)

        ax.text(0.5, -0.10,
                f"Total downtime:  P50 = {p50:.0f} ms    "
                f"P95 = {p95:.0f} ms    P99 = {p99:.0f} ms",
                transform=ax.transAxes, ha="center", fontsize=10,
                style="italic", color="#333")

    plt.tight_layout()
    _save(fig, output_dir, "migration_timing.png", show)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    events = load_all_migration_events(args.migration_flag)

    if not os.path.exists(args.csv):
        print(f"CSV not found: {args.csv}")
        sys.exit(1)

    df = pd.read_csv(args.csv)
    if df.empty:
        print("CSV is empty.")
        sys.exit(1)

    if "timestamp_unix_milli" in df.columns:
        df["timestamp_unix_milli"] = pd.to_numeric(
            df["timestamp_unix_milli"], errors="coerce")
        t0 = float(df["timestamp_unix_milli"].iloc[0])
        df["t_sec"] = (df["timestamp_unix_milli"] - t0) / 1000.0
    elif "elapsed_s" in df.columns:
        df["t_sec"] = pd.to_numeric(df["elapsed_s"], errors="coerce")
    else:
        print("CSV needs 'timestamp_unix_milli' or 'elapsed_s'")
        sys.exit(1)

    m_times = _migration_times_sec(df, events)

    print(f"Loaded {len(df)} rows, duration {df['t_sec'].iloc[-1]:.0f}s, "
          f"{len(events)} migrations")
    for i, ev in enumerate(events):
        t_s = m_times[i] if i < len(m_times) else "?"
        dt = ev.get("time_to_ready_ms", ev.get("total_ms", "?"))
        if isinstance(t_s, float):
            print(f"  #{i+1} at t={t_s:.0f}s  downtime={dt} ms")
        else:
            print(f"  #{i+1}")

    plot_connection_health(df, m_times, args.output_dir, args.show)
    plot_ws_latency(df, m_times, args.output_dir, args.show)
    plot_throughput(df, m_times, args.output_dir, args.show)
    plot_ping_rtt(df, m_times, args.output_dir, args.show)
    plot_container_resources(df, m_times, args.output_dir, args.show)
    plot_migration_timing(events, args.output_dir, args.show)
    print("Done.")


if __name__ == "__main__":
    main()
