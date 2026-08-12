import os
import streamlit as st
from supabase import create_client, Client, ClientOptions

_BUCKET_CHECKED = False

def get_supabase() -> Client:
    global _BUCKET_CHECKED
    # Use secrets if available, else environment variables
    try:
        if hasattr(st, "secrets") and "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
            url = st.secrets["SUPABASE_URL"]
            key = st.secrets["SUPABASE_KEY"]
        else:
            url = os.environ.get("SUPABASE_URL")
            key = os.environ.get("SUPABASE_KEY")
    except FileNotFoundError:
        # st.secrets throws FileNotFoundError if secrets.toml doesn't exist
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        
    if not url or not key:
        raise ValueError("Missing Supabase credentials.")
    client = create_client(url, key, options=ClientOptions(storage_client_timeout=120, postgrest_client_timeout=120))
    
    if not _BUCKET_CHECKED:
        try:
            buckets = client.storage.list_buckets()
            if not any(b.name == "study-materials" for b in buckets):
                client.storage.create_bucket("study-materials")
            _BUCKET_CHECKED = True
        except Exception:
            pass
            
    return client

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
    """Download all study files (except .pkl index files) to a local directory for indexing."""
    client = get_supabase()
    prefix = f"{username}/"
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
        
    res = client.storage.from_("study-materials").list(path=prefix)
    
    downloaded_files = []
    if isinstance(res, list):
        for item in res:
            fname = item.get("name")
            if fname and fname != ".emptyFolderPlaceholder" and not fname.endswith(".pkl"):
                local_path = os.path.join(local_dir, fname)
                content = download_file(username, fname)
                if content:
                    with open(local_path, "wb") as f:
                        f.write(content)
                    downloaded_files.append(fname)
    return downloaded_files


def list_room_files(room_code: str):
    client = get_supabase()
    prefix = f"rooms/{room_code}/"
    res = client.storage.from_("study-materials").list(path=prefix)
    files = []
    if isinstance(res, list):
        for item in res:
            if item.get("name") != ".emptyFolderPlaceholder":
                files.append(item.get("name"))
    return files

def upload_room_file(room_code: str, filename: str, content: bytes):
    client = get_supabase()
    path = f"rooms/{room_code}/{filename}"
    try:
        client.storage.from_("study-materials").remove([path])
    except Exception:
        pass
    res = client.storage.from_("study-materials").upload(file=content, path=path, file_options={"content-type": "application/octet-stream"})
    return res

def download_room_file(room_code: str, filename: str, silent: bool = False) -> bytes:
    client = get_supabase()
    path = f"rooms/{room_code}/{filename}"
    try:
        res = client.storage.from_("study-materials").download(path)
        return res
    except Exception as e:
        if not silent:
            try:
                import streamlit as st
                st.error(f"Failed to download '{filename}': {e}")
            except Exception:
                pass
        return None



def delete_room_file(room_code: str, filename: str):
    client = get_supabase()
    path = f"rooms/{room_code}/{filename}"
    client.storage.from_("study-materials").remove([path])

def download_all_room_files(room_code: str, local_dir: str):
    client = get_supabase()
    prefix = f"rooms/{room_code}/"
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
    res = client.storage.from_("study-materials").list(path=prefix)
    downloaded_files = []
    if isinstance(res, list):
        for item in res:
            fname = item.get("name")
            if fname and fname != ".emptyFolderPlaceholder" and not fname.endswith(".pkl"):
                local_path = os.path.join(local_dir, fname)
                content = download_room_file(room_code, fname, silent=True)
                if content:
                    with open(local_path, "wb") as f:
                        f.write(content)
                    downloaded_files.append(fname)
    return downloaded_files

def delete_all_room_files(room_code: str):
    client = get_supabase()
    files = list_room_files(room_code)
    paths_to_remove = [f"rooms/{room_code}/{f}" for f in files]
    if paths_to_remove:
        client.storage.from_("study-materials").remove(paths_to_remove)
