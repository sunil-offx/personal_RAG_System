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
    answer_question_with_citations,
    evaluate_groundedness,
    generate_study_quiz,
    generate_flashcards,
    Chunk
)
import guardrails
import audit_chain
import ragas_evaluator

# ── Firebase / Firestore setup & Resilient Local Storage Fallbacks ────────────
USERS_LOCAL_FILE = "users_local.json"
ADMIN_LOGINS_LOCAL_FILE = "admin_logins_local.json"
ROOMS_LOCAL_FILE = "rooms_local.json"
ADMIN_PROFILES_LOCAL_FILE = "admin_profiles_local.json"
UPLOADS_LOCAL_FILE = "uploads_local.json"
HISTORY_LOCAL_FILE = "history_local.json"
ROOM_VISITS_LOCAL_FILE = "room_visits_local.json"
STUDENT_QUERIES_LOCAL_FILE = "student_queries_local.json"

def _load_json_data(filepath: str, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def _save_json_data(filepath: str, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _load_local_users() -> dict:
    return _load_json_data(USERS_LOCAL_FILE, {})

def _save_local_users(data: dict):
    _save_json_data(USERS_LOCAL_FILE, data)


@st.cache_resource
def get_db():
    """Initialize Firebase app and return Firestore client if explicitly enabled (defaults to local storage)."""
    if os.environ.get("ENABLE_FIRESTORE", "false").lower() not in ("true", "1"):
        return None

    try:
        if not firebase_admin._apps:
            key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase_key.json")
            cred = None
            try:
                if hasattr(st, "secrets") and "FIREBASE_KEY" in st.secrets:
                    key_dict = json.loads(st.secrets["FIREBASE_KEY"])
                    cred = credentials.Certificate(key_dict)
            except Exception:
                pass
            if cred is None and os.path.exists(key_path):
                try:
                    cred = credentials.Certificate(key_path)
                except Exception:
                    cred = None

            if cred:
                firebase_admin.initialize_app(cred)
            else:
                return None
        return firestore.client()
    except Exception as e:
        print(f"Notice: Firestore initialization deferred ({e}). Operating with local storage.")
        return None


def init_storage():
    """Ensure study folder exists."""
    STUDY_DIR.mkdir(exist_ok=True)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.strip().encode()).hexdigest()


def record_admin_login(email: str):
    clean_email = (email or "").strip().lower()
    # 1. Local append
    logins = _load_json_data(ADMIN_LOGINS_LOCAL_FILE, [])
    logins.insert(0, {
        "email": clean_email,
        "login_at": datetime.utcnow().isoformat()
    })
    _save_json_data(ADMIN_LOGINS_LOCAL_FILE, logins[:100])

    # 2. Remote Firestore
    try:
        db = get_db()
        if db:
            db.collection("admin_logins").add({
                "email": clean_email,
                "login_at": datetime.utcnow().isoformat(),
            })
    except Exception:
        pass

    # 3. Cryptographic Audit Chain
    try:
        audit_chain.record_audit_event(clean_email, "ADMIN_LOGIN", {"status": "SUCCESS"})
    except Exception:
        pass


def load_admin_logins():
    # Try Firestore
    try:
        db = get_db()
        if db:
            docs = db.collection("admin_logins").order_by("login_at", direction=firestore.Query.DESCENDING).limit(50).stream()
            items = [
                {
                    "Email": d.to_dict().get("email"),
                    "Login Time": d.to_dict().get("login_at")
                }
                for d in docs
            ]
            if items:
                return items
    except Exception:
        pass

    # Fallback to local
    local_data = _load_json_data(ADMIN_LOGINS_LOCAL_FILE, [])
    return [{"Email": d.get("email"), "Login Time": d.get("login_at")} for d in local_data]


def record_student_query(email: str, room_code: str, question: str):
    """Records student questions for topic analysis and weak area identification."""
    clean_email = (email or "").strip().lower()
    rec = {
        "email": clean_email,
        "room_code": (room_code or "").strip().upper(),
        "question": question.strip(),
        "asked_at": datetime.utcnow().isoformat(),
    }
    queries = _load_json_data(STUDENT_QUERIES_LOCAL_FILE, [])
    queries.append(rec)
    _save_json_data(STUDENT_QUERIES_LOCAL_FILE, queries[-500:])

    try:
        db = get_db()
        if db:
            db.collection("student_queries").add(rec)
    except Exception:
        pass


def load_student_analytics(room_code: str = None, user_email: str = None):
    """Aggregates most frequent topics and identifies repeated inquiries (weak areas)."""
    import re
    from collections import Counter
    clean_code = (room_code or "").strip().upper() if room_code else None
    clean_user = (user_email or "").strip().lower() if user_email else None

    local_queries = _load_json_data(STUDENT_QUERIES_LOCAL_FILE, [])
    queries = []
    for q in local_queries:
        if clean_code and q.get("room_code") != clean_code:
            continue
        if clean_user and q.get("email") != clean_user:
            continue
        queries.append(q)

    try:
        db = get_db()
        if db:
            q = db.collection("student_queries")
            if clean_code:
                q = q.where("room_code", "==", clean_code)
            if clean_user:
                q = q.where("email", "==", clean_user)
            for d in q.stream():
                qd = d.to_dict()
                if qd not in queries:
                    queries.append(qd)
    except Exception:
        pass

    stopwords = {
        "what", "is", "how", "the", "a", "an", "explain", "in", "to", "for", "of",
        "and", "can", "you", "why", "does", "are", "with", "between", "difference",
        "tell", "about", "me", "from", "that", "this", "which"
    }
    words = []
    question_texts = []
    for item in queries:
        text = item.get("question", "").lower()
        question_texts.append(text)
        tokens = re.findall(r"\b[a-zA-Z]{3,}\b", text)
        words.extend([w for w in tokens if w not in stopwords])

    top_topics = Counter(words).most_common(8)
    repeated = [q for q, count in Counter(question_texts).items() if count > 1]

    return {
        "total_queries": len(queries),
        "top_topics": top_topics,
        "repeated_inquiries": repeated[:5],
        "recent_queries": queries[-10:]
    }


def authenticate_user(email: str, password: str):
    clean_email = (email or "").strip().lower()
    clean_password = (password or "").strip()

    admin_email = os.environ.get("ADMIN_EMAIL", "admin").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "myadminsecret").strip()

    if clean_email == admin_email and clean_password == admin_password:
        try:
            record_admin_login(admin_email)
        except Exception:
            pass
        return True, admin_email

    for i in range(1, 9):
        adm_email = (os.environ.get(f"ADMIN_EMAIL_{i}") or "").strip().lower()
        adm_password = (os.environ.get(f"ADMIN_PASSWORD_{i}") or "").strip()
        if adm_email and clean_email == adm_email and clean_password == adm_password:
            try:
                record_admin_login(adm_email)
            except Exception:
                pass
            return True, adm_email

    # Check local users backup first
    local_users = _load_local_users()
    if clean_email in local_users:
        user_record = local_users[clean_email]
        if user_record.get("password_hash") == hash_password(clean_password):
            user_record["last_login"] = datetime.utcnow().isoformat()
            _save_local_users(local_users)
            return True, clean_email

    # Check Firestore users if online
    try:
        db = get_db()
        if db:
            docs = db.collection("users").where("email", "==", clean_email).limit(1).stream()
            for doc in docs:
                data = doc.to_dict()
                if data.get("password_hash") == hash_password(clean_password):
                    try:
                        doc.reference.update({"last_login": datetime.utcnow().isoformat()})
                    except Exception:
                        pass
                    local_users[clean_email] = data
                    _save_local_users(local_users)
                    return True, clean_email
    except Exception as e:
        print(f"Firestore authentication lookup notice: {e}")

    return False, None


