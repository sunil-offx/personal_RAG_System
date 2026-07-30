import os
from firebase_admin import storage

def get_bucket():
    return storage.bucket()

def list_user_files(username: str):
    """List all files in the user's directory."""
    bucket = get_bucket()
    prefix = f"users/{username}/"
    blobs = bucket.list_blobs(prefix=prefix)
    files = []
    for blob in blobs:
        # Avoid including the directory itself if it exists as a zero-byte object
        if blob.name != prefix and not blob.name.endswith('/'):
            files.append(blob.name.replace(prefix, ""))
    return files

def upload_file(username: str, filename: str, content: bytes):
    """Upload a file to the user's directory."""
    bucket = get_bucket()
    blob = bucket.blob(f"users/{username}/{filename}")
    blob.upload_from_string(content)

def download_file(username: str, filename: str) -> bytes:
    """Download a file from the user's directory."""
    bucket = get_bucket()
    blob = bucket.blob(f"users/{username}/{filename}")
    if blob.exists():
        return blob.download_as_bytes()
    return None

def delete_file(username: str, filename: str):
    """Delete a file from the user's directory."""
    bucket = get_bucket()
    blob = bucket.blob(f"users/{username}/{filename}")
    if blob.exists():
        blob.delete()

def download_all_user_files(username: str, local_dir: str):
    """Download all files (except index.pkl) to a local directory for indexing."""
    bucket = get_bucket()
    prefix = f"users/{username}/"
    blobs = bucket.list_blobs(prefix=prefix)
    
    if not os.path.exists(local_dir):
        os.makedirs(local_dir)
        
    downloaded_files = []
    for blob in blobs:
        if blob.name != prefix and not blob.name.endswith('/'):
            fname = blob.name.replace(prefix, "")
            if fname != "index.pkl":
                local_path = os.path.join(local_dir, fname)
                blob.download_to_filename(local_path)
                downloaded_files.append(fname)
    return downloaded_files
