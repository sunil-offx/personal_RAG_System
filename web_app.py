import hashlib
import os
import json
import tempfile
import base64
from dotenv import load_dotenv

load_dotenv()

def create_pdf(text: str) -> bytes:
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.multi_cell(0, 8, text.encode('latin-1', 'replace').decode('latin-1'))
    return bytes(pdf.output())

import subprocess
from datetime import datetime, timedelta

import firebase_admin
from firebase_admin import credentials, firestore

import streamlit as st
import extra_streamlit_components as stx
import importlib
import sys

import rag_study_assistant
if 'rag_study_assistant' in sys.modules:
    importlib.reload(rag_study_assistant)

from rag_study_assistant import (
    STUDY_DIR,
    ingest,
    retrieve,
    answer_question,
    Chunk
)

# ── Firebase / Firestore setup ─────────────────────────────────────────────────
@st.cache_resource
def get_db():
    """Initialize Firebase app and return Firestore client (cached per session)."""
    if not firebase_admin._apps:
        try:
            if hasattr(st, "secrets") and "FIREBASE_KEY" in st.secrets:
                key_dict = json.loads(st.secrets["FIREBASE_KEY"])
                cred = credentials.Certificate(key_dict)
            else:
                key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase_key.json")
                cred = credentials.Certificate(key_path)
        except Exception:
            key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase_key.json")
            cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def init_storage():
    """Ensure study folder exists. Firestore creates collections automatically."""
    STUDY_DIR.mkdir(exist_ok=True)
    # No table creation needed — Firestore is schema-less


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def authenticate_user(email, password):
    admin_email = os.environ.get("ADMIN_EMAIL", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "myadminsecret")
    
    if email == admin_email and password == admin_password:
        return True, admin_email

    db = get_db()
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        if data.get("password_hash") == hash_password(password):
            doc.reference.update({"last_login": datetime.utcnow().isoformat()})
            return True, email
    return False, None


def create_user(email, password):
    db = get_db()
    # Check if user already exists
    existing = db.collection("users").where("email", "==", email).limit(1).stream()
    for _ in existing:
        return False  # Already exists
    db.collection("users").add({
        "email": email,
        "password_hash": hash_password(password),
        "created_at": datetime.utcnow().isoformat(),
    })
    return True


def load_users():
    db = get_db()
    docs = db.collection("users").stream()
    return [
        {
            "Email": d.to_dict().get("email"),
            "Sign Up Date": d.to_dict().get("created_at"),
            "Last Login": d.to_dict().get("last_login", "Never")
        }
        for d in docs
    ]


def create_room(room_code: str, created_by: str):
    db = get_db()
    existing = db.collection("rooms").where("room_code", "==", room_code).limit(1).stream()
    for _ in existing:
        return False
    db.collection("rooms").add({
        "room_code": room_code,
        "created_by": created_by,
        "created_at": datetime.utcnow().isoformat()
    })
    return True

def load_rooms():
    db = get_db()
    docs = db.collection("rooms").order_by("created_at", direction=firestore.Query.DESCENDING).stream()
    return [d.to_dict()["room_code"] for d in docs]

def check_room_exists(room_code: str):
    db = get_db()
    docs = db.collection("rooms").where("room_code", "==", room_code).limit(1).stream()
    for _ in docs:
        return True
    return False

def record_upload(filename: str, original_name: str, size_bytes: int):
    db = get_db()
    db.collection("uploads").add({
        "filename": filename,
        "original_name": original_name,
        "size_bytes": size_bytes,
        "uploaded_at": datetime.utcnow().isoformat(),
    })


def load_uploads():
    db = get_db()
    docs = db.collection("uploads").order_by(
        "uploaded_at", direction=firestore.Query.DESCENDING
    ).stream()
    return [
        (d.to_dict()["filename"], d.to_dict()["original_name"],
         d.to_dict()["size_bytes"], d.to_dict()["uploaded_at"])
        for d in docs
    ]


