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
    is_correct_username = secrets.compare_digest(credentials.username, ADMIN_EMAIL)
    is_correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (is_correct_username and is_correct_password):
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
        # Call your ingest logic here
        return {"status": "success", "message": "Index rebuild triggered"}
        
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
