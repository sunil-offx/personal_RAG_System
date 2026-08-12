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

def get_room_creator(room_code: str) -> str:
    db = get_db()
    docs = db.collection("rooms").where("room_code", "==", room_code).limit(1).stream()
    for doc in docs:
        creator_email = doc.to_dict().get("created_by", "")
        if creator_email:
            admin_name = get_admin_profile(creator_email)
            if admin_name:
                return f"{admin_name} ({creator_email})"
            return creator_email
    return "Admin"

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

    # ── CSS with animations & viewport centering ────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body { 
        font-family: 'Inter', sans-serif;
        height: 100vh !important;
        overflow: hidden !important;
    }

    .stApp {
        background: linear-gradient(135deg, #070B15 0%, #0F172A 50%, #1E3A8A 100%) !important;
        height: 100vh !important;
        overflow: hidden !important;
    }

    [data-testid="stHeader"], [data-testid="stToolbar"] { display: none !important; }

    /* Center main block container vertically & horizontally */
    [data-testid="stMainBlockContainer"] {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
        max-width: 520px !important;
        margin: auto !important;
    }

    /* Card Box Container */
    [data-testid="stForm"] {
        background: rgba(15, 23, 42, 0.85) !important;
        border: 1px solid rgba(14, 165, 233, 0.4) !important;
        border-radius: 16px !important;
        padding: 2rem 1.8rem !important;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), 0 0 20px rgba(14, 165, 233, 0.15) !important;
        backdrop-filter: blur(10px) !important;
    }

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
    @keyframes pulseGlow {
        0%, 100% { box-shadow: 0 4px 18px rgba(37, 99, 235, 0.4); }
        50%      { box-shadow: 0 6px 28px rgba(14, 165, 233, 0.6); }
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

    /* Labels */
    div[data-testid="stTextInput"] label,
    div[data-testid="stTextInput"] label p {
        color: #F0F6FF !important;
        font-weight: 500 !important;
    }

    /* Inputs */
    div[data-testid="stTextInput"] input {
        background: rgba(15, 23, 42, 0.9) !important;
        border: 1px solid rgba(37, 99, 235, 0.6) !important;
        border-radius: 10px !important;
        color: #F0F6FF !important;
        font-size: 0.95rem;
        padding: 10px 14px;
        transition: all 0.3s ease;
    }
    div[data-testid="stTextInput"] input:focus {
        border-color: #0EA5E9 !important;
        box-shadow: 0 0 12px rgba(14, 165, 233, 0.4) !important;
        outline: none;
    }

    /* Checkbox */
    div[data-testid="stCheckbox"] label p {
        color: #94A3B8 !important;
        font-size: 0.85rem !important;
    }

    /* Primary button */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #1E3A8A 0%, #2563EB 50%, #0EA5E9 100%) !important;
        color: white !important;
        border: none !important;
        border-radius: 10px !important;
        font-size: 1rem !important;
        font-weight: 600 !important;
        transition: all 0.3s ease;
        animation: pulseGlow 2s ease-in-out infinite;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(135deg, #2563EB 0%, #0EA5E9 100%) !important;
        box-shadow: 0 8px 25px rgba(14, 165, 233, 0.6) !important;
        transform: translateY(-2px);
    }

    /* Link-style secondary button */
    .stButton > button[kind="secondary"] {
        background: transparent !important;
        border: none !important;
        color: #0EA5E9 !important;
        font-size: 0.9rem !important;
        padding: 4px 10px !important;
        text-decoration: underline !important;
        box-shadow: none !important;
        white-space: nowrap !important;
        width: 100% !important;
        text-align: center !important;
    }
    .stButton > button[kind="secondary"]:hover {
        color: #38BDF8 !important;
        background: transparent !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── Animated centered header ──────────────────────────────────────────────
    st.markdown("""
    <div style="text-align:center; padding: 1rem 0 1.2rem 0;">
      <div class="login-logo" style="font-size:3.2rem; margin-bottom:0.4rem;">📚</div>
      <div class="login-title" style="font-size:1.9rem; font-weight:700; color:#0ea5e9; letter-spacing:-0.5px; line-height:1.2;">
        Study Material Assistant
      </div>
      <div class="login-subtitle" style="font-size:0.92rem; color:#94a3b8; margin-top:0.3rem;">
        Your AI-powered RAG learning companion
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Centered form container ───────────────────────────────────────────────
    with st.form("auth_form", clear_on_submit=False):
        if mode == "signup":
            st.markdown("<h4 style='text-align:center; color:#F0F6FF; margin-bottom:1rem;'>Create an Account</h4>", unsafe_allow_html=True)
            email = st.text_input("Email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Create Account", use_container_width=True, type="primary")
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
            st.markdown("<h4 style='text-align:center; color:#F0F6FF; margin-bottom:1rem;'>Welcome Back</h4>", unsafe_allow_html=True)
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
    if mode == "signup":
        if st.button("Already have an account? Sign In", key="go_signin", use_container_width=True):
            st.session_state["login_mode"] = "signin"
            st.rerun()
    else:
        if st.button("Need an account? Create an Account", key="go_signup", use_container_width=True):
            st.session_state["login_mode"] = "signup"
            st.rerun()
                
    st.markdown(
        "<p style='text-align:center; color:#64748b; font-size:0.78rem; margin-top:0.8rem;'>"
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

    # Custom Theme Styling using #1E3A8A, #2563EB, and #0EA5E9
    st.markdown("""
        <style>
        /* Main background & Container styling */
        .stApp {
            background: linear-gradient(135deg, #070B15 0%, #0F172A 50%, #1E3A8A 100%) !important;
            color: #F0F6FF !important;
        }

        /* Sidebar Styling */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0A0F1D 0%, #1E3A8A 100%) !important;
            border-right: 1px solid rgba(14, 165, 233, 0.2) !important;
        }

        /* Headers with gradient colors */
        h1, h2, h3 {
            background: linear-gradient(90deg, #0EA5E9 0%, #2563EB 50%, #60A5FA 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 700 !important;
        }

        /* Buttons Styling */
        .stButton > button {
            background: linear-gradient(135deg, #1E3A8A 0%, #2563EB 100%) !important;
            color: #FFFFFF !important;
            border: 1px solid #0EA5E9 !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            transition: all 0.3s ease !important;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3) !important;
        }

        .stButton > button:hover {
            background: linear-gradient(135deg, #2563EB 0%, #0EA5E9 100%) !important;
            border-color: #38BDF8 !important;
            transform: translateY(-2px) !important;
            box-shadow: 0 6px 18px rgba(14, 165, 233, 0.5) !important;
        }

        /* Input boxes & Text area styling */
        .stTextInput > div > div > input, .stTextArea > div > div > textarea, .stSelectbox > div > div {
            background-color: rgba(15, 23, 42, 0.8) !important;
            color: #F0F6FF !important;
            border: 1px solid rgba(37, 99, 235, 0.5) !important;
            border-radius: 8px !important;
        }

        .stTextInput > div > div > input:focus, .stTextArea > div > div > textarea:focus {
            border-color: #0EA5E9 !important;
            box-shadow: 0 0 10px rgba(14, 165, 233, 0.4) !important;
        }

        /* Cards & Metric containers */
        [data-testid="stMetricValue"], [data-testid="stMetric"] {
            background: rgba(30, 58, 138, 0.3) !important;
            border: 1px solid rgba(14, 165, 233, 0.3) !important;
            border-radius: 10px !important;
            padding: 10px !important;
        }

        /* Links and accents */
        a {
            color: #0EA5E9 !important;
        }

        /* Streamlit info & warning box borders */
        .stAlert {
            background-color: rgba(30, 58, 138, 0.4) !important;
            border: 1px solid #2563EB !important;
            color: #F0F6FF !important;
        }
        </style>
    """, unsafe_allow_html=True)

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
                new_room_code = st.text_input("Room ID (e.g., DQZ4QX)", key="new_room_input")
            with col2:
                st.write("") # spacing
                st.write("") # spacing
                st.button("🎲 Generate Random ID", use_container_width=True, on_click=generate_random_code)
                    
            if st.button("Create Room", type="primary"):
                clean_code = new_room_code.strip().upper()
                if clean_code:
                    if not clean_code.isalnum():
                        st.warning("Room ID must contain letters and numbers only (e.g., DQZ4QX).")
                    elif create_room(clean_code, st.session_state["username"]):
                        st.success(f"Room '{clean_code}' created successfully!")
                    else:
                        st.error("Room ID already exists.")
                else:
                    st.warning("Please enter a Room ID.")
                    
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
                st.write("Enter the Room ID provided by your admin to access the study materials.")
                room_code = st.text_input("Room ID (e.g., DQZ4QX)")
                if st.button("Join room", type="primary"):
                    clean_code = room_code.strip().upper()
                    if not clean_code:
                        st.warning("Please enter a Room ID.")
                    elif not clean_code.isalnum():
                        st.error("Room ID must contain letters and numbers (e.g., DQZ4QX).")
                    elif check_room_exists(clean_code):
                        st.session_state["current_room"] = clean_code
                        st.session_state["messages"] = []
                        set_user_room(st.session_state["username"], clean_code)
                        record_room_visit(st.session_state["username"], clean_code)
                        st.success(f"Joined room {clean_code}!")
                        st.rerun()
                    else:
                        st.error("Invalid Room ID. Please check and try again.")
            
            with tab2:
                if joined_rooms:
                    room_options_map = {}
                    for r_code in joined_rooms:
                        creator_info = get_room_creator(r_code)
                        label = f"Room {r_code} (Admin: {creator_info})"
                        room_options_map[label] = r_code

                    selected_label = st.selectbox(
                        "Select an opened room", 
                        options=list(room_options_map.keys()),
                        index=None,
                        placeholder="-- Choose a room --"
                    )
                    if st.button("Enter Room", type="primary"):
                        if selected_label:
                            actual_room_code = room_options_map[selected_label]
                            if check_room_exists(actual_room_code):
                                st.session_state["current_room"] = actual_room_code
                                st.session_state["messages"] = []
                                set_user_room(st.session_state["username"], actual_room_code)
                                record_room_visit(st.session_state["username"], actual_room_code)
                                st.rerun()
                            else:
                                st.error("This room no longer exists.")
                else:
                    st.info("You haven't opened any rooms yet. Use 'To open' tab to join a room.")
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


