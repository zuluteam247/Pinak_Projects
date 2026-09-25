import typer
import uvicorn
import os
import sys
import json
import sqlite3
import faiss
from typing import Optional
from pathlib import Path

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

import datetime
import jwt
import httpx
from app.core.database import DatabaseManager

app = typer.Typer(name="pinak-memory", help="Pinak Memory Service CLI")

def _get_db_path():
    # Load from config or default
    # Simplified: assume default for CLI
    return "data/memory.db"

def _get_vector_path():
    return "data/vectors.index"

@app.command()
def start(
    host: str = "0.0.0.0",
    port: int = 8001,
    reload: bool = False
):
    """Start the Pinak Memory Service."""
    typer.echo(f"Starting Memory Service on {host}:{port}...")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)

@app.command()
def doctor(fix: bool = False):
    """Check system health and integrity."""
    typer.echo("Running Pinak Memory Doctor...")
    issues = []

    # 1. Check DB
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        issues.append(f"Database not found at {db_path}")
    else:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            res = cursor.fetchone()[0]
            if res != "ok":
                issues.append(f"DB Integrity Check Failed: {res}")
            conn.close()
            typer.echo("✅ Database Integrity OK")
        except Exception as e:
            issues.append(f"DB Error: {e}")

    # 2. Check Vector Store
    vec_path = _get_vector_path()
    if not os.path.exists(vec_path):
        issues.append(f"Vector Index not found at {vec_path}")
    else:
        try:
            index = faiss.read_index(vec_path)
            typer.echo(f"✅ Vector Index OK (Size: {index.ntotal})")

            # Check Consistency
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM memories_semantic WHERE embedding_id IS NOT NULL")
                db_count = cursor.fetchone()[0]
                conn.close()

                if db_count != index.ntotal:
                    msg = f"Inconsistency detected: DB has {db_count} embeddings, Vector Index has {index.ntotal}"
                    issues.append(msg)
                    if fix:
                        typer.echo("Attempting fix... (Not implemented: requires re-indexing from DB)")
                        # Logic: Iterate DB, clear Index, add all back.
        except Exception as e:
            issues.append(f"Vector Index Error: {e}")

    if not issues:
        typer.echo("\n🎉 All Systems Operational!")
    else:
        typer.echo("\n⚠️  Issues Found:")
        for i in issues:
            typer.echo(f" - {i}")
        if not fix:
            typer.echo("\nRun with --fix to attempt repairs (where supported).")

@app.command()
def stats(json_output: bool = False):
    """Show usage statistics."""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        typer.echo("Database not found.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    tables = ["memories_semantic", "memories_episodic", "memories_procedural", "memories_rag", "working_memory", "logs_events"]
    stats_data = {}

    for t in tables:
        try:
            cursor.execute(f"SELECT count(*) FROM {t}")
            stats_data[t] = cursor.fetchone()[0]
        except:
            stats_data[t] = 0

    # Storage Size
    stats_data['db_size_bytes'] = os.path.getsize(db_path)
    if os.path.exists(_get_vector_path()):
        stats_data['vector_size_bytes'] = os.path.getsize(_get_vector_path())

    conn.close()

    if json_output:
        typer.echo(json.dumps(stats_data, indent=2))
    else:
        typer.echo("\n📊 Memory Statistics")
        for k, v in stats_data.items():
            typer.echo(f"{k}: {v}")

@app.command()
def tui():
    """Launch Real-time Dashboard."""
    from cli.tui import MemoryApp
    MemoryApp().run()

@app.command()
def mint(tenant: str, project: str = "default", secret: str = None):
    """Mint a development JWT token."""
    jwt_secret = secret or os.environ.get("PINAK_JWT_SECRET")
    if not jwt_secret or jwt_secret in {"secret", "dev-secret-change-me"}:
        raise typer.BadParameter("Set a non-default PINAK_JWT_SECRET")
    payload = {
        "sub": "local-dev",
        "tenant": tenant,
        "project_id": project,
        "scopes": ["memory.read", "memory.write"],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=30),
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    typer.echo(token)

@app.command()
def search(query: str, tenant: str = "demo", project: str = "default", url: str = "http://localhost:8001"):
    """Perform a hybrid search (RRF) across all memory layers."""
    token_secret = os.environ.get("PINAK_JWT_SECRET")
    if not token_secret or token_secret in {"secret", "dev-secret-change-me"}:
        raise typer.BadParameter("Set a non-default PINAK_JWT_SECRET")
    token_payload = {
        "sub": "search-cli",
        "tenant": tenant,
        "project_id": project,
        "scopes": ["memory.read"],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=5),
    }
    token = jwt.encode(token_payload, token_secret, algorithm="HS256")
    
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client() as client:
            response = client.get(f"{url}/api/v1/memory/retrieve_context", params={"query": query}, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            typer.echo(f"\n🔍 Hybrid Search Results for: '{query}'")
            typer.echo("-" * 60)
            
            for layer, results in data.get("context_by_layer", {}).items():
                if results:
                    typer.echo(f"[{layer.upper()}]")
                    for r in results:
                        content = r.get("content") or r.get("steps") or str(r)
                        typer.echo(f" • {content}")
            
    except Exception as e:
        typer.echo(f"Error: {e}")

if __name__ == "__main__":
    app()
