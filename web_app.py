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

from streamlit_option_menu import option_menu

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


def record_admin_login(email: str):
    db = get_db()
    db.collection("admin_logins").add({
        "email": email,
        "login_at": datetime.utcnow().isoformat(),
    })

def load_admin_logins():
    db = get_db()
    docs = db.collection("admin_logins").order_by("login_at", direction=firestore.Query.DESCENDING).stream()
    return [
        {
            "Email": d.to_dict().get("email"),
            "Login Time": d.to_dict().get("login_at")
        }
        for d in docs
    ]

def authenticate_user(email, password):
    admin_email = os.environ.get("ADMIN_EMAIL", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "myadminsecret")
    
    if email == admin_email and password == admin_password:
        record_admin_login(admin_email)
        return True, admin_email

    for i in range(1, 9):
        adm_email = os.environ.get(f"ADMIN_EMAIL_{i}")
        adm_password = os.environ.get(f"ADMIN_PASSWORD_{i}")
        if adm_email and email == adm_email and password == adm_password:
            record_admin_login(adm_email)
            return True, adm_email

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

def update_user_profile(email: str, name: str, dob: str, register_number: str):
    db = get_db()
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for doc in docs:
        doc.reference.update({
            "name": name,
            "dob": dob,
            "register_number": register_number
        })

def get_user_profile(email: str):
    db = get_db()
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        return {
            "name": data.get("name", ""),
            "dob": data.get("dob", ""),
            "register_number": data.get("register_number", "")
        }
    return {"name": "", "dob": "", "register_number": ""}

def set_user_room(email: str, room_code: str):
    db = get_db()
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for doc in docs:
        doc.reference.update({"current_room": room_code})

def get_room_student_counts(admin_email: str):
    db = get_db()
    rooms = load_rooms(admin_email)
    users = db.collection("users").stream()
    room_counts = {r: 0 for r in rooms}
    for u in users:
        data = u.to_dict()
        r = data.get("current_room")
        if r in room_counts:
            room_counts[r] += 1
    return [{"Room Code": r, "Students Joined": room_counts[r]} for r in rooms]

def record_room_visit(email: str, room_code: str):
    db = get_db()
    db.collection("room_visits").add({
        "email": email,
        "room_code": room_code,
        "visited_at": datetime.utcnow().isoformat()
    })

def get_user_joined_rooms(email: str):
    db = get_db()
    docs = db.collection("room_visits").where("email", "==", email).stream()
    rooms = set()
    for d in docs:
        r = d.to_dict().get("room_code")
        if r:
            rooms.add(r)
    return sorted(list(rooms))

def get_room_visits_data(admin_email: str, timeframe: str):
    import pandas as pd
    db = get_db()
    rooms = load_rooms(admin_email)
    if not rooms:
        return pd.DataFrame()
    
    docs = db.collection("room_visits").stream()
    data = []
    for d in docs:
        v = d.to_dict()
        if v.get("room_code") in rooms:
            data.append(v)
            
    if not data:
        return pd.DataFrame()
        
    df = pd.DataFrame(data)
    df["visited_at"] = pd.to_datetime(df["visited_at"])
    
    if timeframe == "Daily":
        df["period"] = df["visited_at"].dt.strftime("%Y-%m-%d")
    elif timeframe == "Weekly":
        df["period"] = df["visited_at"].dt.strftime("%G-W%V")
    else: # Monthly
        df["period"] = df["visited_at"].dt.strftime("%Y-%m")
        
    grouped = df.groupby(["period", "room_code"]).size().reset_index(name="count")
    pivot_df = grouped.pivot(index="period", columns="room_code", values="count").fillna(0)
    return pivot_df

def update_admin_profile(email: str, name: str):
    db = get_db()
    docs = db.collection("admins").where("email", "==", email).limit(1).stream()
    found = False
    for doc in docs:
        doc.reference.update({"name": name})
        found = True
    if not found:
        db.collection("admins").add({"email": email, "name": name})

def get_admin_profile(email: str):
    db = get_db()
    docs = db.collection("admins").where("email", "==", email).limit(1).stream()
    for doc in docs:
        return doc.to_dict().get("name", "")
    return ""


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

