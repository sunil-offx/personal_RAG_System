from fastapi import FastAPI, HTTPException, Security, Request
from fastapi.security import APIKeyHeader
import os
from dotenv import load_dotenv

load_dotenv()

from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

app = FastAPI(title="Admin Webhook Service")

# Setup HTTP Basic Auth using email and password from .env
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "myadminsecret")
security = HTTPBasic()

def get_current_admin(credentials: HTTPBasicCredentials = Security(security)):
    is_correct = False
    
    # Check default admin
    if secrets.compare_digest(credentials.username, ADMIN_EMAIL) and secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        is_correct = True
    
    # Check additional 8 admins
    if not is_correct:
        for i in range(1, 9):
            adm_email = os.environ.get(f"ADMIN_EMAIL_{i}")
            adm_password = os.environ.get(f"ADMIN_PASSWORD_{i}")
            if adm_email and adm_password:
                if secrets.compare_digest(credentials.username, adm_email) and secrets.compare_digest(credentials.password, adm_password):
                    is_correct = True
                    break

    if not is_correct:
        raise HTTPException(
            status_code=403, 
            detail="Could not validate credentials"
        )
    return credentials.username

from pydantic import BaseModel
from typing import Optional, Dict, Any

class WebhookPayload(BaseModel):
    action: str
    email: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

@app.post("/admin/webhook")
async def process_admin_webhook(payload: WebhookPayload, username: str = Security(get_current_admin)):
    """
    Generic webhook endpoint for admin tasks.
    Send a POST request with HTTP Basic Auth (email and password).
    """
    action = payload.action
    
    # Example logic based on action type
    if action == "rebuild_index":
        room_code = "default"
        if payload.data and "room_code" in payload.data:
            room_code = payload.data["room_code"]
        
        try:
            import tempfile
            from pathlib import Path
            from rag_study_assistant import ingest
            import supabase_storage_util
            
            with tempfile.TemporaryDirectory() as tmpdir:
                local_dir = Path(tmpdir)
                downloaded = supabase_storage_util.download_all_room_files(room_code, str(local_dir))
                if not downloaded:
                    return {"status": "warning", "message": f"No files found to index for room '{room_code}'"}
                ingest(study_dir=local_dir, room_code=room_code)
            return {"status": "success", "message": f"Index rebuilt successfully for room '{room_code}'"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to rebuild index: {e}")
        
    elif action == "delete_user":
        user_email = payload.email
        if not user_email:
            raise HTTPException(status_code=400, detail="Missing email for deletion")
        # Call your delete user logic here
        return {"status": "success", "message": f"User {user_email} deleted"}
        
    else:
        return {
            "status": "success", 
            "message": "Webhook received, but no specific action was matched.",
            "payload_received": payload.model_dump()
        }

if __name__ == "__main__":
    import uvicorn
    # Run this locally using: python admin_webhook.py
    uvicorn.run(app, host="0.0.0.0", port=8000)