def create_user(email: str, password: str) -> bool:
    clean_email = (email or "").strip().lower()
    clean_password = (password or "").strip()
    if not clean_email or not clean_password:
        return False

    local_users = _load_local_users()
    if clean_email in local_users:
        return False

    new_record = {
        "email": clean_email,
        "password_hash": hash_password(clean_password),
        "created_at": datetime.utcnow().isoformat(),
        "last_login": datetime.utcnow().isoformat()
    }

    # Save to Firestore if available
    try:
        db = get_db()
        if db:
            existing = db.collection("users").where("email", "==", clean_email).limit(1).stream()
            for _ in existing:
                return False
            db.collection("users").add(new_record)
    except Exception as e:
        print(f"Firestore create_user notice: {e}")

    # Always persist to local user registry as backup
    local_users[clean_email] = new_record
    _save_local_users(local_users)
    return True


def load_users():
    users_dict = dict(_load_local_users())

    try:
        db = get_db()
        if db:
            docs = db.collection("users").stream()
            for d in docs:
                u = d.to_dict()
                em = u.get("email")
                if em and em not in users_dict:
                    users_dict[em] = u
    except Exception:
        pass

    return [
        {
            "Email": u.get("email"),
            "Sign Up Date": u.get("created_at", "N/A"),
            "Last Login": u.get("last_login", "Never")
        }
        for u in users_dict.values()
    ]


def update_user_profile(email: str, name: str, dob: str, register_number: str):
    clean_email = (email or "").strip().lower()
    local_users = _load_local_users()
    if clean_email in local_users:
        local_users[clean_email].update({
            "name": name,
            "dob": dob,
            "register_number": register_number
        })
        _save_local_users(local_users)

    try:
        db = get_db()
        if db:
            docs = db.collection("users").where("email", "==", clean_email).limit(1).stream()
            for doc in docs:
                doc.reference.update({
                    "name": name,
                    "dob": dob,
                    "register_number": register_number
                })
    except Exception:
        pass


def get_user_profile(email: str):
    clean_email = (email or "").strip().lower()
    local_users = _load_local_users()
    if clean_email in local_users and local_users[clean_email].get("name"):
        rec = local_users[clean_email]
        return {
            "name": rec.get("name", ""),
            "dob": rec.get("dob", ""),
            "register_number": rec.get("register_number", "")
        }

    try:
        db = get_db()
        if db:
            docs = db.collection("users").where("email", "==", clean_email).limit(1).stream()
            for doc in docs:
                data = doc.to_dict()
                return {
                    "name": data.get("name", ""),
                    "dob": data.get("dob", ""),
                    "register_number": data.get("register_number", "")
                }
    except Exception:
        pass

    if clean_email in local_users:
        rec = local_users[clean_email]
        return {
            "name": rec.get("name", ""),
            "dob": rec.get("dob", ""),
            "register_number": rec.get("register_number", "")
        }

    return {"name": "", "dob": "", "register_number": ""}


def set_user_room(email: str, room_code: str):
    clean_email = (email or "").strip().lower()
    clean_code = (room_code or "").strip().upper()
    local_users = _load_local_users()
    if clean_email in local_users:
        local_users[clean_email]["current_room"] = clean_code
        _save_local_users(local_users)

    try:
        db = get_db()
        if db:
            docs = db.collection("users").where("email", "==", clean_email).limit(1).stream()
            for doc in docs:
                doc.reference.update({"current_room": clean_code})
    except Exception:
        pass


