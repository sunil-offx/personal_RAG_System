import argparse
import os
import pickle
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
import nest_asyncio
from llama_parse import LlamaParse
from openai import OpenAI
from dotenv import load_dotenv
import re

load_dotenv()

STUDY_DIR = Path(os.environ.get("STUDY_DIR", Path(__file__).parent / "study_materials"))
EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_CHAT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")


@dataclass
class Chunk:
    text: str
    source: str
    page: int | None = None


def load_study_files(study_dir: Path = None) -> List[Path]:
    if study_dir is None:
        study_dir = STUDY_DIR
        
    if not study_dir.exists():
        raise FileNotFoundError(
            f"Study materials folder '{study_dir}' does not exist. "
            f"Create it next to this script and put your PDFs or .txt files inside."
        )

    files: List[Path] = []
    for ext in ("*.pdf", "*.txt"):
        files.extend(study_dir.glob(ext))

    if not files:
        raise FileNotFoundError(
            f"No study files found in '{study_dir}'. "
            f"Add some .pdf or .txt files and run ingest again."
        )
    return files


def read_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_pdf(path: Path) -> List[Tuple[int, str]]:
    
    api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
    try:
        import streamlit as st
        if not api_key and hasattr(st, "secrets") and "LLAMA_CLOUD_API_KEY" in st.secrets:
            api_key = st.secrets["LLAMA_CLOUD_API_KEY"]
    except Exception:
        pass
        
    pages: List[Tuple[int, str]] = []
    
    if api_key:
        try:
            print(f"Parsing {path.name} with LlamaParse...")
            parser = LlamaParse(
                api_key=api_key,
                result_type="markdown",
                premium_mode=True,
                verbose=True
            )
            
            documents = parser.load_data(str(path))
            
            for i, doc in enumerate(documents):
                if doc.text and doc.text.strip():
                    pages.append((i + 1, doc.text))
        except Exception as e:
            print(f"LlamaParse failed for {path.name}: {e}. Falling back to PyPDF2...")
            try:
                import streamlit as st
                st.warning(f"LlamaParse OCR failed for {path.name}: {e}. Falling back to basic text extraction (which may not work for scanned images).")
            except Exception:
                pass
            pages = []
            
    if not pages:
        print(f"Parsing {path.name} with PyPDF2/pypdf (Fallback)...")
        try:
            try:
                import PyPDF2
            except ImportError:
                import pypdf as PyPDF2
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for i in range(len(reader.pages)):
                    page = reader.pages[i]
                    text = page.extract_text()
                    if text and text.strip():
                        pages.append((i + 1, text))
        except Exception as e:
            print(f"PyPDF2/pypdf failed for {path.name}: {e}")
            
    return pages


def chunk_text(text: str, max_tokens: int = 512, overlap: int = 128) -> List[str]:
    # Sentence-aware chunking
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if not sentences:
        words = text.split()
        if not words:
            return []
        chunks = []
        start = 0
        step = max_tokens - overlap
        while start < len(words):
            chunks.append(" ".join(words[start:start + max_tokens]))
            start += step
        return chunks

    chunks = []
    current_chunk = []
    current_length = 0
    
    i = 0
    while i < len(sentences):
        sentence = sentences[i]
        sentence_len = len(sentence.split())
        
        if current_length + sentence_len > max_tokens and current_chunk:
            chunks.append(" ".join(current_chunk))
            
            # Calculate overlap
            overlap_words = 0
            overlap_sentences = []
            for s in reversed(current_chunk):
                s_len = len(s.split())
                if overlap_words + s_len <= overlap:
                    overlap_sentences.insert(0, s)
                    overlap_words += s_len
                else:
                    break
            
            current_chunk = overlap_sentences
            current_length = sum(len(s.split()) for s in current_chunk)
        
        current_chunk.append(sentence)
        current_length += sentence_len
        i += 1
        
    if current_chunk:
        chunks.append(" ".join(current_chunk))
        
    return chunks


def build_chunks(study_dir: Path = None) -> List[Chunk]:
    files = load_study_files(study_dir)
    chunks: List[Chunk] = []

    for f in files:
        if f.suffix.lower() == ".txt":
            text = read_txt(f)
            for ch in chunk_text(text):
                if ch.strip():
                    chunks.append(Chunk(text=ch, source=f.name, page=None))
        elif f.suffix.lower() == ".pdf":
            for page_num, page_text in read_pdf(f):
                for ch in chunk_text(page_text):
                    if ch.strip():
                        chunks.append(Chunk(text=ch, source=f.name, page=page_num))
    if not chunks:
        raise RuntimeError("No usable text could be extracted from the study materials.")

    return chunks


