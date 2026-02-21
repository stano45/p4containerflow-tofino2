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
    "Pre-transfer":  "#81C784",
    "Transfer":      "#03A9F4",
    "Pre-restore":   "#FFB74D",
    "Restore":       "#FF9800",
    "Switch Update": "#9C27B0",
    "Overhead":      "#BDBDBD",
}

ALL_PHASE_KEYS = [
    "checkpoint_ms", "pre_transfer_ms", "transfer_ms",
    "pre_restore_ms", "restore_ms", "switch_ms",
]
ALL_PHASE_LABELS = [
    "Checkpoint", "Pre-transfer", "Transfer",
    "Pre-restore", "Restore", "Switch Update",
]

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
    """Save figure as both PDF (vector) and high-DPI PNG."""
    if show:
        plt.show()
    else:
        base, _ = os.path.splitext(filename)
        for ext, kwargs in [
            (".pdf", dict(bbox_inches="tight")),
            # (".png", dict(dpi=300, bbox_inches="tight")),
        ]:
            path = os.path.join(output_dir, base + ext)
            fig.savefig(path, **kwargs)
            print(f"  {path}")
    plt.close(fig)


def _short_node(name):
    """Abbreviate a node name for legend labels (e.g. 'lakewood' → 'LW')."""
    abbrevs = {"lakewood": "LW", "loveland": "LV"}
    return abbrevs.get(name, name[:3].upper() if name else "?")


def _draw_migrations(ax, m_times, events=None, label=True):
    """Draw vertical dashed lines at each migration start time."""
    color = "#D32F2F"
    for i, t in enumerate(m_times):
        lbl = f"Migrations (n={len(m_times)})" if (label and i == 0) else None
        ax.axvline(t, color=color, ls="--", lw=0.8, alpha=0.5, label=lbl)


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


def _split_legend(ax, metric_loc="upper left", migration_loc="upper right"):
    """Place a single unified legend (migrations now have only one entry)."""
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, loc=metric_loc, fontsize=8, framealpha=0.9)


# ═════════════════════════════════════════════════════════════════════════
#  PLOTS — one function per figure, one concept per figure
# ═════════════════════════════════════════════════════════════════════════


def _trim_shutdown(df):
    """Drop trailing rows caused by the loadgen being killed at experiment end.

    The loadgen is stopped after the collector, but a race can leave one or two
    final rows with lg_connected_clients == 0 and all RTTs == 0.  These create
    a misleading visual drop.  We trim rows at the tail where the loadgen
    reports 0 connections, provided there were > 0 connections before.
    """
    lg_col = _col(df, "lg_connected_clients")
    if not lg_col:
        return df
    vals = _numeric(df, lg_col)
    if vals.empty or (vals == 0).all():
        return df
    last_nonzero = vals[vals > 0].index[-1]
    return df.loc[:last_nonzero].copy()


def plot_connection_health(df, m_times, output_dir, show, events=None):
    """Client-side connection continuity — the central result.

    Top panel : number of active TCP connections as seen by the load generator.
    Bottom panel: cumulative connection drops reported by the load generator.

    Both metrics come from the client (loadgen), which is always running and
    always reachable.  A flat line at the expected count + zero drops proves
    that migrations are transparent.
    """
    df = _trim_shutdown(df)
    fig, ax = plt.subplots(figsize=(12, 4))
    fig.suptitle("Client Connection Health", fontweight="bold")
    t = df["t_sec"]

    lg_col = _col(df, "lg_connected_clients")
    srv_col = _col(df, "connected_clients")

    if lg_col:
        vals = _numeric(df, lg_col)
        ax.plot(t, vals, lw=1.5, color=sns.color_palette()[0],
                label="Active connections (client)")
        ax.fill_between(t, 0, vals, alpha=0.10, color=sns.color_palette()[0])

        rtt_col = _col(df, "ws_rtt_p50_ms")
        if rtt_col:
            rtt_vals = _numeric(df, rtt_col)
            frozen = (vals > 0) & ((rtt_vals == 0) | rtt_vals.isna())
            ax.fill_between(t, 0, vals.max() * 1.05, where=frozen,
                            alpha=0.12, color="red", label="Service frozen",
                            step="mid")
    elif srv_col:
        vals = _numeric(df, srv_col).copy()
        vals[vals == 0] = np.nan
        ax.plot(t, vals, lw=1.2, color=sns.color_palette()[0],
                label="Active connections (server-reported, gaps = unreachable)")
        ax.fill_between(t, 0, vals, alpha=0.10, color=sns.color_palette()[0])

    ax.set_ylabel("Active Connections")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    _draw_migrations(ax, m_times, events=events)
    _split_legend(ax, metric_loc="lower left")

    plt.tight_layout()
    _save(fig, output_dir, "connection_health.png", show)


