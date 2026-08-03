import os
import streamlit as st
from supabase import create_client, Client, ClientOptions

def get_supabase() -> Client:
    # Use secrets if available, else environment variables
    if hasattr(st, "secrets") and "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    else:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        
    if not url or not key:
        raise ValueError("Missing Supabase credentials.")
    return create_client(url, key, options=ClientOptions(storage_client_timeout=120, postgrest_client_timeout=120))

def list_user_files(username: str):
    """List all files in the user's directory."""
    client = get_supabase()
    prefix = f"{username}/"
    res = client.storage.from_("study-materials").list(path=prefix)
    
    files = []
    # Supabase list() returns a list of dictionaries with 'name', 'id', 'updated_at', etc.
    if isinstance(res, list):
        for item in res:
            if item.get("name") != ".emptyFolderPlaceholder":
                files.append(item.get("name"))
    return files

def upload_file(username: str, filename: str, content: bytes):
    """Upload a file to the user's directory."""
    client = get_supabase()
    path = f"{username}/{filename}"
    # Remove existing if overwriting to avoid errors
    client.storage.from_("study-materials").remove([path])
    res = client.storage.from_("study-materials").upload(file=content, path=path, file_options={"content-type": "application/octet-stream"})

def download_file(username: str, filename: str) -> bytes:
    """Download a file from the user's directory."""
    client = get_supabase()
    path = f"{username}/{filename}"
    try:
        res = client.storage.from_("study-materials").download(path)
        return res
    except Exception as e:
        import streamlit as st
        st.error(f"Failed to download '{filename}': {e}")
        return None

def delete_file(username: str, filename: str):
    """Delete a file from the user's directory."""
    client = get_supabase()
    path = f"{username}/{filename}"
    client.storage.from_("study-materials").remove([path])

def download_all_user_files(username: str, local_dir: str):
    """Download all files (except index.pkl) to a local directory for indexing."""
    client = get_supabase()
    prefix = f"{username}/"
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
        
    res = client.storage.from_("study-materials").list(path=prefix)
    
    downloaded_files = []
    if isinstance(res, list):
        for item in res:
            fname = item.get("name")
            if fname and fname != ".emptyFolderPlaceholder" and fname != "index.pkl":
                local_path = os.path.join(local_dir, fname)
                content = download_file(username, fname)
                if content:
                    with open(local_path, "wb") as f:
                        f.write(content)
                    downloaded_files.append(fname)
    return downloaded_files