def get_embedder() -> SentenceTransformer:
    return SentenceTransformer(EMBED_MODEL_NAME)


def ingest(study_dir: Path = None, room_code: str = "default") -> None:
    if study_dir is None:
        study_dir = STUDY_DIR
    if not room_code:
        room_code = "default"

    print(f"Loading study materials from '{study_dir}' for room '{room_code}'...")
    chunks = build_chunks(study_dir)
    print(f"Found {len(chunks)} text chunks. Computing embeddings...")

    embedder = get_embedder()
    texts = [c.text for c in chunks]
    embeddings = embedder.encode(texts, show_progress_bar=True, convert_to_numpy=True)

    # 1. Save local pickle file (.pkl) if local disk is writable
    try:
        index_filename = f"{room_code}_index.pkl" if room_code != "default" else "study_index.pkl"
        index_path = Path(__file__).parent / index_filename
        index_data = {
            "embeddings": embeddings.astype(np.float32),
            "chunks": chunks,
            "embed_model": EMBED_MODEL_NAME,
            "room_code": room_code
        }
        with open(index_path, "wb") as f:
            pickle.dump(index_data, f)
        print(f"Local index saved to '.pkl' file at '{index_path}'.")

        # 1b. Backup .pkl index file to Supabase storage for cloud persistence
        try:
            from supabase_storage_util import upload_room_file
            with open(index_path, "rb") as f:
                upload_room_file(room_code, index_filename, f.read())
            print(f"Index pickle file '{index_filename}' backed up to Supabase storage.")
        except Exception as e:
            print(f"Note: Cloud pickle backup skipped ({e}). Operating in pure cloud vector mode.")
    except Exception as e:
        print(f"Note: Local disk pickle file write skipped ({e}). Operating in pure cloud vector mode.")

    # 2. Sync to Supabase pgvector if configured
    try:
        from supabase_storage_util import get_supabase
        client = get_supabase()
        
        # Delete existing vectors for this room before re-inserting
        client.table("documents").delete().eq("room_code", room_code).execute()
        
        batch_data = []
        for chunk, emb in zip(chunks, embeddings):
            batch_data.append({
                "room_code": room_code,
                "source": chunk.source,
                "page": chunk.page,
                "content": chunk.text,
                "embedding": emb.tolist()
            })
            
        # Insert in batches
        for i in range(0, len(batch_data), 100):
            client.table("documents").insert(batch_data[i:i+100]).execute()

        print(f"Index synced successfully to Supabase pgvector for room '{room_code}'.")
    except Exception as e:
        print(f"Note: Supabase database sync skipped ({e}). Operating in local .pkl mode.")


def retrieve(query: str, k: int = 5, room_code: str = "default") -> List[Chunk]:
    if not room_code:
        room_code = "default"

    embedder = get_embedder()
    query_emb = embedder.encode([query], convert_to_numpy=True)[0]

    # 1. Try Supabase pgvector retrieval first
    try:
        from supabase_storage_util import get_supabase
        client = get_supabase()
        
        res = client.rpc(
            "match_documents",
            {
                "query_embedding": query_emb.tolist(),
                "match_threshold": 0.0,
                "match_count": k,
                "room_code_filter": room_code
            }
        ).execute()
        
        if res.data:
            top_chunks = []
            for row in res.data:
                top_chunks.append(Chunk(text=row["content"], source=row["source"], page=row["page"]))
            return top_chunks
    except Exception as e:
        pass  # Fall back to local pickle file search below

    # 2. Fallback to local .pkl file vector search
    index_filename = f"{room_code}_index.pkl" if room_code != "default" else "study_index.pkl"
    index_path = Path(__file__).parent / index_filename
    
    # If local index is missing, attempt to restore from Supabase Storage
    if not index_path.exists() and room_code != "default":
        try:
            from supabase_storage_util import download_room_file
            content = download_room_file(room_code, index_filename, silent=True)
            if content:
                with open(index_path, "wb") as f:
                    f.write(content)
                print(f"Restored index pickle file '{index_filename}' from Supabase storage.")
        except Exception as e:
            print(f"Could not restore index pickle file from Supabase storage: {e}")

    if not index_path.exists():
        index_path = Path(__file__).parent / "study_index.pkl"

    # 3. Real-Time On-Demand Auto-Ingest: If index STILL does not exist, build it in real-time from room files!
    if not index_path.exists() and room_code != "default":
        try:
            print(f"No existing .pkl index found for room '{room_code}'. Initiating real-time index generation...")
            import tempfile
            from supabase_storage_util import download_all_room_files
            with tempfile.TemporaryDirectory() as tmpdir:
                local_dir = Path(tmpdir)
                downloaded = download_all_room_files(room_code, str(local_dir))
                if downloaded:
                    ingest(study_dir=local_dir, room_code=room_code)
                    target_pkl = Path(__file__).parent / index_filename
                    if target_pkl.exists():
                        index_path = target_pkl
        except Exception as e:
            print(f"Real-time index generation failed: {e}")

    if not index_path.exists():
        raise FileNotFoundError(
            f"No index found in Supabase pgvector or local pickle file '{index_path}'. "
            f"Please ensure study materials are uploaded to room '{room_code}'."
        )

    with open(index_path, "rb") as f:
        index_data = pickle.load(f)

    embeddings: np.ndarray = index_data["embeddings"]
    chunks: List[Chunk] = index_data["chunks"]

    sims = cosine_similarity([query_emb], embeddings)[0]
    top_indices = np.argsort(sims)[::-1][:k]

    top_chunks: List[Chunk] = []
    for idx in top_indices:
        if sims[idx] <= 0:
            continue
        top_chunks.append(chunks[int(idx)])
    return top_chunks