def get_room_student_counts(admin_email: str):
    rooms = load_rooms(admin_email)
    room_counts = {r: 0 for r in rooms}

    local_users = _load_local_users()
    for u in local_users.values():
        r = u.get("current_room")
        if r in room_counts:
            room_counts[r] += 1

    try:
        db = get_db()
        if db:
            users = db.collection("users").stream()
            for u in users:
                data = u.to_dict()
                r = data.get("current_room")
                em = data.get("email")
                if em not in local_users and r in room_counts:
                    room_counts[r] += 1
    except Exception:
        pass

    return [{"Room Code": r, "Students Joined": room_counts[r]} for r in rooms]


def record_room_visit(email: str, room_code: str):
    clean_email = (email or "").strip().lower()
    clean_code = (room_code or "").strip().upper()
    visits = _load_json_data(ROOM_VISITS_LOCAL_FILE, [])
    visits.append({
        "email": clean_email,
        "room_code": clean_code,
        "visited_at": datetime.utcnow().isoformat()
    })
    _save_json_data(ROOM_VISITS_LOCAL_FILE, visits[-1000:])

    try:
        db = get_db()
        if db:
            db.collection("room_visits").add({
                "email": clean_email,
                "room_code": clean_code,
                "visited_at": datetime.utcnow().isoformat()
            })
    except Exception:
        pass


def get_user_joined_rooms(email: str):
    clean_email = (email or "").strip().lower()
    visits = _load_json_data(ROOM_VISITS_LOCAL_FILE, [])
    rooms = set()
    for v in visits:
        if v.get("email") == clean_email and v.get("room_code"):
            rooms.add(v.get("room_code"))

    try:
        db = get_db()
        if db:
            docs = db.collection("room_visits").where("email", "==", clean_email).stream()
            for d in docs:
                r = d.to_dict().get("room_code")
                if r:
                    rooms.add(r)
    except Exception:
        pass
    return sorted(list(rooms))


def get_room_visits_data(admin_email: str, timeframe: str):
    import pandas as pd
    rooms = load_rooms(admin_email)
    if not rooms:
        return pd.DataFrame()

    data = []
    local_visits = _load_json_data(ROOM_VISITS_LOCAL_FILE, [])
    for v in local_visits:
        if v.get("room_code") in rooms:
            data.append(v)

    try:
        db = get_db()
        if db:
            docs = db.collection("room_visits").stream()
            for d in docs:
                v = d.to_dict()
                if v.get("room_code") in rooms and v not in data:
                    data.append(v)
    except Exception:
        pass

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["visited_at"] = pd.to_datetime(df["visited_at"])

    if timeframe == "Daily":
        df["period"] = df["visited_at"].dt.strftime("%Y-%m-%d")
    elif timeframe == "Weekly":
        df["period"] = df["visited_at"].dt.strftime("%G-W%V")
    else:  # Monthly
        df["period"] = df["visited_at"].dt.strftime("%Y-%m")

    grouped = df.groupby(["period", "room_code"]).size().reset_index(name="count")
    pivot_df = grouped.pivot(index="period", columns="room_code", values="count").fillna(0)
    return pivot_df


def update_admin_profile(email: str, name: str):
    clean_email = (email or "").strip().lower()
    profiles = _load_json_data(ADMIN_PROFILES_LOCAL_FILE, {})
    profiles[clean_email] = {"name": name}
    _save_json_data(ADMIN_PROFILES_LOCAL_FILE, profiles)

    try:
        db = get_db()
        if db:
            docs = db.collection("admins").where("email", "==", clean_email).limit(1).stream()
            found = False
            for doc in docs:
                doc.reference.update({"name": name})
                found = True
            if not found:
                db.collection("admins").add({"email": clean_email, "name": name})
    except Exception:
        pass


def get_admin_profile(email: str):
    clean_email = (email or "").strip().lower()
    profiles = _load_json_data(ADMIN_PROFILES_LOCAL_FILE, {})
    if clean_email in profiles and profiles[clean_email].get("name"):
        return profiles[clean_email].get("name")

    try:
        db = get_db()
        if db:
            docs = db.collection("admins").where("email", "==", clean_email).limit(1).stream()
            for doc in docs:
                return doc.to_dict().get("name", "")
    except Exception:
        pass
    return ""


def create_room(room_code: str, created_by: str):
    clean_code = (room_code or "").strip().upper()
    clean_by = (created_by or "").strip().lower()
    if not clean_code:
        return False

    rooms_data = _load_json_data(ROOMS_LOCAL_FILE, {})
    if clean_code in rooms_data:
        return False

    room_record = {
        "room_code": clean_code,
        "created_by": clean_by,
        "created_at": datetime.utcnow().isoformat()
    }
    rooms_data[clean_code] = room_record
    _save_json_data(ROOMS_LOCAL_FILE, rooms_data)

    room_dir = STUDY_DIR / clean_code
    room_dir.mkdir(parents=True, exist_ok=True)

    try:
        db = get_db()
        if db:
            existing = db.collection("rooms").where("room_code", "==", clean_code).limit(1).stream()
            for _ in existing:
                pass
            db.collection("rooms").add(room_record)
    except Exception:
        pass

    try:
        audit_chain.record_audit_event(clean_by, "CREATE_ROOM", {"room_code": clean_code})
    except Exception:
        pass
    return True


def delete_room_db(room_code: str):
    clean_code = (room_code or "").strip().upper()
    rooms_data = _load_json_data(ROOMS_LOCAL_FILE, {})
    if clean_code in rooms_data:
        del rooms_data[clean_code]
        _save_json_data(ROOMS_LOCAL_FILE, rooms_data)

    try:
        db = get_db()
        if db:
            docs = db.collection("rooms").where("room_code", "==", clean_code).stream()
            for d in docs:
                d.reference.delete()
    except Exception:
        pass

    try:
        audit_chain.record_audit_event(
            st.session_state.get("username", "admin"),
            "DELETE_ROOM",
            {"room_code": clean_code}
        )
    except Exception:
        pass


