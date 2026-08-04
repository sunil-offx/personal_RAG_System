from fastapi import FastAPI, HTTPException, Security, Request
from fastapi.security import APIKeyHeader
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Admin Webhook Service")

# Setup a simple API key security for the webhook
API_KEY = os.environ.get("ADMIN_WEBHOOK_SECRET", "super-secret-default-key")
api_key_header = APIKeyHeader(name="X-Admin-Token", auto_error=True)

async def get_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(
            status_code=403, 
            detail="Could not validate credentials"
        )
    return api_key

@app.post("/admin/webhook")
async def process_admin_webhook(request: Request, api_key: str = Security(get_api_key)):
    """
    Generic webhook endpoint for admin tasks.
    Send a POST request with the header: X-Admin-Token: <your-secret>
    """
    try:
        # Parse the JSON payload
        payload = await request.json()
    except Exception:
        payload = {}
        
    action = payload.get("action")
    
    # Example logic based on action type
    if action == "rebuild_index":
        # Call your ingest logic here
        return {"status": "success", "message": "Index rebuild triggered"}
        
    elif action == "delete_user":
        user_email = payload.get("email")
        if not user_email:
            raise HTTPException(status_code=400, detail="Missing email for deletion")
        # Call your delete user logic here
        return {"status": "success", "message": f"User {user_email} deleted"}
        
    else:
        return {
            "status": "success", 
            "message": "Webhook received, but no specific action was matched.",
            "payload_received": payload
        }

if __name__ == "__main__":
    import uvicorn
    # Run this locally using: python admin_webhook.py
    uvicorn.run(app, host="0.0.0.0", port=8000)