def build_context(chunks: List[Chunk]) -> str:
    if not chunks:
        return ""

    parts: List[str] = []
    for i, ch in enumerate(chunks, start=1):
        location = f"{ch.source}"
        if ch.page is not None:
            location += f", page {ch.page}"
        parts.append(f"[{i}] From {location}:\n{ch.text}")
    return "\n\n".join(parts)


def get_groq_client() -> OpenAI:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY environment variable is not set. "
            "Please add your Groq API key to the .env file in the project directory."
        )
    # Groq provides an OpenAI-compatible API.
    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)


def answer_question(question: str, chunks: List[Chunk]) -> str:
    context = build_context(chunks)

    client = get_groq_client()

    if context:
        system_prompt = (
            "You are a friendly, conversational, and highly intelligent study tutor. "
            "Your goal is to explain concepts clearly, breaking them down so they are easy to understand, as if you are having a one-on-one conversation with a student. "
            "You have access to some study material excerpts. If they are helpful, use them to answer the question. "
            "If the excerpts do not contain the answer, use your general knowledge to answer the question accurately. "
            "Structure your answers to be highly readable and well-formatted using Markdown (headings, bullet points, italics, bold text). "
            "Speak directly to the user (e.g., 'Sure, let's break that down!', 'Here's how it works:'). "
            "Do NOT mention the names of the source files, filenames, or page numbers in your response."
        )

        user_prompt = (
            f"Study material excerpts:\n\n{context}\n\n"
            f"Question: {question}\n\n"
            "Explain the answer in a clear, conversational, and easy-to-understand way. Speak directly to me like a tutor. Do NOT mention which study material or page was used."
        )
    else:
        system_prompt = (
            "You are a friendly, conversational, and highly intelligent study tutor. "
            "Your goal is to explain concepts clearly, breaking them down so they are easy to understand, as if you are having a one-on-one conversation with a student. "
            "Structure your answers to be highly readable and well-formatted using Markdown. "
            "Speak directly to the user."
        )

        user_prompt = (
            f"Question: {question}\n\n"
            "Explain the answer in a clear, conversational, and easy-to-understand way. Speak directly to me like a tutor."
        )

    completion = client.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )

    return completion.choices[0].message.content.strip()


def chat_loop() -> None:
    print("Study Material Assistant (RAG)")
    room_code = input("Enter room code to chat with: ").strip()
    if not room_code:
        print("Room code is required.")
        return
        
    print("Type your question, or 'exit' to quit.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            print("Goodbye.")
            break

        try:
            chunks = retrieve(question, k=5, room_code=room_code)
            answer = answer_question(question, chunks)
        except Exception as e:
            print(f"Error: {e}")
            continue

        print("\nAssistant:\n")
        print(answer)
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAG-based Study Material Assistant"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ingest", help="Index the study materials")
    subparsers.add_parser("chat", help="Ask questions about the study materials")

    args = parser.parse_args()
    if args.command == "ingest":
        room_code = input("Enter room code: ").strip()
        ingest(study_dir=STUDY_DIR, room_code=room_code)
    elif args.command == "chat":
        chat_loop()


if __name__ == "__main__":
    main()