def load_rooms(admin_email: str = None):
    clean_admin = (admin_email or "").strip().lower() if admin_email else None
    rooms_data = _load_json_data(ROOMS_LOCAL_FILE, {})

    # Auto-discover any directories in STUDY_DIR as existing rooms
    if STUDY_DIR.exists():
        for d in STUDY_DIR.iterdir():
            if d.is_dir() and d.name not in rooms_data:
                rooms_data[d.name] = {
                    "room_code": d.name,
                    "created_by": clean_admin or "admin",
                    "created_at": datetime.utcnow().isoformat()
                }
        _save_json_data(ROOMS_LOCAL_FILE, rooms_data)

    try:
        db = get_db()
        if db:
            query = db.collection("rooms")
            if clean_admin:
                query = query.where("created_by", "==", clean_admin)
            for d in query.stream():
                rd = d.to_dict()
                rc = rd.get("room_code")
                if rc and rc not in rooms_data:
                    rooms_data[rc] = rd
    except Exception:
        pass

    filtered = []
    for r in rooms_data.values():
        if clean_admin and r.get("created_by") and r.get("created_by") != clean_admin:
            continue
        filtered.append(r)

    filtered.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return [r.get("room_code") for r in filtered if r.get("room_code")]


def check_room_exists(room_code: str):
    clean_code = (room_code or "").strip().upper()
    if not clean_code:
        return False
    rooms_data = _load_json_data(ROOMS_LOCAL_FILE, {})
    if clean_code in rooms_data:
        return True
    if (STUDY_DIR / clean_code).is_dir():
        return True

    try:
        db = get_db()
        if db:
            docs = db.collection("rooms").where("room_code", "==", clean_code).limit(1).stream()
            for _ in docs:
                return True
    except Exception:
        pass
    return False


def get_room_creator(room_code: str) -> str:
    clean_code = (room_code or "").strip().upper()
    rooms_data = _load_json_data(ROOMS_LOCAL_FILE, {})
    if clean_code in rooms_data:
        creator_email = rooms_data[clean_code].get("created_by", "")
        if creator_email:
            admin_name = get_admin_profile(creator_email)
            return f"{admin_name} ({creator_email})" if admin_name else creator_email

    try:
        db = get_db()
        if db:
            docs = db.collection("rooms").where("room_code", "==", clean_code).limit(1).stream()
            for doc in docs:
                creator_email = doc.to_dict().get("created_by", "")
                if creator_email:
                    admin_name = get_admin_profile(creator_email)
                    return f"{admin_name} ({creator_email})" if admin_name else creator_email
    except Exception:
        pass
    return "Admin"


