
import os
import sys
import httpx
import jwt
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Config
# We can use the running server or default
API_URL = "http://localhost:8000/api/v1"
JWT_SECRET = os.getenv("PINAK_JWT_SECRET")
PROJECT_ID = "pinak-history"  # Keep it consistent 
TENANT = "default"

KNOWLEDGE_ROOT = os.path.expanduser("~/.gemini/antigravity/knowledge/")
BRAIN_ROOT = os.path.expanduser("~/.gemini/antigravity/brain/")

def mint_token() -> str:
    if not JWT_SECRET or JWT_SECRET in {"secret", "dev-secret-change-me"}:
        raise RuntimeError("Set a non-default PINAK_JWT_SECRET")
    payload = {
        "sub": "ingest-script",
        "tenant": TENANT,
        "project_id": PROJECT_ID,
        "role": "admin",
        "scopes": ["memory.write", "memory.read"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

async def ingest_semantic(client: httpx.AsyncClient):
    print(f"🚀 Ingesting Semantic Knowledge from {KNOWLEDGE_ROOT}...")
    count = 0
    if not os.path.exists(KNOWLEDGE_ROOT):
        print("   ⚠️ Knowledge root not found.")
        return

    for root, dirs, files in os.walk(KNOWLEDGE_ROOT):
        for file in files:
            if file.endswith(".md") or file.endswith(".txt"):
                path = Path(root) / file
                try:
                    content = path.read_text(errors="ignore")
                    tags = ["knowledge", file]
                    # Attempt to extract some tags from path
                    rel_path = path.relative_to(KNOWLEDGE_ROOT)
                    tags.extend([p for p in rel_path.parts[:-1]]) # Folders are tags

                    payload = {
                        "content": content,
                        "tags": tags,
                        "tenant": TENANT,
                        "project_id": PROJECT_ID
                    }
                    r = await client.post(f"{API_URL}/memory/add", json=payload)
                    r.raise_for_status()
                    count += 1
                    print(f"   ✅ Indexed: {rel_path}")
                except Exception as e:
                    print(f"   ❌ Failed {file}: {e}")
    print(f"✨ Semantic Ingestion Complete: {count} items.")

async def ingest_episodic(client: httpx.AsyncClient):
    print(f"🚀 Ingesting Episodic Context from {BRAIN_ROOT}...")
    count = 0
    if not os.path.exists(BRAIN_ROOT):
        print("   ⚠️ Brain root not found.")
        return

    # Iterate mainly over UUID directories
    for item in os.listdir(BRAIN_ROOT):
        session_dir = Path(BRAIN_ROOT) / item
        if session_dir.is_dir():
            # Look for summary artifacts
            artifacts = []
            summary_content = ""
            outcome = "unknown"
            
            # Priority files for summary
            search_order = ["walkthrough.md", "README.md", "task.md", "implementation_plan.md"]
            
            for fname in search_order:
                fpath = session_dir / fname
                if fpath.exists():
                    text = fpath.read_text(errors="ignore")
                    summary_content += f"\n\n--- {fname} ---\n{text[:2000]}" # Truncate individual files
            
            if not summary_content:
                # If no major artifacts, maybe skip or just log the ID
                continue

            try:
                # Create episodic entry
                payload = {
                    "content": f"Session {item} Artifacts:\n{summary_content}",
                    "goal": f"Session {item}", # We don't have the original prompt easily 
                    "outcome": "completed", # Assumed
                    "tags": ["session", item, "history"],
                    "tenant": TENANT,
                    "project_id": PROJECT_ID
                }
                
                # Check duplication? The API manages IDs, but we might want to avoid duplicate identical content.
                # For now, just push.
                r = await client.post(f"{API_URL}/memory/episodic/add", json=payload)
                r.raise_for_status()
                count += 1
                if count % 10 == 0:
                    print(f"   ✅ Processed {count} sessions...")
            except Exception as e:
                 print(f"   ❌ Failed session {item}: {e}")

    print(f"✨ Episodic Ingestion Complete: {count} items.")

async def main():
    token = mint_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        # Check health
        try:
            h = await client.get(f"{API_URL}/health")
            h.raise_for_status()
            print("🟢 Server is Online.")
        except Exception:
            print("🔴 Server unreachable. Start it with ./pinak-memory first!")
            return

        await ingest_semantic(client)
        await ingest_episodic(client)

if __name__ == "__main__":
    asyncio.run(main())
