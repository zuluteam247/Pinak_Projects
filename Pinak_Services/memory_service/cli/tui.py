from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Static, DataTable, ListView, ListItem, Label, Button
from textual.reactive import reactive
from textual.screen import Screen
from textual.message import Message
from typing import Optional, Dict, Tuple
import sqlite3
import os
import datetime
import json
import numpy as np
import subprocess

# --- CSS with Visual Polish ---
GLOBAL_CSS = """
Screen {
    background: #05070D;
    color: #E5E7EB;
}

Header {
    background: #0B1220;
    color: #E5E7EB;
    dock: top;
    text-style: bold;
}

Footer {
    background: #0B1220;
    color: #9CA3AF;
    dock: bottom;
}

#sidebar {
    dock: left;
    width: 28;
    background: #0A0F1A;
    border-right: solid #111827;
}

#sidebar .brand {
    padding: 1;
    background: #0F172A;
    color: #E5E7EB;
    text-align: center;
    text-style: bold;
    border-bottom: solid #1F2937;
}

#sidebar .nav-title {
    padding: 1 2;
    color: #64748B;
    text-style: bold;
}

ListView.nav-list {
    height: 100%;
}

ListItem.nav-item {
    padding: 1 2;
    color: #A3B0C2;
}

ListItem.nav-item:hover {
    background: #0E1628;
    color: #E5E7EB;
}

ListItem.nav-item.--highlight {
    background: #F97316;
    color: #0B0F17;
    text-style: bold;
    border-left: thick #FDBA74;
}

#main-content {
    padding: 1 2;
    height: 100%;
    background: #0B111E;
}

#topbar {
    height: 4;
    padding: 0 1;
    background: #0F172A;
    border: solid #1E293B;
    margin: 0 0 1 0;
}

#topbar-title {
    color: #F59E0B;
    text-style: bold;
}

#topbar-subtitle {
    color: #94A3B8;
}

#topbar-status {
    color: #22D3EE;
    text-style: bold;
    text-align: right;
}

.stat-card {
    background: #0F172A;
    border: solid #1E293B;
    height: 9;
    margin: 1;
    padding: 1 2;
    width: 1fr;
    border-title-color: #F59E0B;
    border-title-style: bold;
}

.stat-title {
    color: #FDBA74;
    text-style: bold;
    border-bottom: solid #1E293B;
    margin-bottom: 1;
}

.stat-value {
    text-align: center;
    color: #22D3EE;
    text-style: bold;
    margin-top: 1;
}

.panel {
    background: #0F172A;
    border: solid #1E293B;
    padding: 1 2;
}

DataTable {
    background: #0F172A;
    border: solid #1E293B;
    color: #E5E7EB;
}

.section-header {
    color: #F59E0B;
    text-style: bold;
    border-bottom: solid #F59E0B;
    margin-bottom: 1;
}

Button {
    background: #111827;
    color: #E5E7EB;
    border: solid #1E293B;
}

Button.-primary {
    background: #F97316;
    color: #0B0F17;
    border: solid #FDBA74;
    text-style: bold;
}

.toolbar {
    height: 3;
    margin: 0 0 1 0;
}

.muted {
    color: #94A3B8;
}

.mono {
    color: #A3B0C2;
}
"""

class Sidebar(Container):
    def compose(self) -> ComposeResult:
        yield Label("🏹  PINAK OS", classes="brand")
        yield Label("CORE", classes="nav-title")
        yield ListView(
            ListItem(Label("📊  System Mesh"), id="nav-dashboard", classes="nav-item"),
            ListItem(Label("📡  Memory Access"), id="nav-access", classes="nav-item"),
            ListItem(Label("👥  Agent Swarm"), id="nav-agents", classes="nav-item"),
            ListItem(Label("🧷  Client Issues"), id="nav-issues", classes="nav-item"),
            ListItem(Label("🧭  Client Registry"), id="nav-clients", classes="nav-item"),
            ListItem(Label("❤️   Bio-Health"), id="nav-health", classes="nav-item"),
            classes="nav-list",
        )