def _mask_stale_rtt(df, col):
    """Replace stale (unchanging) RTT values with NaN.

    After a migration the loadgen may stop receiving echo responses.  The
    /metrics endpoint keeps reporting the last computed RTT values, which
    appear as flat lines on the chart.  We detect sequences of >= 5
    consecutive identical values and mask them so the plot shows gaps
    instead of misleading flat lines.
    """
    vals = _numeric(df, col).copy()
    if vals.empty:
        return vals

    # Build runs of identical values
    shifted = vals.shift(1)
    same = (vals == shifted) & vals.notna()
    groups = (~same).cumsum()
    run_len = same.groupby(groups).transform("count") + 1

    # Mask runs of 5+ identical values (keep the first occurrence)
    mask = (run_len >= 5) & same
    vals[mask] = np.nan
    return vals


def plot_ws_latency(df, m_times, output_dir, show, events=None):
    """Application-layer latency measured by the load generator.

    Top panel : RTT percentiles (P50, P95, P99) on a log scale.
    Bottom panel: inter-packet jitter.

    Stale (frozen) RTT values are masked so that the chart shows gaps
    instead of misleading flat lines when the loadgen isn't receiving
    echo responses after a migration.
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

    t = df["t_sec"]

    # ── RTT percentiles (log scale) ──
    fig_rtt, ax = plt.subplots(figsize=(12, 4.5))
    fig_rtt.suptitle("Application-Layer RTT (client-measured)", fontweight="bold")
    for col, lbl, color, ls, lw in present:
        vals = _mask_stale_rtt(df, col)
        vals = vals.where(lambda x: x > 0)
        ax.plot(t, vals, color=color, ls=ls, lw=lw, label=lbl, alpha=0.9)

    ax.set_ylabel("RTT (ms)")
    ax.set_xlabel("Time (s)")
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(mticker.LogLocator(base=10, numticks=10))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda v, _: f"{v:g}" if v >= 1 else f"{v:.1f}" if v >= 0.1 else f"{v:.2f}"
    ))
    ax.yaxis.set_minor_locator(
        mticker.LogLocator(base=10, subs=np.arange(2, 10) * 0.1, numticks=50))
    ax.yaxis.set_minor_formatter(mticker.NullFormatter())
    _draw_migrations(ax, m_times, events=events)

    _split_legend(ax)

    ax.text(0.01, 0.01,
            "Gaps = no echo responses received during that interval",
            transform=ax.transAxes, fontsize=7.5, color="#888", style="italic")

    plt.tight_layout()
    _save(fig_rtt, output_dir, "ws_rtt.png", show)

    # ── Jitter ──
    jitter_col = _col(df, "ws_jitter_ms")
    if jitter_col:
        fig_jit, ax = plt.subplots(figsize=(12, 4.5))
        fig_jit.suptitle("Application-Layer Jitter (client-measured)", fontweight="bold")
        jitter = _mask_stale_rtt(df, jitter_col)
        jitter = jitter.where(lambda x: x > 0)
        ax.plot(t, jitter, lw=1.2, color="#7B1FA2", label="Jitter (mean |RTTₙ − RTTₙ₋₁|)", alpha=0.85)
        ax.set_ylabel("Jitter (ms)")
        ax.set_xlabel("Time (s)")
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(mticker.LogLocator(base=10, numticks=10))
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v:g}" if v >= 1 else f"{v:.2f}" if v >= 0.01 else f"{v:.3f}"
        ))
        ax.yaxis.set_minor_locator(
            mticker.LogLocator(base=10, subs=np.arange(2, 10) * 0.1, numticks=50))
        ax.yaxis.set_minor_formatter(mticker.NullFormatter())
        _draw_migrations(ax, m_times, events=events)
        _split_legend(ax)
        plt.tight_layout()
        _save(fig_jit, output_dir, "ws_jitter.png", show)


def plot_throughput(df, m_times, output_dir, show, events=None):
    """Server data throughput derived from bytes_sent.

    Shows two metrics when bytes_received is available:
    - Server write rate (bytes_sent delta — includes buffer fills)
    - Client receive confirmation (bytes_received delta on server — actual delivery)
    """
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

    ax.plot(t, rate_plot, lw=1.2, color=sns.color_palette()[1], label="Throughput (server writes)")
    ax.fill_between(t, 0, rate_plot, alpha=0.12, color=sns.color_palette()[1])

    # If bytes_received is available, overlay client→server data rate as
    # a proxy for bidirectional health (client sends pings → server receives).
    recv_col = _col(df, "bytes_received")
    if recv_col:
        recv_raw = _numeric(df, recv_col).where(lambda x: x > 0)
        recv_rate = (recv_raw.diff().clip(lower=0) / dt) / 1024
        recv_smooth = recv_rate.rolling(3, min_periods=1, center=True).mean()
        recv_plot = recv_smooth.copy()
        recv_plot[recv_plot <= 0] = np.nan
        ax.plot(t, recv_plot, lw=1.0, color=sns.color_palette()[2],
                ls="--", alpha=0.8, label="Client→Server (server receives)")

    ax.set_ylabel("Throughput (KB/s)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    _draw_migrations(ax, m_times, events=events)
    _split_legend(ax)

    plt.tight_layout()
    _save(fig, output_dir, "throughput.png", show)


def plot_ping_rtt(df, m_times, output_dir, show, events=None):
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
    _draw_migrations(ax, m_times, events=events)
    _split_legend(ax)

    if unreachable:
        labels = [PING_LABELS.get(h, h) for h in unreachable]
        ax.text(0.99, 0.01, f"Always unreachable: {', '.join(labels)}",
                transform=ax.transAxes, ha="right", va="bottom",
                fontsize=7.5, color="#888", style="italic")

    plt.tight_layout()
    _save(fig, output_dir, "ping_rtt.png", show)


def plot_container_resources(df, m_times, output_dir, show, events=None):
    """Container CPU utilisation over time."""
    cpu_cols = [c for c in df.columns
                if c.endswith("_cpu") or (c.startswith("cpu_") and len(c) > 4)
                or c == "cpu_percent"]
    if not cpu_cols:
        return

    has_data = False
    for col in cpu_cols:
        vals = df[col].astype(str).str.rstrip("%").str.strip()
        vals = pd.to_numeric(vals, errors="coerce")
        if vals.notna().any() and (vals > 0).any():
            has_data = True
            break
    if not has_data:
        return

    fig, ax = plt.subplots(figsize=(12, 4))
    fig.suptitle("Container CPU Utilisation", fontweight="bold")
    t = df["t_sec"]

    palette = sns.color_palette("deep", len(cpu_cols))
    for i, col in enumerate(cpu_cols):
        name = col.replace("container_", "").replace("_cpu", "").replace("cpu_", "")
        if name == "percent":
            name = "server"
        cpu = df[col].astype(str).str.rstrip("%").str.strip()
        cpu = pd.to_numeric(cpu, errors="coerce")
        cpu = cpu.where(cpu > 0)
        ax.plot(t, cpu, lw=1.2, color=palette[i], label=f"{name}")
        ax.fill_between(t, 0, cpu, alpha=0.10, color=palette[i])

    ax.set_ylabel("CPU %")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(bottom=0)
    _draw_migrations(ax, m_times, events=events)
    _split_legend(ax)

    plt.tight_layout()
    _save(fig, output_dir, "container_resources.png", show)


def _get_phases(events):
    """Return (phase_labels, phase_keys) filtering out phases that are always 0."""
    labels, keys = [], []
    for lbl, key in zip(ALL_PHASE_LABELS, ALL_PHASE_KEYS):
        if any(int(ev.get(key, 0)) > 0 for ev in events):
            labels.append(lbl)
            keys.append(key)
    return labels, keys


def plot_migration_timing(events, output_dir, show):
    """Migration phase breakdown — all phases that contribute to downtime."""
    if not events:
        return

    phases, phase_keys = _get_phases(events)

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
        x_pad = max(durations) * 0.03
        for bar, v in zip(bars, durations):
            ax.text(bar.get_width() + x_pad, bar.get_y() + bar.get_height() / 2,
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

        x_pad = max(means) * 0.03
        for bar, m, s in zip(bars, means, stds):
            ax.text(bar.get_width() + s + x_pad, bar.get_y() + bar.get_height() / 2,
                    f"{m:.0f} \u00b1 {s:.0f} ms", va="center", fontsize=9)

        ax.text(0.5, -0.18,
                f"Total downtime:  P50 = {p50:.0f} ms    "
                f"P95 = {p95:.0f} ms    P99 = {p99:.0f} ms",
                transform=ax.transAxes, ha="center", fontsize=10,
                style="italic", color="#333")

    plt.tight_layout()
    fig.subplots_adjust(bottom=0.18)
    _save(fig, output_dir, "migration_timing.png", show)


def _build_location_windows(df, events, m_times):
    """Segment the timeline into windows labelled by server location.

    Returns a list of (label, mask) tuples where *mask* is a boolean Series
    selecting rows belonging to that window.  Windows during migration
    (between start and ready) are labelled "Migration".
    """
    t = df["t_sec"]
    windows = []

    # Compute migration end times (start + downtime)
    m_ends = []
    for i, ev in enumerate(events):
        if i < len(m_times):
            dt_s = int(ev.get("time_to_ready_ms", 0)) / 1000.0
            m_ends.append(m_times[i] + dt_s)
        else:
            m_ends.append(m_times[i] if i < len(m_times) else 0)

    # Walk through timeline segments.
    # Skip a buffer after each migration to exclude recovery transients.
    # With stale-echo filtering in the loadgen (RTT cap), the first valid
    # measurement arrives shortly after restore completes.
    RECOVERY_BUFFER = 5  # seconds to skip after migration ends
    prev_end = 0.0
    location = "lakewood"  # server starts here
    for i in range(len(m_times)):
        tgt = events[i].get("target_node", "")

        # Pre-migration stable window
        stable_start = prev_end + RECOVERY_BUFFER if prev_end > 0 else 0
        if m_times[i] > stable_start:
            mask = (t >= stable_start) & (t < m_times[i])
            if mask.any():
                windows.append((location.capitalize(), mask))

        location = tgt
        prev_end = m_ends[i]

    # Final window after last migration
    mask = t >= prev_end + RECOVERY_BUFFER
    if mask.any():
        windows.append((location.capitalize(), mask))

    return windows


def plot_rtt_by_location(df, m_times, events, output_dir, show):
    """Box plot comparing application-layer RTT when the server is on
    different nodes (same-host vs cross-switch) and during migration."""
    if not events or "ws_rtt_p50_ms" not in df.columns:
        return

    windows = _build_location_windows(df, events, m_times)
    if not windows:
        return

    # Collect P50 RTT samples per location category, dropping recovery spikes
    categories = {}
    for label, mask in windows:
        vals = _numeric(df.loc[mask], "ws_rtt_p50_ms").dropna()
        vals = vals[vals > 0]
        if vals.empty:
            continue
        # Remove recovery spikes (> 5x median) that leak past the buffer
        med = vals.median()
        vals = vals[vals <= med * 5]
        if vals.empty:
            continue
        categories.setdefault(label, []).append(vals)

    if not categories:
        return

    # Merge samples per category
    box_data = []
    box_labels = []
    order = ["Lakewood", "Loveland", "Migration"]
    for cat in order:
        if cat in categories:
            combined = pd.concat(categories[cat], ignore_index=True)
            box_data.append(combined.values)
            n = len(combined)
            med = combined.median()
            box_labels.append(f"{cat}\n(n={n}, med={med:.2f} ms)")

    if not box_data:
        return

    fig, ax = plt.subplots(figsize=(7, 5))
    fig.suptitle("Application-Layer RTT by Server Location", fontweight="bold")

    palette = {"Lakewood": "#4CAF50", "Loveland": "#FF9800", "Migration": "#F44336"}
    colors = [palette.get(order[i], "#999") for i in range(len(box_data))]

    bp = ax.boxplot(box_data, tick_labels=box_labels, patch_artist=True,
                    widths=0.5, showfliers=False,
                    medianprops=dict(color="black", lw=1.5))
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    # Overlay individual points (jittered), filtering extreme outliers
    for i, data in enumerate(box_data):
        q1, q3 = np.percentile(data, [25, 75])
        iqr = q3 - q1
        upper_fence = q3 + 3.0 * iqr
        inliers = data[data <= upper_fence]
        jitter = np.random.normal(0, 0.04, len(inliers))
        ax.scatter(np.full(len(inliers), i + 1) + jitter, inliers,
                   alpha=0.15, s=8, color=colors[i], zorder=3)

    ax.set_ylabel("P50 RTT (ms)")
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))

    ax.text(0.5, -0.17,
            "Lakewood = server local to tunnel endpoint (macvlan bridge)    "
            "Loveland = remote host (cross-switch via Tofino P4)",
            transform=ax.transAxes, ha="center", fontsize=8.5,
            color="#555", style="italic")

    plt.tight_layout()
    fig.subplots_adjust(bottom=0.17)
    _save(fig, output_dir, "rtt_by_location.png", show)


def _compute_throughput_rate(df):
    """Derive per-second throughput (KB/s) from cumulative bytes_sent."""
    col = _col(df, "bytes_sent")
    if not col:
        return pd.Series(np.nan, index=df.index)
    raw = _numeric(df, col).where(lambda x: x > 0)
    dt = df["t_sec"].diff().fillna(1).clip(lower=0.1)
    rate = (raw.diff().clip(lower=0) / dt) / 1024
    return rate.rolling(3, min_periods=1, center=True).mean()


def _client_perceived_downtime(df, m_times, events):
    """For each migration, measure the RTT blackout as seen by the client.

    Finds the zero-RTT gap around each migration event: the last sample with
    RTT > 0 before the gap and the first sample with RTT > 0 after the gap.
    This captures the actual service interruption the client experiences,
    independent of the 1 Hz sampling granularity of the collector.

    Returns list of dicts per migration.
    """
    rtt_col = _col(df, "ws_rtt_p50_ms")
    if not rtt_col:
        return []

    t = df["t_sec"].values
    rtt = _numeric(df, rtt_col).values

    results = []
    for i, m_t in enumerate(m_times):
        win = (t >= m_t - 10) & (t <= m_t + 30)
        t_win = t[win]
        rtt_win = rtt[win]

        # Find the zero-RTT gap nearest to the migration timestamp.
        # The gap starts when RTT drops to 0 and ends when it becomes > 0 again.
        zero_mask = (rtt_win <= 0) | np.isnan(rtt_win)
        nonzero_idx = np.where(~zero_mask)[0]

        if len(nonzero_idx) < 2:
            results.append({"migration": i+1, "client_downtime_s": np.nan,
                            "server_downtime_s": int(events[i].get("time_to_ready_ms", 0)) / 1000.0 if i < len(events) else np.nan})
            continue

        # Find the transition: last nonzero before the gap, first nonzero after
        m_t_local = m_t
        before = nonzero_idx[t_win[nonzero_idx] <= m_t_local + 2]
        after = nonzero_idx[t_win[nonzero_idx] > m_t_local + 2]

        last_before_t = t_win[before[-1]] if len(before) > 0 else np.nan
        first_after_t = t_win[after[0]] if len(after) > 0 else np.nan

        client_dt = first_after_t - last_before_t if np.isfinite(last_before_t) and np.isfinite(first_after_t) else np.nan
        server_dt = int(events[i].get("time_to_ready_ms", 0)) / 1000.0 if i < len(events) else np.nan

        results.append({
            "migration": i + 1,
            "last_valid_before": last_before_t,
            "first_valid_after": first_after_t,
            "client_downtime_s": client_dt,
            "server_downtime_s": server_dt,
        })
    return results


# ═════════════════════════════════════════════════════════════════════════
#  MIGRATION-FOCUSED PLOTS — zoomed analysis of each migration event
# ═════════════════════════════════════════════════════════════════════════


def plot_migration_zoom_grid(df, m_times, events, output_dir, show):
    """Small-multiples grid: RTT zoomed to [-5s, +20s] around each migration.

    All panels share the same y-axis so recovery behaviour can be compared
    across migrations at a glance.
    """
    if not events or "ws_rtt_p50_ms" not in df.columns:
        return

    n = len(m_times)
    ncols = 5
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.2 * ncols, 2.8 * nrows),
                             sharex=True, sharey=True)
    fig.suptitle("RTT Around Each Migration Event", fontweight="bold", fontsize=13)
    axes = np.atleast_2d(axes)

    t = df["t_sec"]
    rtt = _mask_stale_rtt(df, "ws_rtt_p50_ms")

    window_before, window_after = 5, 20

    # Pre-compute a shared y-limit: P95 of all recovery spikes across migrations
    all_peaks = []
    for m_t in m_times:
        mask = (t >= m_t) & (t <= m_t + window_after)
        vals = rtt[mask].dropna()
        if not vals.empty:
            all_peaks.append(vals.max())
    y_max = np.percentile(all_peaks, 95) * 1.15 if all_peaks else 500

    for idx in range(nrows * ncols):
        row, col_idx = divmod(idx, ncols)
        ax = axes[row][col_idx]

        if idx >= n:
            ax.set_visible(False)
            continue

        m_t = m_times[idx]
        ev = events[idx]
        mask = (t >= m_t - window_before) & (t <= m_t + window_after)
        t_rel = t[mask] - m_t

        rtt_win = rtt[mask].copy()
        rtt_plot = rtt_win.where(rtt_win > 0)

        ttr_s = int(ev.get("time_to_ready_ms", 0)) / 1000.0
        ax.axvspan(0, ttr_s, alpha=0.10, color="#E53935", zorder=0)
        ax.plot(t_rel, rtt_plot, lw=1.2, color=sns.color_palette()[0],
                alpha=0.9, zorder=2)
        ax.axvline(0, color="#E53935", ls="--", lw=1.0, alpha=0.7, zorder=1)

        src = _short_node(ev.get("source_node", ""))
        tgt = _short_node(ev.get("target_node", ""))
        ttr_ms = int(ev.get("time_to_ready_ms", 0))
        ax.set_title(f"M{idx+1} {src}\u2192{tgt} ({ttr_ms}ms)", fontsize=8, pad=3)

        ax.tick_params(labelsize=7)
        if row == nrows - 1 or idx + ncols >= n:
            ax.set_xlabel("t \u2212 t_mig (s)", fontsize=7)
        if col_idx == 0:
            ax.set_ylabel("P50 RTT (ms)", fontsize=7)
        ax.set_xlim(-window_before, window_after)
        ax.set_ylim(0, y_max)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    _save(fig, output_dir, "migration_zoom_grid.png", show)


def plot_ensemble_recovery(df, m_times, events, output_dir, show):
    """Ensemble RTT recovery: all migrations aligned at t=0, with mean and CI."""
    if not events or "ws_rtt_p50_ms" not in df.columns:
        return

    t = df["t_sec"]
    rtt = _mask_stale_rtt(df, "ws_rtt_p50_ms")

    window_before, window_after = 5, 25
    t_grid = np.arange(-window_before, window_after + 0.5, 0.5)
    traces = []

    for m_t in m_times:
        mask = (t >= m_t - window_before - 1) & (t <= m_t + window_after + 1)
        t_rel = (t[mask] - m_t).values
        vals = rtt[mask].values

        valid = np.isfinite(vals) & (vals > 0)
        if valid.sum() < 3:
            continue
        interp = np.interp(t_grid, t_rel[valid], vals[valid],
                           left=np.nan, right=np.nan)
        traces.append(interp)

    if not traces:
        return

    traces = np.array(traces)
    mean_trace = np.nanmean(traces, axis=0)
    std_trace = np.nanstd(traces, axis=0)
    p25 = np.nanpercentile(traces, 25, axis=0)
    p75 = np.nanpercentile(traces, 75, axis=0)

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.suptitle("Ensemble RTT Recovery Profile", fontweight="bold")

    for i, trace in enumerate(traces):
        ax.plot(t_grid, trace, lw=0.5, alpha=0.25, color="#90CAF9")

    ax.fill_between(t_grid, p25, p75, alpha=0.25, color=sns.color_palette()[0],
                    label="IQR (P25\u2013P75)")
    ax.plot(t_grid, mean_trace, lw=2.0, color=sns.color_palette()[0],
            label="Mean RTT", zorder=5)

    ax.axvline(0, color="#E53935", ls="--", lw=1.5, label="Migration start")

    mean_ttr = np.mean([int(ev.get("time_to_ready_ms", 0)) for ev in events]) / 1000.0
    ax.axvline(mean_ttr, color="#FF9800", ls=":", lw=1.2,
               label=f"Mean recovery ({mean_ttr:.1f}s)")

    ax.set_xlabel("Time relative to migration start (s)")
    ax.set_ylabel("P50 RTT (ms)")
    ax.set_xlim(-window_before, window_after)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right", fontsize=9, framealpha=0.9)

    ax.text(0.01, 0.97,
            f"n = {len(traces)} migrations aligned at t=0",
            transform=ax.transAxes, fontsize=8, va="top", color="#555",
            style="italic")

    plt.tight_layout()
    _save(fig, output_dir, "ensemble_rtt_recovery.png", show)


def plot_client_vs_server_downtime(df, m_times, events, output_dir, show):
    """Client-perceived vs server-reported downtime comparison."""
    perceived = _client_perceived_downtime(df, m_times, events)
    if not perceived:
        return

    mig_ids = [p["migration"] for p in perceived]
    client_dt = np.array([p["client_downtime_s"] for p in perceived])
    server_dt = np.array([p["server_downtime_s"] for p in perceived])

    valid = np.isfinite(client_dt) & np.isfinite(server_dt)
    if valid.sum() == 0:
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5),
                                    gridspec_kw={"width_ratios": [2, 1]})
    fig.suptitle("Client-Perceived vs Server-Reported Downtime", fontweight="bold")

    x = np.arange(len(mig_ids))
    w = 0.35
    ax1.bar(x - w/2, server_dt, w, label="Server-reported (time_to_ready)",
            color="#FF9800", alpha=0.8, edgecolor="white", lw=0.5)
    ax1.bar(x + w/2, client_dt, w, label="Client-perceived (RTT gap)",
            color=sns.color_palette()[0], alpha=0.8, edgecolor="white", lw=0.5)

    ax1.set_xlabel("Migration #")
    ax1.set_ylabel("Downtime (s)")
    ax1.set_xticks(x[::2])
    ax1.set_xticklabels([f"M{m}" for m in np.array(mig_ids)[::2]], fontsize=8)
    ax1.legend(fontsize=8)
    ax1.set_ylim(bottom=0)

    d_max = max(np.nanmax(client_dt[valid]), np.nanmax(server_dt[valid])) * 1.15
    ax2.scatter(server_dt[valid], client_dt[valid], s=40, alpha=0.7,
                color=sns.color_palette()[0], edgecolor="white", lw=0.5, zorder=3)
    ax2.plot([0, d_max], [0, d_max], ls="--", color="#999", lw=1, label="y = x")
    ax2.set_xlabel("Server-reported (s)")
    ax2.set_ylabel("Client-perceived (s)")
    ax2.set_xlim(0, d_max)
    ax2.set_ylim(0, d_max)
    ax2.set_aspect("equal")
    ax2.legend(fontsize=8)

    delta = client_dt[valid] - server_dt[valid]
    corr = np.corrcoef(server_dt[valid], client_dt[valid])[0, 1]
    ax2.text(0.05, 0.95,
             f"Mean \u0394: {np.mean(delta):+.2f}s\n"
             f"Median \u0394: {np.median(delta):+.2f}s\n"
             f"r = {corr:.3f}",
             transform=ax2.transAxes, fontsize=8, va="top",
             bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#ccc", alpha=0.9))

    fig.text(0.5, -0.02,
             "Client-perceived = time between last and first non-zero P50 RTT around migration "
             "(quantised to collector\u2019s 1 Hz polling interval)",
             ha="center", fontsize=8, color="#666", style="italic")

    plt.tight_layout()
    fig.subplots_adjust(bottom=0.12)
    _save(fig, output_dir, "downtime_comparison.png", show)


def plot_downtime_cdf(df, m_times, events, output_dir, show):
    """Empirical CDF of server-reported downtime (time_to_ready)."""
    if not events:
        return

    server_vals = np.array([int(ev.get("time_to_ready_ms", 0)) for ev in events])
    if len(server_vals) == 0:
        return

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.suptitle("Migration Downtime CDF", fontweight="bold")

    color = "#FF9800"
    sorted_v = np.sort(server_vals)
    cdf = np.arange(1, len(sorted_v) + 1) / len(sorted_v)
    ax.step(sorted_v, cdf, where="post", lw=2.2, color=color,
            label=f"time_to_ready (n={len(sorted_v)})")

    for pct, y_off in zip([50, 95, 99], [0.52, 0.92, 0.97]):
        pval = np.percentile(sorted_v, pct)
        ax.axvline(pval, color=color, ls=":", lw=0.8, alpha=0.5)
        ax.text(pval + 20, y_off,
                f"P{pct} = {pval:.0f} ms", fontsize=9, color=color,
                style="italic")

    ax.set_xlabel("Downtime (ms)")
    ax.set_ylabel("Cumulative Probability")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=9, loc="lower right")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))

    lo = np.min(sorted_v) * 0.92
    hi = np.max(sorted_v) * 1.06
    ax.set_xlim(lo, hi)

    mean_dt = np.mean(sorted_v)
    std_dt = np.std(sorted_v)
    ax.text(0.02, 0.97,
            f"Mean = {mean_dt:.0f} ms  (SD = {std_dt:.0f} ms)",
            transform=ax.transAxes, fontsize=9, va="top", color="#333",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#ccc", alpha=0.9))

    plt.tight_layout()
    _save(fig, output_dir, "downtime_cdf.png", show)


def plot_throughput_recovery(df, m_times, events, output_dir, show):
    """Ensemble throughput around migration events."""
    rate = _compute_throughput_rate(df)
    if rate.isna().all():
        return

    t = df["t_sec"]
    window_before, window_after = 5, 25
    t_grid = np.arange(-window_before, window_after + 0.5, 0.5)
    traces = []

    for m_t in m_times:
        mask = (t >= m_t - window_before - 1) & (t <= m_t + window_after + 1)
        t_rel = (t[mask] - m_t).values
        vals = rate[mask].values

        valid = np.isfinite(vals) & (vals > 0)
        if valid.sum() < 3:
            continue
        interp = np.interp(t_grid, t_rel[valid], vals[valid],
                           left=np.nan, right=np.nan)
        traces.append(interp)

    if not traces:
        return

    traces = np.array(traces)
    mean_trace = np.nanmean(traces, axis=0)
    p25 = np.nanpercentile(traces, 25, axis=0)
    p75 = np.nanpercentile(traces, 75, axis=0)

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.suptitle("Ensemble Throughput Recovery Profile", fontweight="bold")

    for trace in traces:
        ax.plot(t_grid, trace, lw=0.5, alpha=0.2, color="#A5D6A7")

    ax.fill_between(t_grid, p25, p75, alpha=0.25, color=sns.color_palette()[2],
                    label="IQR (P25\u2013P75)")
    ax.plot(t_grid, mean_trace, lw=2.0, color=sns.color_palette()[2],
            label="Mean throughput", zorder=5)
    ax.axvline(0, color="#E53935", ls="--", lw=1.5, label="Migration start")

    mean_ttr = np.mean([int(ev.get("time_to_ready_ms", 0)) for ev in events]) / 1000.0
    ax.axvline(mean_ttr, color="#FF9800", ls=":", lw=1.2,
               label=f"Mean recovery ({mean_ttr:.1f}s)")

    ax.set_xlabel("Time relative to migration start (s)")
    ax.set_ylabel("Throughput (KB/s)")
    ax.set_xlim(-window_before, window_after)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right", fontsize=9, framealpha=0.9)

    ax.text(0.01, 0.97,
            f"n = {len(traces)} migrations aligned at t=0",
            transform=ax.transAxes, fontsize=8, va="top", color="#555",
            style="italic")

    plt.tight_layout()
    _save(fig, output_dir, "ensemble_throughput_recovery.png", show)


def plot_downtime_strip(events, output_dir, show):
    """Per-migration stacked bars and phase variability box plots."""
    if not events or len(events) < 2:
        return

    phase_labels, phase_keys = _get_phases(events)

    rows = []
    for i, ev in enumerate(events):
        try:
            phased_sum = 0
            for pk, pl in zip(phase_keys, phase_labels):
                v = int(ev.get(pk, 0))
                rows.append({"migration": f"M{i+1}", "phase": pl, "ms": v})
                phased_sum += v
            ttr = int(ev.get("time_to_ready_ms", ev.get("total_ms", 0)))
            overhead = max(0, ttr - phased_sum)
            if overhead > 0:
                rows.append({"migration": f"M{i+1}", "phase": "Overhead", "ms": overhead})
        except (KeyError, ValueError):
            continue
    if not rows:
        return

    all_labels = list(phase_labels)
    if any(r["phase"] == "Overhead" for r in rows):
        all_labels.append("Overhead")

    mig_df = pd.DataFrame(rows)
    migrations = sorted(mig_df["migration"].unique(),
                        key=lambda s: int(s[1:]))

    # ── Stacked bar per migration ──
    fig1, ax = plt.subplots(figsize=(max(8, len(migrations) * 0.45), 5))
    fig1.suptitle("Per-Migration Phase Breakdown", fontweight="bold")
    bottoms = np.zeros(len(migrations))
    for phase in all_labels:
        vals = [mig_df[(mig_df["migration"] == m) & (mig_df["phase"] == phase)]["ms"].sum()
                for m in migrations]
        color = PHASE_COLORS.get(phase, "#999")
        ax.bar(migrations, vals, bottom=bottoms, label=phase,
               color=color, edgecolor="white", lw=0.5, width=0.6)
        bottoms += np.array(vals)

    for i, m in enumerate(migrations):
        ev = events[i]
        ttr = int(ev.get("time_to_ready_ms", ev.get("total_ms", 0)))
        ax.text(i, bottoms[i] + 100, f"{ttr/1000:.1f}s",
                ha="center", va="bottom", fontsize=8, fontweight="bold")

    ax.set_ylabel("Duration (ms)")
    ax.set_xlabel("Migration #")
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.legend(loc="upper left", fontsize=8,
              bbox_to_anchor=(1.01, 1), borderaxespad=0)
    ax.set_ylim(bottom=0)
    plt.tight_layout()
    _save(fig1, output_dir, "migration_bars.png", show)

    # ── Box plots: phase variability across migrations ──
    fig2, ax = plt.subplots(figsize=(7, 5))
    fig2.suptitle("Migration Phase Variability", fontweight="bold")
    phase_data = []
    box_labels = []
    for phase in all_labels:
        vals = mig_df[mig_df["phase"] == phase]["ms"].values
        if vals.sum() > 0:
            phase_data.append(vals)
            box_labels.append(phase)

    bp = ax.boxplot(phase_data, tick_labels=box_labels, patch_artist=True,
                    widths=0.5, showfliers=True,
                    medianprops=dict(color="black", lw=1.5))
    for patch, phase in zip(bp["boxes"], box_labels):
        patch.set_facecolor(PHASE_COLORS.get(phase, "#999"))
        patch.set_alpha(0.6)

    for i, (data, phase) in enumerate(zip(phase_data, box_labels)):
        jitter = np.random.normal(0, 0.06, len(data))
        ax.scatter(np.full(len(data), i + 1) + jitter, data,
                   alpha=0.6, s=25, color=PHASE_COLORS.get(phase, "#999"),
                   edgecolor="white", lw=0.3, zorder=3)

    ax.set_ylabel("Duration (ms)")
    ax.tick_params(axis="x", rotation=15)
    plt.tight_layout()
    _save(fig2, output_dir, "phase_variability.png", show)


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

    plot_connection_health(df, m_times, args.output_dir, args.show, events=events)
    plot_ws_latency(df, m_times, args.output_dir, args.show, events=events)
    plot_throughput(df, m_times, args.output_dir, args.show, events=events)
    plot_ping_rtt(df, m_times, args.output_dir, args.show, events=events)
    plot_container_resources(df, m_times, args.output_dir, args.show, events=events)
    plot_migration_timing(events, args.output_dir, args.show)
    plot_rtt_by_location(df, m_times, events, args.output_dir, args.show)
    plot_downtime_strip(events, args.output_dir, args.show)

    # Migration-focused zoom plots
    # plot_migration_zoom_grid(df, m_times, events, args.output_dir, args.show)
    plot_ensemble_recovery(df, m_times, events, args.output_dir, args.show)
    # plot_client_vs_server_downtime(df, m_times, events, args.output_dir, args.show)
    plot_downtime_cdf(None, None, events, args.output_dir, args.show)
    plot_throughput_recovery(df, m_times, events, args.output_dir, args.show)
    print("Done.")


if __name__ == "__main__":
    main()