def save_history(username: str, question: str, answer: str):
    db = get_db()
    db.collection("history").add({
        "username": username,
        "question": question,
        "answer": answer,
        "pinned": 0,
        "created_at": datetime.utcnow().isoformat(),
    })


def load_history(username: str):
    db = get_db()
    docs = db.collection("history").where("username", "==", username).stream()
    rows = []
    for d in docs:
        data = d.to_dict()
        rows.append((d.id, data["question"], data["answer"],
                     data.get("pinned", 0), data["created_at"]))
    # Sort: pinned first, then newest
    rows.sort(key=lambda r: (-r[3], r[4]), reverse=False)
    rows.sort(key=lambda r: (-r[3], r[4]))
    return rows


def clear_history(username: str):
    db = get_db()
    docs = db.collection("history").where("username", "==", username).stream()
    for d in docs:
        d.reference.delete()


def set_pin(history_id: str, pinned: bool):
    db = get_db()
    db.collection("history").document(history_id).update(
        {"pinned": 1 if pinned else 0}
    )



def ensure_user_signed_in():
    """Centered login gate — Sign Up default, Sign In & Admin as links below."""
    now = datetime.utcnow()
    
    expires = st.session_state.get("signed_in_expires")
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires)
        except ValueError:
            expires = None

    username = st.session_state.get("username")
    if username and expires and expires > now:
        return

    # Default mode = Sign Up
    if "login_mode" not in st.session_state:
        st.session_state["login_mode"] = "signup"
    mode = st.session_state["login_mode"]

    # ── CSS with animations ──────────────────────────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

    .stApp {
        background: linear-gradient(135deg, #e8f4fd 0%, #f0f7ff 50%, #e8f0fe 100%);
        min-height: 100vh;
    }
    [data-testid="stHeader"], [data-testid="stToolbar"] { display: none; }

    /* ── Animations ─────────────────────────────────────────────────────── */
    @keyframes bounceIn {
        0%   { transform: scale(0.3) rotate(-15deg); opacity: 0; }
        50%  { transform: scale(1.15) rotate(5deg); opacity: 1; }
        70%  { transform: scale(0.9) rotate(-2deg); }
        100% { transform: scale(1) rotate(0deg); opacity: 1; }
    }
    @keyframes slideDown {
        0%   { transform: translateY(-30px); opacity: 0; filter: blur(8px); }
        100% { transform: translateY(0); opacity: 1; filter: blur(0); }
    }
    @keyframes fadeInUp {
        0%   { transform: translateY(20px); opacity: 0; }
        100% { transform: translateY(0); opacity: 1; }
    }
    @keyframes shimmer {
        0%   { background-position: -200% center; }
        100% { background-position: 200% center; }
    }
    @keyframes pulseGlow {
        0%, 100% { box-shadow: 0 4px 18px rgba(14,165,233,0.35); }
        50%      { box-shadow: 0 6px 30px rgba(14,165,233,0.6); }
    }

    .login-logo {
        animation: bounceIn 0.7s cubic-bezier(0.34, 1.56, 0.64, 1) both;
    }
    .login-title {
        animation: slideDown 0.6s ease-out 0.2s both;
    }
    .login-subtitle {
        animation: fadeInUp 0.5s ease-out 0.4s both;
    }

    /* Staggered form field animations */
    [data-testid="stMainBlockContainer"] div[data-testid="stTextInput"]:nth-of-type(1) {
        animation: fadeInUp 0.4s ease-out 0.5s both;
    }
    [data-testid="stMainBlockContainer"] div[data-testid="stTextInput"]:nth-of-type(2) {
        animation: fadeInUp 0.4s ease-out 0.65s both;
    }

    /* Labels */
    div[data-testid="stTextInput"] label,
    div[data-testid="stTextInput"] label p {
        color: #000000 !important;
    }

    /* Inputs */
    div[data-testid="stTextInput"] input {
        background: #ffffff !important;
        border: 1.5px solid #7dd3fc !important;
        border-radius: 10px !important;
        color: #000000 !important;
        font-size: 0.95rem;
        padding: 10px 14px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.04) !important;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    div[data-testid="stTextInput"] input:focus {
        border-color: #0ea5e9 !important;
        box-shadow: 0 0 0 3px rgba(14,165,233,0.15) !important;
        outline: none;
        transform: scale(1.01);
    }

    /* Primary button — shimmer + pulse */
    .stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #0ea5e9, #38bdf8, #7dd3fc, #38bdf8, #0ea5e9) !important;
        background-size: 200% auto !important;
        color: white !important;
        border: none !important;
        border-radius: 12px !important;
        font-size: 1rem !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 18px rgba(14,165,233,0.35);
        transition: all 0.3s;
        animation: shimmer 3s linear infinite, pulseGlow 2s ease-in-out infinite, fadeInUp 0.4s ease-out 0.8s both;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(90deg, #0284c7, #0ea5e9, #38bdf8, #0ea5e9, #0284c7) !important;
        box-shadow: 0 8px 32px rgba(14,165,233,0.6);
        transform: translateY(-2px) scale(1.02);
    }
    .stButton > button[kind="primary"]:active {
        transform: translateY(0) scale(0.98);
    }

    /* Link-style small buttons */
    .stButton > button[kind="secondary"] {
        background: transparent !important;
        border: none !important;
        color: #0ea5e9 !important;
        font-size: 0.88rem !important;
        padding: 2px 8px !important;
        text-decoration: underline !important;
        box-shadow: none !important;
        min-height: unset !important;
        transition: all 0.2s;
        animation: fadeInUp 0.4s ease-out 0.7s both;
    }
    .stButton > button[kind="secondary"]:hover {
        color: #0284c7 !important;
        background: transparent !important;
        transform: scale(1.05);
    }
    </style>
    """, unsafe_allow_html=True)

    # ── Animated centered header ──────────────────────────────────────────────
    st.markdown("""
    <div style="text-align:center; padding: 3.5rem 0 2rem 0;">
      <div class="login-logo" style="font-size:3.8rem; margin-bottom:0.6rem;">📚</div>
      <div class="login-title" style="font-size:2.1rem; font-weight:700; color:#0ea5e9; letter-spacing:-0.5px; line-height:1.2;">
        Study Material Assistant
      </div>
      <div class="login-subtitle" style="font-size:0.97rem; color:#64748b; margin-top:0.4rem;">
        Your AI-powered RAG learning companion
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Centered form ─────────────────────────────────────────────────────────
    _, col, _ = st.columns([1, 2, 1])
    with col:
        with st.form("auth_form", clear_on_submit=False):
            if mode == "signup":
                st.markdown("<h4 style='text-align:center; color:#334155; margin-bottom:1rem;'>Create an Account</h4>", unsafe_allow_html=True)
                email = st.text_input("Email", placeholder="you@example.com")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                submitted = st.form_submit_button("Sign Up", use_container_width=True, type="primary")
                if submitted:
                    if not email or not password:
                        st.error("Please enter email and password.")
                    else:
                        success = create_user(email, password)
                        if success:
                            st.success("Account created! You can now log in.")
                            st.session_state["login_mode"] = "signin"
                            st.rerun()
                        else:
                            st.error("Account already exists or error occurred.")
            else:
                st.markdown("<h4 style='text-align:center; color:#334155; margin-bottom:1rem;'>Welcome Back</h4>", unsafe_allow_html=True)
                email = st.text_input("Email", placeholder="you@example.com")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")
                if submitted:
                    if not email or not password:
                        st.error("Please enter email and password.")
                    else:
                        success, user_email = authenticate_user(email, password)
                        if success:
                            st.session_state["username"] = user_email
                            st.session_state["is_admin"] = (user_email == os.environ.get("ADMIN_EMAIL", "admin"))
                            st.session_state["signed_in_expires"] = now + timedelta(hours=48)
                            st.rerun()
                        else:
                            st.error("Invalid credentials.")

        # Links below the form
        c1, c2 = st.columns(2)
        with c1:
            if mode == "signup":
                if st.button("Already have an account? Sign In", key="go_signin"):
                    st.session_state["login_mode"] = "signin"
                    st.rerun()
            else:
                if st.button("Need an account? Sign Up", key="go_signup"):
                    st.session_state["login_mode"] = "signup"
                    st.rerun()
                    
        st.markdown(
            "<p style='text-align:center; color:#94a3b8; font-size:0.78rem; margin-top:1.2rem;'>"
            "🔒 Your files are securely stored</p>",
            unsafe_allow_html=True,
        )

    st.stop()


def ensure_env():
    if not os.environ.get("GROQ_API_KEY"):
        st.warning(
            "Environment variable `GROQ_API_KEY` is not set. "
            "Set it before using the assistant, otherwise answering will fail."
        )


def main():
    st.set_page_config(
        page_title="Study Material RAG Assistant",
        page_icon="📚",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    ensure_env()
    init_storage()
    ensure_user_signed_in()

    if st.session_state.get("is_admin"):
        admin_panel()
    else:
        user_panel()

def admin_panel():
    st.sidebar.title("Admin Panel")
    menu = st.sidebar.radio("Navigation", ["Manage Rooms", "All Users", "All Uploads", "User Search History"])

    if st.sidebar.button("Log out"):
        st.session_state.clear()
        st.rerun()

    if menu == "Manage Rooms":
        st.title("Manage Study Rooms")
        st.write("Create rooms, upload materials, and build AI memory.")
        
        # Room Creation
        with st.expander("➕ Create a New Room", expanded=True):
            new_room_code = st.text_input("Room Code (e.g., BIO101)")
            if st.button("Create Room"):
                if new_room_code:
                    if create_room(new_room_code, st.session_state["username"]):
                        st.success(f"Room '{new_room_code}' created successfully!")
                    else:
                        st.error("Room code already exists.")
                else:
                    st.warning("Please enter a room code.")
                    
        st.markdown("---")
        rooms = load_rooms()
        if not rooms:
            st.info("No rooms created yet.")
            return
            
        selected_room = st.selectbox("Select Room to Manage", rooms)
        if selected_room:
            st.header(f"Room: {selected_room}")
            
            import supabase_storage_util
            try:
                existing_files = supabase_storage_util.list_room_files(selected_room)
            except Exception as e:
                st.error(f"Failed to load files: {e}")
                existing_files = []
                
            if existing_files:
                st.subheader("Current Materials")
                for fname in existing_files:
                    if fname == "index.pkl":
                        continue
                    col1, col2 = st.columns([4, 1])
                    col1.write(f"📄 {fname}")
                    if col2.button("✕ Remove", key=f"del_{fname}"):
                        supabase_storage_util.delete_room_file(selected_room, fname)
                        st.rerun()
            
            st.subheader("Upload New Materials")
            uploaded_files = st.file_uploader(
                "Upload PDFs or Text files", 
                type=["pdf", "txt"], 
                accept_multiple_files=True,
                key="admin_upload"
            )
            
            if st.button("Upload to Room", type="primary") and uploaded_files:
                with st.spinner("Uploading..."):
                    for f in uploaded_files:
                        supabase_storage_util.upload_room_file(selected_room, f.name, f.getbuffer())
                    st.success("Uploaded successfully!")
                    st.rerun()
                    
            st.markdown("---")
            st.subheader("Build AI Memory")
            st.write("After adding or removing files, click below to update the AI's memory for this room.")
            if st.button("Analyze & Index Materials", type="primary"):
                with st.spinner("Analyzing... this may take a minute."):
                    import tempfile
                    from pathlib import Path
                    from rag_study_assistant import ingest
                    with tempfile.TemporaryDirectory() as tmpdir:
                        local_dir = Path(tmpdir)
                        downloaded = supabase_storage_util.download_all_room_files(selected_room, str(local_dir))
                        if not downloaded:
                            st.warning("No files found to index.")
                        else:
                            index_file_path = local_dir / "index.pkl"
                            ingest(study_dir=local_dir, index_path=index_file_path)
                            with open(index_file_path, "rb") as index_f:
                                supabase_storage_util.upload_room_file(selected_room, "index.pkl", index_f.read())
                            st.success("Index rebuilt successfully!")

    elif menu == "All Users":
        st.title("User Management")
        users = load_users()
        if users:
            st.table(users)
        else:
            st.write("No users found.")

    elif menu == "All Uploads":
        st.title("Upload History")
        uploads = load_uploads()
        if uploads:
            st.table([{"Filename": u[0], "Size (Bytes)": u[2], "Date": u[3]} for u in uploads])
        else:
            st.write("No uploads found.")

    elif menu == "User Search History":
        st.title("Search History")
        username_query = st.text_input("Enter username (email) to view history:")
        if username_query:
            hist = load_history(username_query)
            if hist:
                for h in hist:
                    with st.expander(f"Q: {h['question']} ({h['created_at']})"):
                        st.write(h["answer"])
            else:
                st.write("No history found for this user.")

def user_panel():
    st.sidebar.title("📚 Study Assistant")
    
    if st.sidebar.button("Log out"):
        st.session_state.clear()
        st.rerun()
        
    current_room = st.session_state.get("current_room")
    
    if not current_room:
        st.title("Join a Study Room")
        st.write("Enter the room code provided by your admin to access the study materials.")
        room_code = st.text_input("Room Code")
        if st.button("Join Room", type="primary"):
            if check_room_exists(room_code):
                st.session_state["current_room"] = room_code
                st.session_state["messages"] = []
                st.success(f"Joined room {room_code}!")
                st.rerun()
            else:
                st.error("Invalid room code. Please try again.")
    else:
        st.sidebar.markdown("---")
        st.sidebar.write(f"**Current Room:** {current_room}")
        if st.sidebar.button("Leave Room"):
            st.session_state["current_room"] = None
            st.session_state["messages"] = []
            st.rerun()
            
        st.subheader(f"Room: {current_room} - Chat")
        
        if "messages" not in st.session_state:
            st.session_state.messages = []
            
        import urllib.parse
        for i, message in enumerate(st.session_state.messages):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                
        if prompt := st.chat_input("Ask a question about this room's materials..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    answer = ""
                    try:
                        import tempfile
                        from pathlib import Path
                        from rag_study_assistant import retrieve, answer_question
                        import supabase_storage_util
                        
                        index_bytes = supabase_storage_util.download_room_file(current_room, "index.pkl")
                        if not index_bytes:
                            st.warning("AI Memory not found! The admin needs to analyze the materials for this room first.")
                        else:
                            with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp_idx:
                                tmp_idx.write(index_bytes)
                                tmp_idx_path = Path(tmp_idx.name)
                            try:
                                chunks = retrieve(prompt, k=5, allowed_sources=[], index_path=tmp_idx_path)
                                if not chunks:
                                    st.warning("No relevant passages found.")
                                else:
                                    answer = answer_question(prompt, chunks)
                                    st.markdown(answer)
                                    save_history(st.session_state["username"], prompt, answer)
                            finally:
                                os.remove(tmp_idx_path)
                    except Exception as e:
                        st.error(f"Error: {e}")
            if answer:
                st.session_state.messages.append({"role": "assistant", "content": answer})

if __name__ == "__main__":
    main()