class DashboardView(Container):
    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Container(
                Static("Synaptic Memories", classes="stat-title"),
                Static("0", id="stat-total-mem", classes="stat-value"),
                classes="stat-card"
            )
            yield Container(
                Static("Active Agents", classes="stat-title"),
                Static("0", id="stat-active-agents", classes="stat-value"),
                classes="stat-card"
            )
            yield Container(
                Static("Access / Min", classes="stat-title"),
                Static("0", id="stat-access-rate", classes="stat-value"),
                classes="stat-card"
            )
        with Horizontal():
             yield Container(
                Static("Substrate Volume", classes="stat-title"),
                Static("0.00 MB", id="stat-db-size", classes="stat-value"),
                classes="stat-card"
            )
             yield Container(
                Static("Vector Capacity", classes="stat-title"),
                Static("0", id="stat-vec-size", classes="stat-value"),
                classes="stat-card"
            )
             yield Container(
                Static("Ingest / Min", classes="stat-title"),
                Static("0", id="stat-ingest-rate", classes="stat-value"),
                classes="stat-card"
            )

    def on_mount(self):
        self.set_interval(2, self.refresh_stats)
        self.refresh_stats()

    def refresh_stats(self):
        db_path = "data/memory.db"
        vec_path = "data/vectors.index.npy"

        total_mem = 0
        active_agents = 0
        db_size = "0 MB"
        vec_count = 0
        access_rate = 0
        ingest_rate = 0

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                count = 0
                for t in ["memories_semantic", "memories_episodic", "memories_procedural", "working_memory"]:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    count += cur.fetchone()[0]
                total_mem = count
                # Active agents in last 60s
                cur.execute("SELECT count(*) FROM logs_agents WHERE last_seen >= datetime('now','-60 seconds')")
                active_agents = cur.fetchone()[0]
                conn.close()
                size_mb = os.path.getsize(db_path) / (1024*1024)
                db_size = f"{size_mb:.2f} MB"
            except: pass

        if os.path.exists(vec_path):
            try:
                from app.services.vector_snapshot import read_snapshot
                _, ids = read_snapshot(vec_path)
                vec_count = len(ids)
            except (ValueError, OSError) as exc:
                vec_count = "INVALID (check logs)"
                import logging
                logging.getLogger(__name__).error("Vector snapshot invalid: %s", exc)

        # Access + ingest rates (last 60s)
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM logs_access WHERE ts >= datetime('now','-60 seconds')")
                access_rate = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM logs_access WHERE event_type = 'write' AND ts >= datetime('now','-60 seconds')")
                ingest_rate = cur.fetchone()[0]
                conn.close()
            except: pass

        self.query_one("#stat-total-mem", Static).update(str(total_mem))
        self.query_one("#stat-active-agents", Static).update(str(active_agents))
        self.query_one("#stat-db-size", Static).update(db_size)
        self.query_one("#stat-vec-size", Static).update(str(vec_count))
        self.query_one("#stat-access-rate", Static).update(str(access_rate))
        self.query_one("#stat-ingest-rate", Static).update(str(ingest_rate))

