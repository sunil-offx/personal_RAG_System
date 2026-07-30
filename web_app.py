import hashlib
import os
import json
import json
import firebase_storage_util
from dotenv import load_dotenv

load_dotenv()

import streamlit as st

if hasattr(st, "secrets"):
    try:
        if "FIREBASE_KEY" in st.secrets and not os.path.exists("firebase_key.json"):
            with open("firebase_key.json", "w") as f:
                f.write(st.secrets["FIREBASE_KEY"])
    except Exception:
        pass

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
        firebase_admin.initialize_app(cred, {"storageBucket": "rag-systems-bf084.appspot.com"})
    return firestore.client()


def init_storage():
    """Ensure study folder exists. Firestore creates collections automatically."""
    STUDY_DIR.mkdir(exist_ok=True)
    # No table creation needed — Firestore is schema-less


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def authenticate_user(email, password):
    if email == "admin" and password == "myadminsecret":
        return True, "admin"

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
        st.markdown("<h4 style='text-align:center; color:#FF4103; margin-bottom:1rem;'>Sign In</h4>", unsafe_allow_html=True)
        
        tab1, tab2 = st.tabs(["Sign In", "Sign Up"])
        
        with tab1:
            with st.form("login_form"):
                email_input = st.text_input("Email")
                password_input = st.text_input("Password", type="password")
                if st.form_submit_button("Sign In"):
                    success, user_email = authenticate_user(email_input, password_input)
                    if success:
                        st.session_state["username"] = user_email
                        st.session_state["is_admin"] = (user_email == "[EMAIL_ADDRESS]")
                        st.session_state["signed_in_expires"] = now + timedelta(hours=48)
                        st.rerun()
                    else:
                        st.error("Invalid email or password.")
                        
        with tab2:
            st.markdown("<h4 style='text-align:center; color:#0ea5e9; margin-bottom:1rem;'>Create an Account</h4>", unsafe_allow_html=True)
            with st.form("signup_form"):
                new_email = st.text_input("Email")
                new_password = st.text_input("Password", type="password")
                confirm_password = st.text_input("Confirm Password", type="password")
                if st.form_submit_button("Sign Up"):
                    if new_password != confirm_password:
                        st.error("Passwords do not match!")
                    elif len(new_password) < 6:
                        st.error("Password must be at least 6 characters.")
                    else:
                        if create_user(new_email, new_password):
                            st.success("Account created successfully! You can now sign in on the 'Sign In' tab.")
                        else:
                            st.error("An account with that email already exists.")

        st.markdown(
            "<p style='text-align:center; color:#94a3b8; font-size:0.78rem; margin-top:1.2rem;'>"
            "🔒 Google Drive file upload requires signing in with Google.</p>",
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
    )

    # ── Global Custom Gradient Dark Theme ────────────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

    /* Main background */
    .stApp { 
        background: linear-gradient(135deg, #FF4103 0%, #001621 100%) !important; 
        color: #ffffff !important;
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background: rgba(0, 22, 33, 0.6) !important;
        border-right: 1px solid rgba(255, 65, 3, 0.3);
    }

    /* Sidebar text */
    [data-testid="stSidebar"] * { color: #f8fafc !important; }

    /* Headings */
    h1, h2, h3 { color: #ffffff !important; text-shadow: 0px 2px 4px rgba(0,0,0,0.5); }

    /* Buttons */
    .stButton > button {
        border-radius: 10px;
        border: 1.5px solid rgba(255, 65, 3, 0.5);
        background: rgba(0, 22, 33, 0.8);
        color: #FF4103;
        font-weight: 600;
        transition: all 0.2s;
    }
    .stButton > button:hover {
        background: #FF4103;
        border-color: #FF4103;
        color: #ffffff;
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #FF4103, #ff6b3d);
        color: white;
        border: none;
        box-shadow: 0 4px 15px rgba(255, 65, 3, 0.4);
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(90deg, #ff6b3d, #FF4103);
        transform: translateY(-1px);
    }

    /* Input fields & Labels */
    [data-testid="stWidgetLabel"] p {
        color: #e2e8f0 !important;
        font-weight: 600 !important;
    }
    
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextArea"] textarea {
        background: rgba(0, 22, 33, 0.7) !important;
        border: 1.5px solid rgba(255, 65, 3, 0.4) !important;
        border-radius: 10px !important;
        color: #ffffff !important;
        box-shadow: 0 2px 6px rgba(0,0,0,0.2) !important;
    }
    div[data-testid="stTextInput"] input:focus,
    div[data-testid="stTextArea"] textarea:focus {
        border-color: #FF4103 !important;
        box-shadow: 0 0 0 3px rgba(255, 65, 3, 0.25) !important;
    }

    /* Radio */
    div[role="radiogroup"] label {
        color: #FF4103 !important;
    }

    /* Expanders / Details */
    details summary {
        background: rgba(0, 22, 33, 0.8);
        border-radius: 8px;
        padding: 6px 12px;
        color: #FF4103;
        border: 1px solid rgba(255, 65, 3, 0.5);
    }
    
    /* Header Menu Bar */
    [data-testid="stHeader"] {
        background: rgba(0, 22, 33, 0.9) !important;
        border-bottom: 1px solid rgba(255, 65, 3, 0.3);
    }
    
    /* Base Chat Message */
    [data-testid="stChatMessage"] {
        padding: 0.8rem 1rem;
        margin-bottom: 1rem;
        width: fit-content;
        max-width: 80%;
        display: flex;
        gap: 0.5rem;
    }
    [data-testid="stChatMessage"] * {
        color: #ffffff !important;
    }
    
    /* User Message (Right aligned, dark bubble, no avatar) */
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
        background: #252529 !important; /* dark grey bubble */
        border-radius: 20px;
        margin-left: auto;
    }
    /* Hide user avatar */
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) > div:first-child {
        display: none !important;
    }

    /* AI Message (Left aligned, transparent background) */
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
        background: transparent !important;
        border: none !important;
        margin-right: auto;
        padding-left: 0;
    }

    /* Info / success / warning boxes */
    .stAlert { border-radius: 10px; background: rgba(0, 22, 33, 0.8) !important; color: #ffffff !important; border: 1px solid rgba(255, 255, 255, 0.1); }

    /* Transparent Chat Input Container Background */
    [data-testid="stBottomBlockContainer"],
    [data-testid="stBottomBlockContainer"] > div,
    [data-testid="stBottom"],
    [data-testid="stBottom"] > div,
    .stChatFloatingInputContainer,
    .stChatFloatingInputContainer > div {
        background-color: transparent !important;
        background: transparent !important;
    }
    </style>
    """, unsafe_allow_html=True)

    ensure_env()
    init_storage()
    ensure_user_signed_in()

    st.markdown("""
    <div style="text-align:center; padding: 1rem 0 2rem 0;">
      <div style="font-size:2.5rem; margin-bottom:0.2rem;">📚</div>
      <div style="font-size:2.2rem; font-weight:700; color:#FF4103; letter-spacing:-0.5px; text-shadow: 0px 2px 4px rgba(0,0,0,0.5);">Hey there! Ready to study?</div>
      <div style="font-size:1.05rem; color:#e2e8f0; margin-top:0.2rem;">Drop your PDFs or text files below and ask me anything about them.</div>
    </div>
    """, unsafe_allow_html=True)

    # Simple account area in sidebar (after initial sign-in gate)
    with st.sidebar:
        st.subheader("Account")
        st.markdown(f"**Current user:** `{st.session_state.get('username', 'guest')}`")
        if st.session_state.get("is_admin"):
            st.markdown("*(Admin Mode)*")
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()

        st.markdown("---")
        if st.session_state.get("is_admin"):
            menu_options = ["Admin Dashboard", "Manage GitHub"]
        else:
            menu_options = ["New study session", "History"]

        menu = st.radio(
            "Menu",
            menu_options,
            index=0,
        )

        st.markdown("---")
        
        st.header("Upload & Index (Firebase Storage)")
        st.write("Your files are securely saved to your personal cloud folder.")

        try:
            existing_files = firebase_storage_util.list_user_files(st.session_state["username"])
        except Exception as e:
            st.error(f"Failed to load files from Storage: {e}")
            existing_files = []
        
        selected_files = []
        if existing_files:
            st.markdown("**Currently uploaded files:**")
            # Style for delete buttons
            st.markdown("""
            <style>
            .del-btn button {
                background: #e0f2fe !important;
                border: 1px solid #bae6fd !important;
                border-radius: 50% !important;
                color: #1e293b !important;
                font-size: 0.75rem !important;
                padding: 0 !important;
                width: 22px !important;
                height: 22px !important;
                min-height: unset !important;
                line-height: 22px !important;
                transition: all 0.2s !important;
            }
            .del-btn button:hover {
                background: #bae6fd !important;
                border-color: #0ea5e9 !important;
                color: #000000 !important;
                transform: scale(1.15);
            }
            </style>
            """, unsafe_allow_html=True)
            for fname in sorted(existing_files):
                col1, col2 = st.columns([0.88, 0.12])
                with col1:
                    is_selected = st.checkbox(f"{fname}", value=True, key=f"sel_{fname}")
                    if is_selected:
                        selected_files.append(fname)
                with col2:
                    with st.container():
                        st.markdown('<div class="del-btn">', unsafe_allow_html=True)
                        if st.button("✕", key=f"del_{fname}", help=f"Remove {fname}"):
                            try:
                                firebase_storage_util.delete_file(st.session_state["username"], fname)
                            except Exception as e:
                                st.error(f"Failed to delete {fname}: {e}")
                            st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)
        st.session_state["selected_files"] = selected_files

        if "file_uploader_key" not in st.session_state:
            st.session_state["file_uploader_key"] = 1

        uploaded_files = st.file_uploader(
            "Upload study materials (PDF / TXT)",
            type=["pdf", "txt"],
            accept_multiple_files=True,
            key=str(st.session_state["file_uploader_key"]),
        )

        if uploaded_files:
            new_files = False
            with st.spinner("Uploading files to your secure storage..."):
                for f in uploaded_files:
                    try:
                        firebase_storage_util.upload_file(st.session_state["username"], f.name, f.getbuffer())
                        record_upload(
                            filename=f.name,
                            original_name=f.name,
                            size_bytes=len(f.getbuffer()),
                        )
                        new_files = True
                    except Exception as e:
                        st.error(f"Failed to save {f.name}: {e}")
            if new_files:
                st.success("Files uploaded successfully. You can upload more files step by step.")
                st.session_state["file_uploader_key"] += 1
                st.rerun()

        if st.button("Analyze the materials"):
            with st.spinner("Downloading files from Cloud and building index..."):
                try:
                    import tempfile
                    from pathlib import Path
                    from rag_study_assistant import ingest
                    
                    with tempfile.TemporaryDirectory() as tmpdir:
                        local_dir = Path(tmpdir) / "docs"
                        downloaded = firebase_storage_util.download_all_user_files(st.session_state["username"], str(local_dir))
                        if not downloaded:
                            st.warning("No files found to index! Please upload a file first.")
                        else:
                            idx = ingest(str(local_dir))
                            with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as index_f:
                                import pickle
                                pickle.dump(idx, index_f)
                                index_f.flush()
                                index_f.seek(0)
                                firebase_storage_util.upload_file(st.session_state["username"], "index.pkl", index_f.read())
                            st.success("Successfully built AI memory index!")
                except Exception as e:
                    st.error(f"Error during indexing: {e}")



    # Main content area depends on selected menu
    if menu == "New study session":
        st.subheader("Study Chat")

        # Initialize chat history
        if "messages" not in st.session_state:
            st.session_state["messages"] = []

        # Display chat messages from history on app rerun
        for message in st.session_state["messages"]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        # Accept user input
        if question := st.chat_input("Write a message..."):
            # Add user message to chat history
            st.session_state["messages"].append({"role": "user", "content": question})
            # Display user message in chat message container
            with st.chat_message("user"):
                st.markdown(question)

            # Display assistant response in chat message container
            with st.chat_message("assistant"):
                with st.spinner("Retrieving relevant passages and thinking..."):
                    try:
                        allowed = st.session_state.get("selected_files", [])
                        import tempfile
                        from pathlib import Path
                        from rag_study_assistant import retrieve, answer_question
                        
                        index_bytes = firebase_storage_util.download_file(st.session_state["username"], "index.pkl")
                        
                        if not index_bytes:
                            st.warning("AI Memory (index.pkl) not found. Please upload files and click 'Analyze the materials'.")
                            answer = "I don't have any study materials indexed yet. Please upload files and click 'Analyze the materials'."
                            st.markdown(answer)
                            st.session_state["messages"].append({"role": "assistant", "content": answer})
                        else:
                            with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp_idx:
                                tmp_idx.write(index_bytes)
                                tmp_idx_path = Path(tmp_idx.name)
                                
                            try:
                                chunks = retrieve(question, k=5, allowed_sources=allowed, index_path=tmp_idx_path)
                                if not chunks:
                                    answer = "No relevant passages found in your study materials. Make sure you uploaded files and rebuilt the index."
                                else:
                                    answer = answer_question(question, chunks)
                                    # Save to history for current user
                                    save_history(st.session_state["username"], question, answer)
                                st.markdown(answer)
                                st.session_state["messages"].append({"role": "assistant", "content": answer})
                            finally:
                                import os
                                os.remove(tmp_idx_path)
                    except Exception as e:
                        answer = f"Error while answering the question: {e}"
                        st.error(answer)
                        st.session_state["messages"].append({"role": "assistant", "content": answer})

    elif menu == "History":
        st.subheader("History")
        st.caption(
            "View your previous questions and answers. You can pin important ones or clear history."
        )

        username = st.session_state.get("username", "guest")
        entries = load_history(username)

        col_left, col_right = st.columns([1, 1])
        with col_left:
            if st.button("Refresh history"):
                entries = load_history(username)
        with col_right:
            if st.button("Clear history for this user"):
                clear_history(username)
                entries = []

        if not entries:
            st.info("No history yet for this user.")
        else:
            for entry in entries:
                hist_id, question, answer, pinned, created_at = entry
                pin_label = "Unpin" if pinned else "Pin"
                with st.expander(
                    f"{'📌 ' if pinned else ''}{created_at} – {question[:60]}{'...' if len(question) > 60 else ''}"
                ):
                    st.markdown(f"**Question**\n\n{question}")
                    st.markdown(f"**Answer**\n\n{answer}")
                    cols = st.columns(2)
                    with cols[0]:
                        if st.button(
                            pin_label,
                            key=f"pin_{hist_id}_{pinned}",
                        ):
                            set_pin(hist_id, not pinned)
                            # For Streamlit >= 1.x, use st.rerun(); older versions used experimental_rerun
                            st.rerun()
                    with cols[1]:
                        st.write(f"Created at: {created_at}")
                        
    elif menu == "Manage GitHub" and st.session_state.get("is_admin"):
        st.subheader("Manage GitHub Repository (Admin Only)")
        st.caption("Sync your latest changes and study materials to GitHub directly from the app.")
        
        commit_msg = st.text_input("Commit Message", value="Update study materials and application files")
        
        if st.button("Commit and Push to GitHub"):
            with st.spinner("Syncing to GitHub..."):
                try:
                    subprocess.run(["git", "add", "."], check=True, capture_output=True, text=True)
                    res_commit = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True)
                    res_push = subprocess.run(["git", "push", "origin", "main"], check=True, capture_output=True, text=True)
                    
                    st.success("Successfully pushed to GitHub!")
                    with st.expander("Show Logs"):
                        st.text("Commit Log:\n" + res_commit.stdout + res_commit.stderr)
                        st.text("Push Log:\n" + res_push.stdout + res_push.stderr)
                except subprocess.CalledProcessError as e:
                    st.error("Failed to sync with GitHub.")
                    with st.expander("Show Error Details"):
                        st.text(e.stderr or e.stdout)

    elif menu == "Admin Dashboard" and st.session_state.get("is_admin"):
        st.subheader("Admin Dashboard")
        st.caption("Overview of registered users (Logins and Sign Ups).")
        users = load_users()
        if users:
            st.dataframe(users, use_container_width=True)
        else:
            st.info("No users registered yet.")


if __name__ == "__main__":
    main()


