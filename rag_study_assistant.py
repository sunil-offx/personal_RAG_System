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

load_dotenv()

STUDY_DIR = Path(os.environ.get("STUDY_DIR", Path(__file__).parent / "study_materials"))
INDEX_PATH = Path(os.environ.get("INDEX_PATH", Path(__file__).parent / "study_index.pkl"))
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
        
    if not api_key:
        raise ValueError("LLAMA_CLOUD_API_KEY is missing. Please add it to Streamlit Secrets or .env file.")
        
    print(f"Parsing {path.name} with LlamaParse...")
    parser = LlamaParse(
        api_key=api_key,
        result_type="markdown",
        verbose=True
    )
    
    documents = parser.load_data(str(path))
    
    pages: List[Tuple[int, str]] = []
    for i, doc in enumerate(documents):
        pages.append((i + 1, doc.text))
        
    return pages


def chunk_text(text: str, max_tokens: int = 512, overlap: int = 128) -> List[str]:
    # Very rough token approximation using words
    words = text.split()
    if not words:
        return []

    chunks: List[str] = []
    start = 0
    step = max_tokens - overlap
    while start < len(words):
        end = start + max_tokens
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))
        start += step
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


def ingest(study_dir: Path = None, index_path: Path = None) -> None:
    if study_dir is None:
        study_dir = STUDY_DIR
    if index_path is None:
        index_path = INDEX_PATH
        
    print(f"Loading study materials from '{study_dir}'...")
    chunks = build_chunks(study_dir)
    print(f"Found {len(chunks)} text chunks. Computing embeddings...")

    embedder = get_embedder()
    texts = [c.text for c in chunks]
    embeddings = embedder.encode(texts, show_progress_bar=True, convert_to_numpy=True)

    index_data = {
        "embeddings": embeddings.astype(np.float32),
        "chunks": chunks,
        "embed_model": EMBED_MODEL_NAME,
    }

    with index_path.open("wb") as f:
        pickle.dump(index_data, f)

    print(f"Index saved to '{index_path}'. You can now run in chat mode.")


def load_index(index_path: Path = None):
    if index_path is None:
        index_path = INDEX_PATH
        
    if not index_path.exists():
        raise FileNotFoundError(
            f"Index file '{index_path}' not found. Run with 'ingest' first."
        )
    with index_path.open("rb") as f:
        index_data = pickle.load(f)

    if index_data.get("embed_model") != EMBED_MODEL_NAME:
        print(
            "Warning: index was built with a different embedding model. "
            "Consider re-running ingest."
        )
    return index_data


def retrieve(query: str, k: int = 5, allowed_sources: Optional[List[str]] = None, index_path: Path = None) -> List[Chunk]:
    index = load_index(index_path)
    embeddings: np.ndarray = index["embeddings"]
    chunks: List[Chunk] = index["chunks"]

    embedder = get_embedder()
    query_emb = embedder.encode([query], convert_to_numpy=True)

    sims = cosine_similarity(query_emb, embeddings)[0]
    top_indices = np.argsort(sims)[::-1]

    top_chunks: List[Chunk] = []
    for idx in top_indices:
        if sims[idx] <= 0:
            continue
            
        chunk = chunks[int(idx)]
        if allowed_sources is not None and chunk.source not in allowed_sources:
            continue
            
        top_chunks.append(chunk)
        if len(top_chunks) >= k:
            break
            
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
    if not context:
        return "the study material not found"

    client = get_groq_client()

    system_prompt = (
        "You are a friendly, conversational, and highly intelligent study tutor. "
        "Your goal is to explain concepts clearly, breaking them down so they are easy to understand, as if you are having a one-on-one conversation with a student. "
        "You must base your explanations ONLY on the provided study material excerpts. "
        "Structure your answers to be highly readable and well-formatted using Markdown (headings, bullet points, italics, bold text). "
        "Speak directly to the user (e.g., 'Sure, let's break that down!', 'Here's how it works:'). "
        "Do NOT mention the names of the source files, filenames, or page numbers in your response. "
        "If the answer to the question is not clearly supported by the material, explicitly state EXACTLY: "
        "'the study material not found'. "
        "Do not use outside knowledge."
    )

    user_prompt = (
        f"Study material excerpts:\n\n{context}\n\n"
        f"Question: {question}\n\n"
        "Explain the answer in a clear, conversational, and easy-to-understand way, using only the excerpts above. Speak directly to me like a tutor. Do NOT mention which study material or page was used. If it cannot be answered from the excerpts, output EXACTLY 'the study material not found'."
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
            chunks = retrieve(question, k=5)
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
        ingest()
    elif args.command == "chat":
        chat_loop()


if __name__ == "__main__":
    main()