class AccessView(Container):
    def compose(self) -> ComposeResult:
        yield Static("Memory Access Stream", classes="stat-title")
        yield DataTable(id="access-table")

    def on_mount(self):
        table = self.query_one("#access-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Time", "Client", "Agent", "Type", "Layer", "Query/ID", "Status")
        table.zebra_stripes = True
        self.set_interval(2, self.refresh_access)
        self.refresh_access()

    def refresh_access(self):
        db_path = "data/memory.db"
        if not os.path.exists(db_path):
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, client_name, agent_id, event_type, target_layer, query, memory_id, status
                FROM logs_access
                ORDER BY ts DESC
                LIMIT 50
            """)
            rows = cur.fetchall()
            conn.close()

            table = self.query_one("#access-table", DataTable)
            table.clear()
            for row in rows:
                ts = row["ts"].split("T")[1][:8] if row["ts"] else ""
                client = row["client_name"] or "unknown"
                agent = row["agent_id"] or "unknown"
                etype = row["event_type"]
                layer = row["target_layer"] or "-"
                payload = row["query"] or row["memory_id"] or "-"
                status = row["status"]
                if etype == "write":
                    etype = "✳ write"
                elif etype == "read":
                    etype = "• read"
                table.add_row(ts, client, agent, etype, layer, payload[:40], status)
        except Exception:
            return

class AgentsView(Container):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Agent Swarm - Live Presence", classes="stat-title")
            yield DataTable(id="agents-table")
        with Vertical(id="agent-details", classes="stat-card"):
            yield Static("Entity Cognition / History", classes="stat-title")
            yield VerticalScroll(Static("Select an entity to view history", id="agent-summary-text"))

    def on_mount(self):
        table = self.query_one("#agents-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Status", "Client", "Agent", "Last Seen", "Reads(5m)", "Writes(5m)", "Errors(5m)")
        table.zebra_stripes = True
        self.refresh_agents()
        self.set_interval(5, self.refresh_agents)

    def refresh_agents(self):
        db_path = "data/memory.db"
        if not os.path.exists(db_path): return
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT agent_id, client_name, status, last_seen
                FROM logs_agents
                ORDER BY last_seen DESC
            """)
            agents = cur.fetchall()

            cur.execute("""
                SELECT agent_id,
                       SUM(CASE WHEN event_type = 'read' AND ts >= datetime('now','-5 minutes') THEN 1 ELSE 0 END) AS reads_5m,
                       SUM(CASE WHEN event_type = 'write' AND ts >= datetime('now','-5 minutes') THEN 1 ELSE 0 END) AS writes_5m,
                       SUM(CASE WHEN status = 'error' AND ts >= datetime('now','-5 minutes') THEN 1 ELSE 0 END) AS errors_5m
                FROM logs_access
                GROUP BY agent_id
            """)
            access_counts = {row[0]: row[1:] for row in cur.fetchall()}
            conn.close()

            table = self.query_one("#agents-table", DataTable)
            table.clear()
            now = datetime.datetime.utcnow()
            for agent_id, client_name, status, last_seen in agents:
                last_seen_ts = None
                try:
                    last_seen_ts = datetime.datetime.fromisoformat(last_seen)
                except Exception:
                    last_seen_ts = None
                delta = (now - last_seen_ts).total_seconds() if last_seen_ts else 9999
                if delta <= 30:
                    indicator = "🟢"
                elif delta <= 120:
                    indicator = "🟡"
                else:
                    indicator = "⚫"
                counts = access_counts.get(agent_id, (0, 0, 0))
                reads_5m, writes_5m, errors_5m = counts
                table.add_row(
                    indicator,
                    client_name or "unknown",
                    agent_id or "unknown",
                    (last_seen or "")[11:19],
                    str(reads_5m),
                    str(writes_5m),
                    str(errors_5m),
                )
        except: pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "agents-table":
            agent_id = event.data_table.get_row_at(event.row_key)[2]
            self.show_agent_history(agent_id)

    def show_agent_history(self, agent_id):
        db_path = "data/memory.db"
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT event_type, target_layer, query, memory_id, status, ts
                FROM logs_access
                WHERE agent_id = ?
                ORDER BY ts DESC LIMIT 12
            """, (agent_id,))
            rows = cur.fetchall()
            conn.close()

            text = f"[bold azure]Recent Activity for {agent_id}[/bold azure]\n\n"
            if not rows:
                text += "[yellow]No recent access events.[/yellow]"
            for r in rows:
                ts = r['ts'].split('T')[1][:8] if r['ts'] else ""
                detail = r['query'] or r['memory_id'] or "-"
                text += f"[bold #60A5FA]◈ {ts} | {r['event_type']} {r['target_layer']}[/bold #60A5FA]\n"
                text += f"[#9CA3AF]{detail} ({r['status']})[/#9CA3AF]\n\n"

            self.query_one("#agent-summary-text", Static).update(text)
        except Exception as e:
            self.query_one("#agent-summary-text", Static).update(f"Error: {e}")

class ClientIssuesView(Container):
    def compose(self) -> ComposeResult:
        yield Static("Client Issues (Ingestion / Schema / Auth)", classes="stat-title")
        yield DataTable(id="issues-table")

    def on_mount(self):
        table = self.query_one("#issues-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Time", "Status", "Client", "Agent", "Layer", "Code", "Message")
        table.zebra_stripes = True
        self.set_interval(3, self.refresh_issues)
        self.refresh_issues()

    def refresh_issues(self):
        db_path = "data/memory.db"
        if not os.path.exists(db_path):
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT id, created_at, status, client_id, client_name, agent_id, layer, error_code, message
                FROM logs_client_issues
                ORDER BY created_at DESC
                LIMIT 80
            """)
            rows = cur.fetchall()
            conn.close()

            table = self.query_one("#issues-table", DataTable)
            table.clear()
            for row in rows:
                ts = row["created_at"].split("T")[1][:8] if row["created_at"] else ""
                status = row["status"] or "open"
                status_label = "open" if status == "open" else "resolved"
                client = row["client_id"] or row["client_name"] or "unknown"
                agent = row["agent_id"] or "-"
                layer = row["layer"] or "-"
                code = row["error_code"] or "-"
                msg = (row["message"] or "-")[:80]
                table.add_row(ts, status_label, client, agent, layer, code, msg)
        except Exception:
            return


