import json
import httpx
import os
import sys

import jwt
import datetime

# Standard Pinak Service URL
API_BASE = "http://localhost:8000/api/v1"
TENANT = "default"
PROJECT = "pinak-history"

def get_token():
    secret = os.getenv("PINAK_JWT_SECRET")
    if not secret or secret in {"secret", "dev-secret-change-me"}:
        raise RuntimeError("Set a non-default PINAK_JWT_SECRET")
    algo = os.getenv("PINAK_JWT_ALGORITHM", "HS256")
    payload = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "role": "agent",
        "scopes": ["memory.write"],
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    }
    return jwt.encode(payload, secret, algorithm=algo)

def ingest_summaries(file_path):
    with open(file_path, "r") as f:
        summaries = json.load(f)
    
    headers = {"Authorization": f"Bearer {get_token()}"}
    client = httpx.Client(base_url=API_BASE, headers=headers, timeout=30.0)
    
    print(f"Ingesting {len(summaries)} session summaries...")
    
    for item in summaries:
        content = f"Session: {item['title']}\nSummary: {item['summary']}"
        payload = {
            "content": content,
            "salience": 5,
            "goal": item.get('title', ''),
            "outcome": item.get('summary', '')
        }
        
        try:
            # We use the /episodic/add endpoint logic directly via MemoryService if possible,
            # but since we want to show how 'all agents' do it, we'll use the API.
            # However, for this script to work, the server MUST be running.
            # I will run it in a separate process in the next step.
            resp = client.post("/memory/episodic/add", json=payload)
            if resp.status_code == 201:
                print(f"  [OK] {item['id']}")
            else:
                print(f"  [FAIL] {item['id']} - {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  [ERROR] {item['id']} - {str(e)}")

if __name__ == "__main__":
    if not os.path.exists("summaries.json"):
        print("summaries.json not found")
        sys.exit(1)
    
    ingest_summaries("summaries.json")