def delete_room_db(room_code: str):
    db = get_db()
    docs = db.collection("rooms").where("room_code", "==", room_code).stream()
    for d in docs:
        d.reference.delete()

def load_rooms(admin_email: str = None):
    db = get_db()
    query = db.collection("rooms")
    if admin_email:
        query = query.where("created_by", "==", admin_email)
    docs = query.stream()
    rooms = [d.to_dict() for d in docs]
    rooms.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return [r.get("room_code") for r in rooms if r.get("room_code")]

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



def get_cookie_manager():
    if "cookie_manager" not in st.session_state:
        import extra_streamlit_components as stx
        st.session_state["cookie_manager"] = stx.CookieManager(key="auth")
    return st.session_state["cookie_manager"]

def check_is_admin(user_email):
    is_adm = (user_email == os.environ.get("ADMIN_EMAIL", "admin"))
    if not is_adm:
        for i in range(1, 9):
            if user_email == os.environ.get(f"ADMIN_EMAIL_{i}"):
                is_adm = True
                break
    return is_adm

def ensure_user_signed_in():
    """Centered login gate — Sign Up default, Sign In & Admin as links below."""
    cm = get_cookie_manager()
    now = datetime.utcnow()
    
    username = st.session_state.get("username")
    if not username:
        cookie_username = cm.get("auth_username")
        if cookie_username:
            st.session_state["username"] = cookie_username
            st.session_state["is_admin"] = check_is_admin(cookie_username)
            st.session_state["signed_in_expires"] = now + timedelta(days=30)
            st.rerun()

    expires = st.session_state.get("signed_in_expires")
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires)
        except ValueError:
            expires = None

    if username and expires and expires > now:
        return

    # Default mode = Sign In
    if "login_mode" not in st.session_state:
        st.session_state["login_mode"] = "signin"
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
                remember_me = st.checkbox("Remember me for 30 days", value=True)
                submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")
                if submitted:
                    if not email or not password:
                        st.error("Please enter email and password.")
                    else:
                        success, user_email = authenticate_user(email, password)
                        if success:
                            st.session_state["username"] = user_email
                            st.session_state["is_admin"] = check_is_admin(user_email)
                            st.session_state["signed_in_expires"] = now + timedelta(days=30)
                            cm = get_cookie_manager()
                            if remember_me:
                                cm.set("auth_username", user_email, expires_at=now + timedelta(days=30))
                            else:
                                cm.delete("auth_username")
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
    admin_email = st.session_state.get("username", "")
    is_super_admin = (admin_email == "orbis.pvt.ltd@gmail.com")

    if is_super_admin:
        menu_options = ["Manage Rooms", "All Users", "All Uploads", "Admin Login History", "Admin Profile", "Room Reports"]
        menu_icons = ['house', 'people', 'cloud-upload', 'clock-history', 'person', 'bar-chart']
    else:
        menu_options = ["Manage Rooms", "Admin Profile", "Room Reports"]
        menu_icons = ['house', 'person', 'bar-chart']

    with st.sidebar:
        menu = option_menu(
            "Admin Panel", 
            menu_options,
            icons=menu_icons,
            menu_icon="cast", 
            default_index=0,
        )

    if st.sidebar.button("Log out"):
        cm = get_cookie_manager()
        cm.delete("auth_username")
        st.session_state.clear()
        st.rerun()

    if menu == "Manage Rooms":
        st.title("Manage Study Rooms")
        st.write("Create rooms, upload materials, and build AI memory.")
        
        # Room Creation
        with st.expander("➕ Create a New Room", expanded=True):
            import random
            import string
            
            if "new_room_input" not in st.session_state:
                st.session_state["new_room_input"] = ""
                
            def generate_random_code():
                st.session_state["new_room_input"] = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                
            col1, col2 = st.columns([3, 1])
            with col1:
                new_room_code = st.text_input("Room Code (e.g., BIO101)", key="new_room_input")
            with col2:
                st.write("") # spacing
                st.write("") # spacing
                st.button("🎲 Generate Random Code", use_container_width=True, on_click=generate_random_code)
                    
            if st.button("Create Room", type="primary"):
                if new_room_code:
                    if create_room(new_room_code, st.session_state["username"]):
                        st.success(f"Room '{new_room_code}' created successfully!")
                    else:
                        st.error("Room code already exists.")
                else:
                    st.warning("Please enter a room code.")
                    
        st.markdown("---")
        rooms = load_rooms(admin_email=st.session_state["username"])
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
                
            def rebuild_room_index(room_code: str):
                import tempfile
                from pathlib import Path
                from rag_study_assistant import ingest
                import supabase_storage_util
                with tempfile.TemporaryDirectory() as tmpdir:
                    local_dir = Path(tmpdir)
                    downloaded = supabase_storage_util.download_all_room_files(room_code, str(local_dir))
                    if not downloaded:
                        return False, "No study material files found in this room to build index from."
                    try:
                        ingest(study_dir=local_dir, room_code=room_code)
                        return True, "Index rebuilt successfully!"
                    except RuntimeError as e:
                        return False, str(e)
                    except Exception as e:
                        return False, f"Unexpected error during indexing: {e}"

            if existing_files:
                st.subheader("Current Materials")
                for fname in existing_files:
                    if fname == "index.pkl" or fname.endswith("_index.pkl"):
                        continue
                    col1, col2 = st.columns([4, 1])
                    col1.write(f"📄 {fname}")
                    if col2.button("✕ Remove", key=f"del_{fname}"):
                        supabase_storage_util.delete_room_file(selected_room, fname)
                        with st.spinner("Updating AI memory in real time..."):
                            success, msg = rebuild_room_index(selected_room)
                        if not success:
                            st.warning(f"File removed. Notice: {msg}")
                        st.rerun()
            
            st.subheader("Upload New Materials")
            uploaded_files = st.file_uploader(
                "Upload PDFs or Text files", 
                type=["pdf", "txt"], 
                accept_multiple_files=True,
                key="admin_upload"
            )
            
            if st.button("Upload to Room", type="primary") and uploaded_files:
                with st.spinner("Uploading & generating AI memory in real time..."):
                    for f in uploaded_files:
                        supabase_storage_util.upload_room_file(selected_room, f.name, f.getvalue())
                    success, msg = rebuild_room_index(selected_room)
                        
                if success:
                    st.success("Uploaded & indexed in real time!")
                else:
                    st.warning(f"Uploaded files, but indexing notice: {msg}")
                # Wait 1.5 seconds before rerun so the user sees the message
                import time
                time.sleep(1.5)
                st.rerun()
            st.markdown("---")
            st.subheader("Build AI Memory")
            st.write("After adding or removing files, click below to update the AI's memory for this room.")
            if st.button("Analyze & Index Materials", type="primary"):
                with st.spinner("Analyzing... this may take a minute."):
                    success, msg = rebuild_room_index(selected_room)
                if success:
                    st.success("Index rebuilt successfully in real-time .pkl and Supabase pgvector!")
                else:
                    st.warning(f"⚠️ {msg}")

            st.markdown("---")
            st.subheader("Danger Zone")
            st.warning("Deleting this room will remove all its materials and AI memory permanently.")
            if st.button(f"🗑️ Delete Room '{selected_room}'", type="primary"):
                with st.spinner("Deleting room..."):
                    supabase_storage_util.delete_all_room_files(selected_room)
                    try:
                        client = supabase_storage_util.get_supabase()
                        client.table("documents").delete().eq("room_code", selected_room).execute()
                    except Exception as e:
                        pass
                    delete_room_db(selected_room)
                st.success(f"Room '{selected_room}' deleted successfully!")
                st.rerun()

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


    elif menu == "Admin Login History":
        st.title("Admin Login History")
        logins = load_admin_logins()
        if logins:
            st.table(logins)
        else:
            st.write("No admin logins found.")

    elif menu == "Admin Profile":
        st.title("Admin Profile")
        admin_email = st.session_state["username"]
        current_name = get_admin_profile(admin_email)
        
        with st.form("admin_profile_form"):
            name = st.text_input("Name", value=current_name)
            if st.form_submit_button("Update Profile"):
                update_admin_profile(admin_email, name)
                st.success("Profile updated!")

    elif menu == "Room Reports":
        st.title("Room Reports")
        admin_email = st.session_state["username"]
        
        st.subheader("Current Students in Rooms")
        counts = get_room_student_counts(admin_email)
        if counts:
            st.table(counts)
        else:
            st.write("No rooms found or no students joined.")
            
        st.subheader("Room Visiting Trends")
        timeframe = st.selectbox("Timeframe", ["Daily", "Weekly", "Monthly"])
        visits_df = get_room_visits_data(admin_email, timeframe)
        
        if not visits_df.empty:
            st.line_chart(visits_df)
        else:
            st.write("No visit data available yet.")