class ClientRegistryView(Container):
    selected_client_id: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Static("Client Registry (Observed / Registered / Trusted)", classes="stat-title")
        with Horizontal(classes="toolbar"):
            yield Button("Mark Trusted", id="btn_client_trusted", variant="primary")
            yield Button("Mark Observed", id="btn_client_observed")
            yield Button("Mark Blocked", id="btn_client_blocked")
            yield Static("Selected: -", id="client-selected", classes="muted")
        yield DataTable(id="clients-table")

    def on_mount(self):
        table = self.query_one("#clients-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Last Seen", "Status", "Client ID", "Name", "Parent", "Updated")
        table.zebra_stripes = True
        self.set_interval(5, self.refresh_clients)
        self.refresh_clients()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id != "clients-table":
            return
        row = event.data_table.get_row_at(event.row_key)
        if not row:
            return
        self.selected_client_id = row[2]
        self.query_one("#client-selected", Static).update(f"Selected: {self.selected_client_id}")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn_client_trusted":
            self._update_client_status("trusted")
        elif event.button.id == "btn_client_observed":
            self._update_client_status("observed")
        elif event.button.id == "btn_client_blocked":
            self._update_client_status("blocked")

    def _update_client_status(self, status: str):
        if not self.selected_client_id:
            return
        db_path = "data/memory.db"
        if not os.path.exists(db_path):
            return
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE clients_registry SET status = ?, updated_at = datetime('now') WHERE client_id = ?",
                    (status, self.selected_client_id),
                )
                conn.commit()
            self.refresh_clients()
        except Exception:
            return

    def refresh_clients(self):
        db_path = "data/memory.db"
        if not os.path.exists(db_path):
            return
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT client_id, client_name, parent_client_id, status, updated_at, last_seen
                FROM clients_registry
                ORDER BY (last_seen IS NULL), last_seen DESC, updated_at DESC
                LIMIT 120
            """)
            rows = cur.fetchall()
            conn.close()

            table = self.query_one("#clients-table", DataTable)
            table.clear()
            for row in rows:
                last_seen = row["last_seen"] or ""
                if "T" in last_seen:
                    last_seen = last_seen.split("T")[1][:8]
                updated = row["updated_at"] or ""
                if "T" in updated:
                    updated = updated.split("T")[1][:8]
                table.add_row(
                    last_seen,
                    row["status"] or "observed",
                    row["client_id"] or "unknown",
                    row["client_name"] or "-",
                    row["parent_client_id"] or "-",
                    updated,
                )
        except Exception:
            return

class HealthView(Container):
    def compose(self) -> ComposeResult:
        yield Static("Substrate Self-Diagnostic", classes="stat-title")
        yield VerticalScroll(Static("Scanning bio-signatures...", id="health_report"))
        yield Button("Run Doctor", id="btn_doctor", variant="primary")
        yield Static("LaunchAgent Health", classes="stat-title")
        yield DataTable(id="launch-table")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn_doctor": self.run_doctor(allow_heavy=True)

    def on_mount(self):
        self.run_doctor(allow_heavy=True)
        table = self.query_one("#launch-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Label", "State", "PID", "Notes")
        table.zebra_stripes = True
        self.set_interval(10, self.refresh_launch_agents)
        self.refresh_launch_agents()

    def run_doctor(self, allow_heavy: bool = False):
        from cli.doctor import run_doctor

        report = run_doctor(fix=True, allow_heavy=allow_heavy)
        lines = []

        if report.actions:
            lines.append("[bold green]🛠 Fixes Applied[/bold green]")
            lines.extend([f"[green]• {a}[/green]" for a in report.actions])

        if report.issues:
            lines.append("[bold red]⚠ Issues Found[/bold red]")
            lines.extend([f"[red]• {i}[/red]" for i in report.issues])
        else:
            lines.append("[green]✅ All Systems Operational[/green]")

        if report.notes:
            lines.append("[bold cyan]ℹ Notes[/bold cyan]")
            lines.extend([f"[cyan]• {n}[/cyan]" for n in report.notes])

        self.query_one("#health_report", Static).update("\n".join(lines))

    def refresh_launch_agents(self):
        table = self.query_one("#launch-table", DataTable)
        table.clear()
        uid = os.getuid()
        labels = [
            ("Server", "com.pinak.memory.server"),
            ("Watchdog", "com.pinak.memory.watchdog"),
            ("Doctor", "com.pinak.memory.doctor"),
            ("Backup", "com.pinak.memory.backup"),
        ]
        for title, label in labels:
            state = "unloaded"
            pid = "-"
            notes = ""
            try:
                result = subprocess.run(
                    ["/bin/launchctl", "print", f"gui/{uid}/{label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0:
                    out = result.stdout or ""
                    if "state = running" in out:
                        state = "running"
                    else:
                        state = "loaded"
                    for line in out.splitlines():
                        if line.strip().startswith("pid ="):
                            pid = line.split("=", 1)[-1].strip()
                            break
                else:
                    notes = (result.stderr or result.stdout or "").strip()[:80]
            except Exception as exc:
                notes = str(exc)[:80]
            table.add_row(f"{title} ({label})", state, pid, notes or "-")

class MemoryApp(App):
    CSS = GLOBAL_CSS
    TAB_META: Dict[str, Tuple[str, str]] = {
        "dashboard": ("System Mesh", "Global signals and memory substrate"),
        "access": ("Memory Access", "Read/write stream across layers"),
        "agents": ("Agent Swarm", "Live presence and activity deltas"),
        "issues": ("Client Issues", "Ingestion, schema, and auth anomalies"),
        "clients": ("Client Registry", "Observed, registered, trusted"),
        "health": ("Bio-Health", "Doctor status and launch agents"),
    }
    BINDINGS = [
        ("q", "quit", "Shutdown"),
        ("1", "show_dashboard", "Mesh"),
        ("2", "show_access", "Access"),
        ("3", "show_agents", "Agents"),
        ("4", "show_issues", "Issues"),
        ("5", "show_clients", "Clients"),
        ("6", "show_health", "Health"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield Sidebar(id="sidebar")
            with Container(id="main-content"):
                with Horizontal(id="topbar"):
                    yield Static("System Mesh", id="topbar-title")
                    yield Static("Global signals and memory substrate", id="topbar-subtitle")
                    yield Static("booting…", id="topbar-status")
                yield DashboardView(id="view-dashboard")
                yield AccessView(id="view-access")
                yield AgentsView(id="view-agents")
                yield ClientIssuesView(id="view-issues")
                yield ClientRegistryView(id="view-clients")
                yield HealthView(id="view-health")
        yield Footer()

    def on_mount(self):
        self.title = "Pinak Command Center"
        self.switch_tab("dashboard")
        self.set_interval(3, self.refresh_topbar_status)
        self.refresh_topbar_status()

    def action_show_dashboard(self):
        self.switch_tab("dashboard")

    def action_show_access(self):
        self.switch_tab("access")

    def action_show_agents(self):
        self.switch_tab("agents")

    def action_show_issues(self):
        self.switch_tab("issues")

    def action_show_clients(self):
        self.switch_tab("clients")

    def action_show_health(self):
        self.switch_tab("health")

    def on_list_view_selected(self, event: ListView.Selected):
        nav_id = event.item.id
        if nav_id == "nav-dashboard": self.switch_tab("dashboard")
        elif nav_id == "nav-access": self.switch_tab("access")
        elif nav_id == "nav-agents": self.switch_tab("agents")
        elif nav_id == "nav-issues": self.switch_tab("issues")
        elif nav_id == "nav-clients": self.switch_tab("clients")
        elif nav_id == "nav-health": self.switch_tab("health")

    def switch_tab(self, tab: str):
        self.query_one("#view-dashboard").display = (tab == "dashboard")
        self.query_one("#view-access").display = (tab == "access")
        self.query_one("#view-agents").display = (tab == "agents")
        self.query_one("#view-issues").display = (tab == "issues")
        self.query_one("#view-clients").display = (tab == "clients")
        self.query_one("#view-health").display = (tab == "health")
        title, subtitle = self.TAB_META.get(tab, ("Pinak Command Center", ""))
        self.query_one("#topbar-title", Static).update(title)
        self.query_one("#topbar-subtitle", Static).update(subtitle)

    def refresh_topbar_status(self):
        db_path = "data/memory.db"
        status = "offline"
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                total = 0
                for t in ["memories_semantic", "memories_episodic", "memories_procedural", "memories_rag", "working_memory"]:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    total += cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM logs_agents WHERE last_seen >= datetime('now','-60 seconds')")
                active_agents = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM logs_client_issues WHERE status = 'open'")
                open_issues = cur.fetchone()[0]
                conn.close()
                status = f"mem {total} • agents {active_agents} • issues {open_issues}"
            except Exception:
                status = "db error"
        self.query_one("#topbar-status", Static).update(status)

if __name__ == "__main__":
    MemoryApp().run()