def record_upload(filename: str, original_name: str, size_bytes: int):
    rec = {
        "filename": filename,
        "original_name": original_name,
        "size_bytes": size_bytes,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    uploads = _load_json_data(UPLOADS_LOCAL_FILE, [])
    uploads.insert(0, rec)
    _save_json_data(UPLOADS_LOCAL_FILE, uploads[:200])

    try:
        db = get_db()
        if db:
            db.collection("uploads").add(rec)
    except Exception:
        pass


def load_uploads():
    local_uploads = _load_json_data(UPLOADS_LOCAL_FILE, [])
    try:
        db = get_db()
        if db:
            docs = db.collection("uploads").order_by(
                "uploaded_at", direction=firestore.Query.DESCENDING
            ).stream()
            items = [
                (d.to_dict().get("filename"), d.to_dict().get("original_name"),
                 d.to_dict().get("size_bytes", 0), d.to_dict().get("uploaded_at"))
                for d in docs
            ]
            if items:
                return items
    except Exception:
        pass
    return [
        (d.get("filename"), d.get("original_name"), d.get("size_bytes", 0), d.get("uploaded_at"))
        for d in local_uploads
    ]


def save_history(username: str, question: str, answer: str):
    import uuid
    clean_user = (username or "").strip().lower()
    rec = {
        "id": str(uuid.uuid4())[:8],
        "username": clean_user,
        "question": question,
        "answer": answer,
        "pinned": 0,
        "created_at": datetime.utcnow().isoformat(),
    }
    history_list = _load_json_data(HISTORY_LOCAL_FILE, [])
    history_list.append(rec)
    _save_json_data(HISTORY_LOCAL_FILE, history_list[-500:])

    try:
        db = get_db()
        if db:
            db.collection("history").add(rec)
    except Exception:
        pass


def load_history(username: str):
    clean_user = (username or "").strip().lower()
    history_list = _load_json_data(HISTORY_LOCAL_FILE, [])
    rows = []
    for d in history_list:
        if d.get("username") == clean_user:
            rows.append((d.get("id"), d.get("question"), d.get("answer"),
                         d.get("pinned", 0), d.get("created_at")))

    try:
        db = get_db()
        if db:
            docs = db.collection("history").where("username", "==", clean_user).stream()
            for d in docs:
                data = d.to_dict()
                row = (d.id, data.get("question"), data.get("answer"),
                       data.get("pinned", 0), data.get("created_at"))
                if row not in rows:
                    rows.append(row)
    except Exception:
        pass

    rows.sort(key=lambda r: (-r[3], r[4]))
    return rows


def clear_history(username: str):
    clean_user = (username or "").strip().lower()
    history_list = _load_json_data(HISTORY_LOCAL_FILE, [])
    history_list = [h for h in history_list if h.get("username") != clean_user]
    _save_json_data(HISTORY_LOCAL_FILE, history_list)

    try:
        db = get_db()
        if db:
            docs = db.collection("history").where("username", "==", clean_user).stream()
            for d in docs:
                d.reference.delete()
    except Exception:
        pass


def set_pin(history_id: str, pinned: bool):
    history_list = _load_json_data(HISTORY_LOCAL_FILE, [])
    for h in history_list:
        if h.get("id") == history_id:
            h["pinned"] = 1 if pinned else 0
    _save_json_data(HISTORY_LOCAL_FILE, history_list)

    try:
        db = get_db()
        if db:
            db.collection("history").document(history_id).update(
                {"pinned": 1 if pinned else 0}
            )
    except Exception:
        pass


def get_cookie_manager():
    if "cookie_manager" not in st.session_state:
        try:
            import extra_streamlit_components as stx
            st.session_state["cookie_manager"] = stx.CookieManager(key="auth")
        except Exception as e:
            st.session_state["cookie_manager"] = None
    return st.session_state.get("cookie_manager")


def check_is_admin(user_email):
    if not user_email:
        return False
    clean_user = str(user_email).strip().lower()
    admin_email = os.environ.get("ADMIN_EMAIL", "admin").strip().lower()
    if clean_user == admin_email:
        return True
    for i in range(1, 9):
        adm = (os.environ.get(f"ADMIN_EMAIL_{i}") or "").strip().lower()
        if adm and clean_user == adm:
            return True
    return False


def ensure_user_signed_in():
    """Centered login gate — Sign Up default, Sign In & Admin as links below."""
    now = datetime.utcnow()
    
    username = st.session_state.get("username")
    if not username:
        try:
            cm = get_cookie_manager()
            if cm:
                cookie_username = cm.get("auth_username")
                if cookie_username:
                    st.session_state["username"] = cookie_username
                    st.session_state["is_admin"] = check_is_admin(cookie_username)
                    st.session_state["signed_in_expires"] = now + timedelta(days=30)
                    st.rerun()
        except Exception:
            pass

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
                        try:
                            cm = get_cookie_manager()
                            if cm:
                                if remember_me:
                                    cm.set("auth_username", user_email, expires_at=now + timedelta(days=30))
                                else:
                                    cm.delete("auth_username")
                        except Exception:
                            pass
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

    base_options = [
        "Manage Rooms",
        "Retrieval Quality (RAGAS)",
        "Security Audit Trail",
        "Student Analytics",
        "Room Reports",
        "Admin Profile"
    ]
    base_icons = [
        'house',
        'graph-up-arrow',
        'shield-check',
        'mortarboard',
        'bar-chart',
        'person'
    ]

    if is_super_admin:
        menu_options = [
            "Manage Rooms",
            "Retrieval Quality (RAGAS)",
            "Security Audit Trail",
            "Student Analytics",
            "Room Reports",
            "All Users",
            "All Uploads",
            "Admin Login History",
            "Admin Profile"
        ]
        menu_icons = [
            'house',
            'graph-up-arrow',
            'shield-check',
            'mortarboard',
            'bar-chart',
            'people',
            'cloud-upload',
            'clock-history',
            'person'
        ]
    else:
        menu_options = base_options
        menu_icons = base_icons

    with st.sidebar:
        menu = option_menu(
            "Admin Panel", 
            menu_options,
            icons=menu_icons,
            menu_icon="cast", 
            default_index=0,
        )

    if st.sidebar.button("Log out"):
        try:
            cm = get_cookie_manager()
            if cm:
                cm.delete("auth_username")
        except Exception:
            pass
        st.session_state.clear()
        st.rerun()

    if menu == "Manage Rooms":
        st.title("Manage Study Rooms")
        st.write("Create rooms, upload materials, scan for security guardrails, and build AI memory.")
        
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
                        audit_chain.record_audit_event(admin_email, "REMOVE_FILE", {"room_code": selected_room, "filename": fname})
                        st.rerun()
            
            st.subheader("Upload New Materials")
            uploaded_files = st.file_uploader(
                "Upload PDFs or Text files", 
                type=["pdf", "txt"], 
                accept_multiple_files=True,
                key="admin_upload"
            )
            
            if st.button("Upload to Room", type="primary") and uploaded_files:
                with st.spinner("Scanning security guardrails & uploading in real time..."):
                    uploaded_names = []
                    security_warnings = []
                    for f in uploaded_files:
                        file_bytes = f.getvalue()
                        if f.name.endswith(".txt"):
                            txt_content = file_bytes.decode("utf-8", errors="ignore")
                            has_warn, warns = guardrails.scan_document_for_injections(txt_content)
                            if has_warn:
                                security_warnings.extend([f"[{f.name}] {w}" for w in warns])
                        supabase_storage_util.upload_room_file(selected_room, f.name, file_bytes)
                        record_upload(f.name, f.name, len(file_bytes))
                        uploaded_names.append(f.name)

                    if security_warnings:
                        st.warning("⚠️ Security Guardrail Notice: Suspicious prompt injection patterns detected:\n" + "\n".join(security_warnings[:3]))

                    audit_chain.record_audit_event(
                        admin_email,
                        "UPLOAD_MATERIALS",
                        {"room_code": selected_room, "files": uploaded_names}
                    )
                    success, msg = rebuild_room_index(selected_room)
                        
                if success:
                    st.success("Uploaded & indexed in real time!")
                else:
                    st.warning(f"Uploaded files, but indexing notice: {msg}")
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
                    audit_chain.record_audit_event(admin_email, "REBUILD_INDEX", {"room_code": selected_room})
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
                    except Exception:
                        pass
                    delete_room_db(selected_room)
                st.success(f"Room '{selected_room}' deleted successfully!")
                st.rerun()

    elif menu == "Retrieval Quality (RAGAS)":
        st.title("🎯 Retrieval Quality Dashboard (RAGAS)")
        st.write(
            "Monitors retrieval and generation quality per room against golden Q&A benchmarks. "
            "Evaluates Faithfulness, Answer Relevance, Context Precision, and Context Recall."
        )
        rooms = load_rooms(admin_email=st.session_state["username"])
        if not rooms:
            st.info("No study rooms available to evaluate. Please create a room and ingest materials first.")
            return

        col1, col2 = st.columns([2, 2])
        with col1:
            eval_room = st.selectbox("Select Study Room", rooms, key="ragas_eval_room")
        with col2:
            eval_mode = st.radio("Retrieval Mode to Benchmark", ["hybrid", "dense"], horizontal=True)

        if st.button("🚀 Run RAGAS Quality Evaluation", type="primary"):
            with st.spinner(f"Evaluating room '{eval_room}' using {eval_mode} retrieval..."):
                try:
                    summary = ragas_evaluator.run_evaluation(eval_room, mode=eval_mode)
                    audit_chain.record_audit_event(
                        admin_email,
                        "RUN_RAGAS_EVALUATION",
                        {"room_code": eval_room, "mode": eval_mode, "score": summary["overall_score"]}
                    )
                    st.success(f"Evaluation finished! Overall RAGAS Score: {summary['overall_score'] * 100:.1f}%")
                except Exception as e:
                    st.error(f"Evaluation encountered an issue: {e}")

        st.markdown("---")
        st.subheader("Latest Quality Metrics")
        history = ragas_evaluator.load_eval_history(room_code=eval_room)
        if history:
            latest = history[0]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Faithfulness (Groundedness)", f"{latest.get('faithfulness', 0) * 100:.1f}%")
            m2.metric("Answer Relevance", f"{latest.get('answer_relevance', 0) * 100:.1f}%")
            m3.metric("Context Precision", f"{latest.get('context_precision', 0) * 100:.1f}%")
            m4.metric("Context Recall", f"{latest.get('context_recall', 0) * 100:.1f}%")

            st.write(f"**Overall RAG Quality Index:** `{latest.get('overall_score', 0) * 100:.1f}%` (Evaluated on {latest.get('timestamp', '')[:16]})")

            with st.expander("🔍 Inspect Detailed Q&A Evaluation Results"):
                for idx, item in enumerate(latest.get("details", []), start=1):
                    st.markdown(f"**Q{idx}: {item.get('question')}**")
                    st.caption(f"Faithfulness: {item.get('faithfulness', 0)*100:.0f}% | Relevance: {item.get('answer_relevance', 0)*100:.0f}% | Precision: {item.get('context_precision', 0)*100:.0f}% | Recall: {item.get('context_recall', 0)*100:.0f}%")
                    st.write(item.get("answer"))
                    st.markdown("---")

            st.subheader("Evaluation History")
            hist_rows = []
            for h in history:
                hist_rows.append({
                    "Timestamp": h.get("timestamp", "")[:16],
                    "Mode": h.get("mode", "hybrid"),
                    "Faithfulness": f"{h.get('faithfulness', 0)*100:.1f}%",
                    "Relevance": f"{h.get('answer_relevance', 0)*100:.1f}%",
                    "Precision": f"{h.get('context_precision', 0)*100:.1f}%",
                    "Recall": f"{h.get('context_recall', 0)*100:.1f}%",
                    "Overall": f"{h.get('overall_score', 0)*100:.1f}%"
                })
            st.table(hist_rows)
        else:
            st.info("No evaluation runs recorded yet for this room. Click 'Run RAGAS Quality Evaluation' above.")

    elif menu == "Security Audit Trail":
        st.title("🛡️ Tamper-Evident Security Audit Trail")
        st.write(
            "Every administrative action is stored in an immutable SHA-256 cryptographic hash chain. "
            "Any modification or tampering with previous events breaks the chain link and is detected instantly."
        )

        col_v1, col_v2 = st.columns([2, 1])
        with col_v1:
            if st.button("🔐 Verify Cryptographic Chain Integrity", type="primary"):
                ver = audit_chain.verify_chain_integrity()
                if ver["is_valid"]:
                    st.success(ver["message"])
                else:
                    st.error(ver["message"])
        with col_v2:
            st.caption("Algorithm: SHA-256 Chained Hash Blocks")

        chain = audit_chain.get_audit_chain()
        if chain:
            st.markdown("---")
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Chain Blocks", len(chain))
            c2.metric("Genesis Linked", "Yes (64-byte zeros)")
            c3.metric("Latest Block #", chain[-1].get("index", 0))

            st.subheader("Chronological Audit Trail")
            table_data = []
            for b in reversed(chain):
                table_data.append({
                    "Block #": b.get("index"),
                    "Timestamp (UTC)": b.get("timestamp", "")[:19],
                    "Admin / Actor": b.get("admin"),
                    "Action": b.get("action"),
                    "Details": str(b.get("details", ""))[:45],
                    "SHA-256 Hash": b.get("hash", "")[:16] + "..."
                })
            st.table(table_data)
        else:
            st.info("No audit events logged yet. Genesis block will be minted on the next admin action.")

    elif menu == "Student Analytics":
        st.title("📊 Student Learning Analytics")
        st.write(
            "Analyzes student questions to uncover frequently queried concepts, conceptual sticking points, "
            "and inferred weak areas based on repeated or rephrased queries."
        )
        rooms = load_rooms(admin_email=st.session_state["username"])
        selected_ana_room = st.selectbox("Filter by Room", ["All Rooms"] + rooms)
        room_filter = None if selected_ana_room == "All Rooms" else selected_ana_room

        analytics = load_student_analytics(room_code=room_filter)

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Student Questions", analytics["total_queries"])
        col2.metric("Frequent Core Topics", len(analytics["top_topics"]))
        col3.metric("Detected Weak Areas", len(analytics["repeated_inquiries"]))

        st.markdown("---")
        st.subheader("🔥 Top Asked Topics & Key Terms")
        if analytics["top_topics"]:
            import pandas as pd
            df_topics = pd.DataFrame(analytics["top_topics"], columns=["Topic / Keyword", "Frequency"])
            st.bar_chart(df_topics.set_index("Topic / Keyword"))
        else:
            st.write("No student queries recorded yet for this selection.")

        st.subheader("⚠️ Inferred Weak Areas (Repeated Questions)")
        if analytics["repeated_inquiries"]:
            st.write("Students have repeatedly asked about these topics, indicating concepts that need more emphasis:")
            for req in analytics["repeated_inquiries"]:
                st.warning(f"📌 '{req}'")
        else:
            st.info("No repeated weak-point questions detected yet.")

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
        try:
            cm = get_cookie_manager()
            if cm:
                cm.delete("auth_username")
        except Exception:
            pass
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
            retrieval_mode = st.sidebar.selectbox(
                "Retrieval Engine",
                ["Hybrid (BM25 + Dense + RRF)", "Dense (Semantic Vector)"],
                index=0,
                help="Toggle live between hybrid sparse-dense fusion or pure dense vector search."
            )
            compare_mode = st.sidebar.toggle(
                "Multi-Doc Compare Mode",
                value=False,
                help="Compare and contrast answers across different textbook sources."
            )
            if st.sidebar.button("Leave Room"):
                st.session_state["current_room"] = None
                st.session_state["messages"] = []
                set_user_room(st.session_state["username"], "")
                st.rerun()

            st.subheader(f"Room: {current_room}")

            tab_chat, tab_practice, tab_my_analytics = st.tabs(
                ["💬 Interactive Study Chat", "📝 Practice & Quizzes", "📊 My Learning Analytics"]
            )

            with tab_chat:
                if "messages" not in st.session_state:
                    st.session_state.messages = []

                for i, message in enumerate(st.session_state.messages):
                    with st.chat_message(message["role"]):
                        if message["role"] == "assistant" and message.get("groundedness"):
                            g = message["groundedness"]
                            score = g.get("score", 85)
                            color = "#10B981" if score >= 75 else ("#F59E0B" if score >= 50 else "#EF4444")
                            bg_color = "rgba(16, 185, 129, 0.15)" if score >= 75 else ("rgba(245, 158, 11, 0.15)" if score >= 50 else "rgba(239, 68, 68, 0.15)")
                            badge_html = f"""
                            <div style="display: inline-block; background: {bg_color}; border: 1px solid {color}; border-radius: 6px; padding: 2px 8px; margin-bottom: 8px; font-size: 0.8rem; font-weight: 600; color: {color};">
                                Groundedness: {g.get('label')}
                            </div>
                            """
                            st.markdown(badge_html, unsafe_allow_html=True)

                        st.markdown(message["content"])

                        if message["role"] == "assistant" and message.get("sources"):
                            sources = message["sources"]
                            with st.expander(f"📚 Sources & Evidence Used ({len(sources)} citations)"):
                                for s in sources:
                                    p_str = f", Page {s['page']}" if s.get("page") else ""
                                    st.markdown(f"**[{s['index']}] {s['source']}{p_str}**")
                                    st.caption(f"\"{s['snippet']}\"")

                if prompt := st.chat_input("Ask a question about this room's materials..."):
                    user_email = st.session_state["username"]

                    # 1. Rate Limiting Check
                    allowed, retry_after = guardrails.global_rate_limiter.is_allowed(user_email)
                    if not allowed:
                        st.error(f"⏳ Rate limit reached. Please wait {retry_after} seconds before asking your next question.")
                        st.stop()

                    # 2. Prompt Injection Guardrail Check
                    is_injected, reason = guardrails.check_prompt_injection(prompt)
                    if is_injected:
                        st.error(f"🚨 Security Guardrail Alert: {reason}. Request blocked for safety.")
                        try:
                            audit_chain.record_audit_event(
                                "SYSTEM_GUARDRAIL",
                                "PROMPT_INJECTION_BLOCKED",
                                {"user": user_email, "room": current_room, "reason": reason}
                            )
                        except Exception:
                            pass
                        st.stop()

                    st.session_state.messages.append({"role": "user", "content": prompt})
                    with st.chat_message("user"):
                        st.markdown(prompt)

                    with st.chat_message("assistant"):
                        with st.spinner("Retrieving notes & preparing explanation..."):
                            try:
                                mode_arg = "hybrid" if "Hybrid" in retrieval_mode else "dense"
                                chunks = []
                                try:
                                    chunks = retrieve(prompt, k=5, room_code=current_room, mode=mode_arg)
                                except Exception as e:
                                    print(f"Retrieval error: {e}")

                                res = answer_question_with_citations(
                                    prompt, chunks, comparison_mode=compare_mode
                                )
                                answer = res["answer"]
                                sources = res.get("sources", [])
                                groundedness = res.get("groundedness", {})

                                # Render Groundedness Badge
                                score = groundedness.get("score", 85)
                                color = "#10B981" if score >= 75 else ("#F59E0B" if score >= 50 else "#EF4444")
                                bg_color = "rgba(16, 185, 129, 0.15)" if score >= 75 else ("rgba(245, 158, 11, 0.15)" if score >= 50 else "rgba(239, 68, 68, 0.15)")
                                badge_html = f"""
                                <div style="display: inline-block; background: {bg_color}; border: 1px solid {color}; border-radius: 6px; padding: 2px 8px; margin-bottom: 8px; font-size: 0.8rem; font-weight: 600; color: {color};">
                                    Groundedness: {groundedness.get('label')}
                                </div>
                                """
                                st.markdown(badge_html, unsafe_allow_html=True)
                                st.markdown(answer)

                                if sources:
                                    with st.expander(f"📚 Sources & Evidence Used ({len(sources)} citations)"):
                                        for s in sources:
                                            p_str = f", Page {s['page']}" if s.get("page") else ""
                                            st.markdown(f"**[{s['index']}] {s['source']}{p_str}**")
                                            st.caption(f"\"{s['snippet']}\"")

                                save_history(user_email, prompt, answer)
                                record_student_query(user_email, current_room, prompt)

                                st.session_state.messages.append({
                                    "role": "assistant",
                                    "content": answer,
                                    "sources": sources,
                                    "groundedness": groundedness
                                })
                            except Exception as e:
                                st.error(f"Error: {e}")

            with tab_practice:
                st.subheader("Interactive Recall & Knowledge Testing")
                st.write("Reinforce your memory with auto-generated flashcards and interactive quizzes.")

                sub_flash, sub_quiz = st.tabs(["🗂️ Spaced-Repetition Flashcards", "🎯 Practice Quiz"])

                with sub_flash:
                    if st.button("✨ Generate Flashcards from Ingested Notes", key=f"btn_flash_{current_room}"):
                        with st.spinner("Synthesizing key definitions and concepts..."):
                            cards = generate_flashcards(current_room, count=6)
                            st.session_state[f"flashcards_{current_room}"] = cards

                    cards = st.session_state.get(f"flashcards_{current_room}", [])
                    if cards:
                        st.write(f"Generated {len(cards)} study flashcards:")
                        cols = st.columns(2)
                        for idx, card in enumerate(cards):
                            with cols[idx % 2]:
                                with st.container():
                                    st.markdown(f"**Card #{idx+1}: {card.get('front')}**")
                                    with st.expander("👁️ Reveal Answer & Explanation"):
                                        st.info(card.get("back"))
                                        st.caption(f"Source: {card.get('source', 'Room notes')}")
                                    st.markdown("---")
                    else:
                        st.info("Click the button above to generate study flashcards from your uploaded materials.")

                with sub_quiz:
                    if st.button("🚀 Generate 5-Question Multiple Choice Quiz", key=f"btn_quiz_{current_room}"):
                        with st.spinner("Formulating multiple-choice questions from notes..."):
                            questions = generate_study_quiz(current_room, count=5)
                            st.session_state[f"quiz_{current_room}"] = questions
                            st.session_state[f"quiz_submitted_{current_room}"] = False

                    questions = st.session_state.get(f"quiz_{current_room}", [])
                    if questions:
                        st.write(f"**Practice Quiz ({len(questions)} Questions):**")
                        user_answers = {}
                        for idx, q in enumerate(questions):
                            st.markdown(f"**Question {idx+1}: {q.get('question')}**")
                            opts = q.get("options", [])
                            ans = st.radio(
                                f"Select an option for Q{idx+1}:",
                                opts,
                                key=f"q_{current_room}_{idx}",
                                index=None
                            )
                            user_answers[idx] = ans
                            st.write("")

                        if st.button("📝 Submit Quiz Answers", type="primary"):
                            score = 0
                            st.session_state[f"quiz_submitted_{current_room}"] = True
                            st.markdown("---")
                            st.subheader("Quiz Results & Explanations")
                            for idx, q in enumerate(questions):
                                correct_idx = q.get("correct_index", 0)
                                correct_opt = q.get("options", [""])[correct_idx] if correct_idx < len(q.get("options", [])) else ""
                                selected = user_answers.get(idx)

                                if selected == correct_opt:
                                    score += 1
                                    st.success(f"✅ **Q{idx+1} Correct!** You selected: {selected}")
                                else:
                                    st.error(f"❌ **Q{idx+1} Incorrect.** You selected: {selected or 'No answer'}. Correct answer: **{correct_opt}**")
                                st.caption(f"💡 Explanation: {q.get('explanation')}")

                            pct = (score / len(questions)) * 100
                            st.metric("Your Final Score", f"{score} / {len(questions)} ({pct:.0f}%)")
                            if pct >= 80:
                                st.balloons()
                    else:
                        st.info("Click 'Generate 5-Question Multiple Choice Quiz' above to test your understanding.")

            with tab_my_analytics:
                st.subheader("My Study Progress in this Room")
                my_analytics = load_student_analytics(
                    room_code=current_room,
                    user_email=st.session_state["username"]
                )

                c1, c2, c3 = st.columns(3)
                c1.metric("My Total Questions", my_analytics["total_queries"])
                c2.metric("Topics Queried", len(my_analytics["top_topics"]))
                c3.metric("Review Points", len(my_analytics["repeated_inquiries"]))

                st.markdown("---")
                st.subheader("📚 My Most Frequent Topics")
                if my_analytics["top_topics"]:
                    import pandas as pd
                    df_topics = pd.DataFrame(my_analytics["top_topics"], columns=["Concept", "Count"])
                    st.bar_chart(df_topics.set_index("Concept"))
                else:
                    st.info("You haven't asked questions in this room yet. Ask questions in the Study Chat to see your personalized learning profile!")

                if my_analytics["repeated_inquiries"]:
                    st.subheader("⚠️ Concepts You Asked About Multiple Times")
                    st.write("Consider reviewing these topics or generating flashcards to strengthen mastery:")
                    for q_text in my_analytics["repeated_inquiries"]:
                        st.warning(f"📌 '{q_text}'")

if __name__ == "__main__":
    main()