def user_panel():
    with st.sidebar:
        menu = option_menu(
            "Study Assistant",
            ["Study Room", "My Profile"],
            icons=['book', 'person'],
            default_index=0,
            styles={
                "menu-title": {"font-size": "1.1rem", "white-space": "nowrap"}
            }
        )
    
    if st.sidebar.button("Log out"):
        cm = get_cookie_manager()
        cm.delete("auth_username")
        st.session_state.clear()
        st.rerun()
        
    if menu == "My Profile":
        st.title("My Profile")
        user_email = st.session_state["username"]
        profile = get_user_profile(user_email)
        with st.form("user_profile_form"):
            name = st.text_input("Name", value=profile.get("name", ""))
            dob = st.text_input("Date of Birth", value=profile.get("dob", ""))
            reg_no = st.text_input("Register Number", value=profile.get("register_number", ""))
            if st.form_submit_button("Update Profile"):
                update_user_profile(user_email, name, dob, reg_no)
                st.success("Profile updated!")
                
    elif menu == "Study Room":
        current_room = st.session_state.get("current_room")
        
        if not current_room:
            st.title("Join a Study Room")
            user_email = st.session_state["username"]
            joined_rooms = get_user_joined_rooms(user_email)
            
            tab1, tab2 = st.tabs(["To open", "Opened"])
            
            with tab1:
                st.write("Enter the room code provided by your admin to access the study materials.")
                room_code = st.text_input("Room Code")
                if st.button("Join room", type="primary"):
                    if check_room_exists(room_code):
                        st.session_state["current_room"] = room_code
                        st.session_state["messages"] = []
                        set_user_room(st.session_state["username"], room_code)
                        record_room_visit(st.session_state["username"], room_code)
                        st.success(f"Joined room {room_code}!")
                        st.rerun()
                    else:
                        st.error("Invalid room code. Please try again.")
            
            with tab2:
                if joined_rooms:
                    selected_recent = st.selectbox(
                        "Select the existing room", 
                        joined_rooms,
                        index=None,
                        placeholder="-- Choose a room --"
                    )
                    if st.button("Enter", type="secondary"):
                        if selected_recent:
                            if check_room_exists(selected_recent):
                                st.session_state["current_room"] = selected_recent
                                st.session_state["messages"] = []
                                set_user_room(st.session_state["username"], selected_recent)
                                record_room_visit(st.session_state["username"], selected_recent)
                                st.rerun()
                            else:
                                st.error("This room no longer exists.")
                else:
                    st.write("You haven't joined any rooms yet.")
        else:
            st.sidebar.markdown("---")
            st.sidebar.write(f"**Current Room:** {current_room}")
            if st.sidebar.button("Leave Room"):
                st.session_state["current_room"] = None
                st.session_state["messages"] = []
                set_user_room(st.session_state["username"], "")
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
                            
                            chunks = []
                            try:
                                chunks = retrieve(prompt, k=5, room_code=current_room)
                            except Exception as e:
                                print(f"Retrieval error: {e}")
                            answer = answer_question(prompt, chunks)
                            st.markdown(answer)
                            save_history(st.session_state["username"], prompt, answer)
                        except Exception as e:
                            st.error(f"Error: {e}")
                if answer:
                    st.session_state.messages.append({"role": "assistant", "content": answer})

if __name__ == "__main__":
    main()


