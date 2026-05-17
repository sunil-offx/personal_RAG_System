import hashlib
import os
import json
from dotenv import load_dotenv

load_dotenv()

import subprocess
from datetime import datetime, timedelta

import firebase_admin
from firebase_admin import credentials, firestore

import streamlit as st
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
)

# ── Firebase / Firestore setup ─────────────────────────────────────────────────
@st.cache_resource
def get_db():
    """Initialize Firebase app and return Firestore client (cached per session)."""
    if not firebase_admin._apps:
        # Try loading key from Streamlit secrets first, then from local file
        if hasattr(st, "secrets") and "FIREBASE_KEY" in st.secrets:
            key_dict = json.loads(st.secrets["FIREBASE_KEY"])
            cred = credentials.Certificate(key_dict)
        else:
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
    if email == "admin" and password == "myadminsecret":
        return True, "admin"

    db = get_db()
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        if data.get("password_hash") == hash_password(password):
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
    [data-testid="stMainBlockContainer"] .stTextInput:nth-of-type(1) {
        animation: fadeInUp 0.4s ease-out 0.5s both;
    }
    [data-testid="stMainBlockContainer"] .stTextInput:nth-of-type(2) {
        animation: fadeInUp 0.4s ease-out 0.65s both;
    }

    /* Inputs */
    .stTextInput > div > div > input {
        background: #ffffff;
        border: 1.5px solid #bae6fd;
        border-radius: 10px;
        color: #1e3a5f;
        font-size: 0.95rem;
        padding: 10px 14px;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .stTextInput > div > div > input:focus {
        border-color: #0ea5e9;
        box-shadow: 0 0 0 3px rgba(14,165,233,0.15);
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

        # Mode heading
        mode_labels = {
            "signup": "✨ Create your account",
            "signin": "👋 Welcome back",
            "admin":  "🔐 Admin Login",
        }
        st.markdown(
            f"<h4 style='text-align:center; color:#0369a1; margin-bottom:1rem;'>"
            f"{mode_labels[mode]}</h4>",
            unsafe_allow_html=True,
        )

        # Form fields
        if mode == "admin":
            email_val = "admin"
            pwd_input = st.text_input(
                "Admin Password", type="password",
                placeholder="Enter admin password",
                key="f_admin_pwd",
            )
            email_input = email_val
            password_input = pwd_input
        else:
            email_input = st.text_input(
                "📧 Email", placeholder="you@example.com",
                key="f_email",
            )
            password_input = st.text_input(
                "🔒 Password", type="password",
                placeholder="Create a password" if mode == "signup" else "Your password",
                key="f_pwd",
            )

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── Centered mode-switch links below password ─────────────────────────
        link_cols = st.columns([1, 1, 1, 1, 1])
        if mode == "signup":
            with link_cols[1]:
                if st.button("Sign In", key="lnk_signin", type="secondary"):
                    st.session_state["login_mode"] = "signin"
                    st.rerun()
            with link_cols[3]:
                if st.button("Admin", key="lnk_admin", type="secondary"):
                    st.session_state["login_mode"] = "admin"
                    st.rerun()
        elif mode == "signin":
            with link_cols[1]:
                if st.button("Sign Up", key="lnk_signup", type="secondary"):
                    st.session_state["login_mode"] = "signup"
                    st.rerun()
            with link_cols[3]:
                if st.button("Admin", key="lnk_admin2", type="secondary"):
                    st.session_state["login_mode"] = "admin"
                    st.rerun()
        else:  # admin
            with link_cols[2]:
                if st.button("← Back to Sign In", key="lnk_back", type="secondary"):
                    st.session_state["login_mode"] = "signin"
                    st.rerun()

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        # ── Main action button ────────────────────────────────────────────────
        btn_label = {
            "signup": "→ Create Account",
            "signin": "→ Sign In",
            "admin":  "→ Admin Login",
        }[mode]

        if st.button(btn_label, use_container_width=True, type="primary", key="auth_btn"):
            email = email_input.strip()
            pwd = password_input.strip()

            if mode == "admin":
                if pwd == os.environ.get("ADMIN_PASSWORD", "Admin@Study2025"):
                    st.session_state.update({
                        "username": "admin",
                        "is_admin": True,
                        "signed_in_expires": now + timedelta(hours=48),
                    })
                    st.success("✅ Signed in as Admin!")
                    st.rerun()
                else:
                    st.error("❌ Invalid admin password.")

            elif mode == "signup":
                if not email or not pwd:
                    st.warning("⚠️ Please fill in both fields.")
                elif create_user(email, pwd):
                    st.success("✅ Account created! Click **Sign In** below to log in.")
                else:
                    st.error("❌ An account with this email already exists.")

            elif mode == "signin":
                if not email or not pwd:
                    st.warning("⚠️ Please fill in both fields.")
                else:
                    ok, user_email = authenticate_user(email, pwd)
                    if ok:
                        st.session_state.update({
                            "username": user_email,
                            "is_admin": (user_email == "admin"),
                            "signed_in_expires": now + timedelta(hours=48),
                        })
                        st.success(f"✅ Welcome back, `{user_email}`!")
                        st.rerun()
                    else:
                        st.error("❌ Invalid email or password.")

        st.markdown(
            "<p style='text-align:center; color:#94a3b8; font-size:0.78rem; margin-top:1.2rem;'>"
            "🔒 Your data is stored securely in the cloud</p>",
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

    # ── Global ice blue + light gray theme ────────────────────────────────────
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

    .stApp { background: #f0f7ff; }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #e0f2fe, #f0f9ff) !important;
        border-right: 1px solid #bae6fd;
    }

    /* Sidebar text */
    [data-testid="stSidebar"] * { color: #1e3a5f !important; }

    /* Headings */
    h1, h2, h3 { color: #0369a1 !important; }

    /* Buttons */
    .stButton > button {
        border-radius: 10px;
        border: 1.5px solid #bae6fd;
        background: #ffffff;
        color: #0ea5e9;
        font-weight: 500;
        transition: all 0.2s;
    }
    .stButton > button:hover {
        background: #e0f2fe;
        border-color: #0ea5e9;
        color: #0284c7;
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(90deg, #0ea5e9, #38bdf8);
        color: white;
        border: none;
        box-shadow: 0 4px 15px rgba(14,165,233,0.3);
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(90deg, #0284c7, #0ea5e9);
        transform: translateY(-1px);
    }

    /* Input fields */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea {
        background: #ffffff;
        border: 1.5px solid #bae6fd;
        border-radius: 10px;
        color: #1e3a5f;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #0ea5e9;
        box-shadow: 0 0 0 3px rgba(14,165,233,0.15);
    }

    /* Radio */
    div[role="radiogroup"] label {
        color: #0ea5e9 !important;
    }

    /* Expanders */
    details summary {
        background: #e0f2fe;
        border-radius: 8px;
        padding: 6px 12px;
        color: #0369a1;
    }

    /* Info / success / warning boxes */
    .stAlert { border-radius: 10px; }
    </style>
    """, unsafe_allow_html=True)

    ensure_env()
    init_storage()
    ensure_user_signed_in()

    st.markdown("""
    <div style="text-align:center; padding: 1rem 0 2rem 0;">
      <div style="font-size:2.5rem; margin-bottom:0.2rem;">📚</div>
      <div style="font-size:2.2rem; font-weight:700; color:#0369a1; letter-spacing:-0.5px;">Hey there! Ready to study?</div>
      <div style="font-size:1.05rem; color:#64748b; margin-top:0.2rem;">Drop your PDFs or text files below and ask me anything about them.</div>
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
        menu_options = ["New study session", "History"]
        if st.session_state.get("is_admin"):
            menu_options.append("Manage GitHub")

        menu = st.radio(
            "Menu",
            menu_options,
            index=0,
        )

        st.markdown("---")
        
        st.header("Upload & Index")
        st.write(f"Study folder on disk: `{STUDY_DIR}`")

        existing_files = []
        if os.path.exists(STUDY_DIR):
            for fname in os.listdir(STUDY_DIR):
                if fname.lower().endswith(".pdf") or fname.lower().endswith(".txt"):
                    existing_files.append(fname)
        
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
                                os.remove(os.path.join(STUDY_DIR, fname))
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
            for f in uploaded_files:
                save_path = STUDY_DIR / f.name
                if not os.path.exists(save_path):
                    try:
                        with open(save_path, "wb") as out:
                            out.write(f.getbuffer())
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

        if st.button("Rebuild index from uploaded materials"):
            with st.spinner("Building index from study materials..."):
                try:
                    ingest()
                    st.success("Index rebuilt successfully.")
                except Exception as e:
                    st.error(f"Error while ingesting study materials: {e}")



    # Main content area depends on selected menu
    if menu == "New study session":
        st.subheader("New study session")
        st.caption("Like 'New chat' in ChatGPT – ask a fresh question.")

        if st.button("Clear question"):
            if "question_input" in st.session_state:
                st.session_state["question_input"] = ""

        question = st.text_area(
            "Your question",
            key="question_input",
            height=80,
            placeholder="Type your question about the uploaded study materials...",
        )

        if st.button("Get answer", type="primary") and question.strip():
            with st.spinner("Retrieving relevant passages and generating answer..."):
                try:
                    allowed = st.session_state.get("selected_files", [])
                    chunks = retrieve(question, k=5, allowed_sources=allowed)
                    if not chunks:
                        st.warning(
                            "No relevant passages found in your study materials. "
                            "Make sure you uploaded files and rebuilt the index."
                        )
                        return

                    answer = answer_question(question, chunks)
                    # Save to history for current user
                    save_history(st.session_state["username"], question, answer)
                except Exception as e:
                    st.error(f"Error while answering the question: {e}")
                    return

            st.markdown("---")
            st.markdown(answer)

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


if __name__ == "__main__":
    main()


