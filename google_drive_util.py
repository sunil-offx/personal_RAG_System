import os
import io
import pickle
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google_auth_oauthlib.flow import Flow

SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email'
]

def get_flow(client_secret_file='google_credentials.json', redirect_uri=None):
    if redirect_uri is None:
        redirect_uri = os.environ.get('OAUTH_REDIRECT_URI', 'http://localhost:8501/')
    if not os.path.exists(client_secret_file):
        raise FileNotFoundError(f"Missing {client_secret_file}. Please follow the setup instructions.")
        
    return Flow.from_client_secrets_file(
        client_secret_file,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

def get_drive_service(creds):
    return build('drive', 'v3', credentials=creds)

def ensure_folder(service, folder_name="StudyAssistant_Files"):
    # Check if folder exists
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    if not items:
        # Create folder
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')
    return items[0].get('id')

def upload_file(service, folder_id, filename, file_bytes, mime_type='application/octet-stream'):
    # Check if file exists to overwrite
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    
    if items:
        # Update existing
        file_id = items[0].get('id')
        request = service.files().update(fileId=file_id, media_body=media)
        request.execute()
    else:
        # Create new
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        request = service.files().create(body=file_metadata, media_body=media, fields='id')
        request.execute()

def list_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    return results.get('files', [])

def download_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    return fh.getvalue()

def download_file_by_name(service, folder_id, filename):
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    items = results.get('files', [])
    if items:
        return download_file(service, items[0].get('id'))
    return None

def delete_file_by_name(service, folder_id, filename):
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
    items = results.get('files', [])
    if items:
        service.files().delete(fileId=items[0].get('id')).execute()

def download_all_files(service, folder_id, local_dir_path):
    import os
    if not os.path.exists(local_dir_path):
        os.makedirs(local_dir_path)
    
    files = list_files(service, folder_id)
    for f in files:
        if f.get('name') != 'index.pkl':
            content = download_file(service, f.get('id'))
            file_path = os.path.join(local_dir_path, f.get('name'))
            with open(file_path, 'wb') as out_f:
                out_f.write(content)
    return [f.get('name') for f in files if f.get('name') != 'index.pkl']
