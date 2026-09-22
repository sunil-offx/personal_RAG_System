import argparse
import os
import pickle
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

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


class BM25Index:
    """
    Self-contained Okapi BM25 implementation for sparse lexical retrieval.
    Requires zero extra C-extensions or external wheels.
    """
    def __init__(self, chunks: List[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.corpus_size = len(chunks)
        self.doc_lens = []
        self.doc_freqs = {}
        self.tokenized_docs = []

        for c in chunks:
            tokens = self._tokenize(c.text)
            self.tokenized_docs.append(tokens)
            self.doc_lens.append(len(tokens))
            for t in set(tokens):
                self.doc_freqs[t] = self.doc_freqs.get(t, 0) + 1

        self.avgdl = (sum(self.doc_lens) / max(self.corpus_size, 1)) if self.corpus_size > 0 else 1.0

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\w+", text.lower())

    def score(self, query: str) -> np.ndarray:
        q_tokens = self._tokenize(query)
        scores = np.zeros(self.corpus_size, dtype=np.float32)
        if self.corpus_size == 0 or not q_tokens:
            return scores

        for t in q_tokens:
            if t not in self.doc_freqs:
                continue
            df = self.doc_freqs[t]
            idf = np.log((self.corpus_size - df + 0.5) / (df + 0.5) + 1.0)
            for i, doc in enumerate(self.tokenized_docs):
                if not doc:
                    continue
                tf = doc.count(t)
                if tf > 0:
                    numerator = tf * (self.k1 + 1)
                    denom = tf + self.k1 * (1 - self.b + self.b * (self.doc_lens[i] / max(self.avgdl, 1e-5)))
                    scores[i] += idf * (numerator / max(denom, 1e-5))
        return scores


def reciprocal_rank_fusion(
    dense_ranks: List[int],
    bm25_ranks: List[int],
    k: int = 60
) -> List[Tuple[int, float]]:
    """
    Reciprocal Rank Fusion (RRF) combining dense and sparse rankings.
    Formula: RRF_score(d) = 1/(k + rank_dense) + 1/(k + rank_bm25)
    """
    scores: dict[int, float] = {}
    for rank, idx in enumerate(dense_ranks):
        scores[idx] = scores.get(idx, 0.0) + (1.0 / (k + rank + 1))
    for rank, idx in enumerate(bm25_ranks):
        scores[idx] = scores.get(idx, 0.0) + (1.0 / (k + rank + 1))

    sorted_candidates = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_candidates


def _load_room_index_data(room_code: str = "default") -> Tuple[np.ndarray, List[Chunk]]:
    """Helper to load embeddings and chunks from local or Supabase backup."""
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

    # Real-time auto-ingest if still missing
    if not index_path.exists() and room_code != "default":
        try:
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
            f"No index found for room '{room_code}'. Please upload materials first."
        )

    with open(index_path, "rb") as f:
        index_data = pickle.load(f)

    return index_data["embeddings"], index_data["chunks"]


def retrieve(
    query: str,
    k: int = 5,
    room_code: str = "default",
    mode: str = "hybrid"
) -> List[Chunk]:
    """
    Retrieves top-k chunks using either:
    - 'dense': Dense cosine similarity
    - 'hybrid': Reciprocal Rank Fusion of Dense + BM25 sparse retrieval
    """
    if not room_code:
        room_code = "default"

    embedder = get_embedder()
    query_emb = embedder.encode([query], convert_to_numpy=True)[0]

    # If pure dense retrieval is requested and Supabase pgvector works, try that first
    if mode == "dense":
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
                return [Chunk(text=r["content"], source=r["source"], page=r.get("page")) for r in res.data]
        except Exception:
            pass

    # Load local chunks and embeddings for dense + BM25 ranking
    embeddings, chunks = _load_room_index_data(room_code)
    if not chunks:
        return []

    # 1. Dense similarity scores
    dense_sims = cosine_similarity([query_emb], embeddings)[0]
    dense_top_indices = list(np.argsort(dense_sims)[::-1])

    if mode == "dense":
        top_chunks: List[Chunk] = []
        for idx in dense_top_indices[:k]:
            if dense_sims[idx] > 0:
                top_chunks.append(chunks[int(idx)])
        return top_chunks

    # 2. Sparse BM25 scores
    bm25 = BM25Index(chunks)
    bm25_scores = bm25.score(query)
    bm25_top_indices = list(np.argsort(bm25_scores)[::-1])

    # 3. Reciprocal Rank Fusion
    rrf_ranked = reciprocal_rank_fusion(
        dense_ranks=dense_top_indices[: min(30, len(chunks))],
        bm25_ranks=bm25_top_indices[: min(30, len(chunks))],
        k=60
    )

    top_chunks = [chunks[idx] for idx, _ in rrf_ranked[:k]]
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
    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)


def evaluate_groundedness(answer: str, chunks: List[Chunk]) -> Dict[str, Any]:
    """
    Computes answer-to-context semantic grounding using sentence cosine similarities.
    Returns score (0-100%), classification label, and rationale.
    """
    if not chunks or not answer or not answer.strip():
        return {
            "score": 50,
            "label": "Partially Grounded ⚠️",
            "reasoning": "Answer generated with limited or no retrieved context excerpts."
        }

    embedder = get_embedder()
    chunk_texts = [c.text for c in chunks]
    chunk_embs = embedder.encode(chunk_texts, convert_to_numpy=True)

    # Split answer into meaningful sentences
    sentences = [
        s.strip() for s in re.split(r'(?<=[.!?])\s+', answer)
        if len(s.strip()) > 20 and not s.strip().startswith("#")
    ]
    if not sentences:
        return {
            "score": 85,
            "label": "Grounded ✅",
            "reasoning": "Concise factual statement aligned with source materials."
        }

    sentence_embs = embedder.encode(sentences, convert_to_numpy=True)
    sim_matrix = cosine_similarity(sentence_embs, chunk_embs)
    max_sims_per_sentence = np.max(sim_matrix, axis=1)

    # Threshold for a sentence being grounded in at least one chunk
    grounded_count = sum(1 for sim in max_sims_per_sentence if sim >= 0.50)
    avg_max_sim = float(np.mean(max_sims_per_sentence))
    grounded_ratio = grounded_count / len(sentences)

    # Composite score 0 - 100
    composite_score = int(round((0.6 * grounded_ratio + 0.4 * min(avg_max_sim / 0.75, 1.0)) * 100))
    composite_score = min(max(composite_score, 10), 100)

    if composite_score >= 75:
        label = f"Grounded ✅ ({composite_score}%)"
        reasoning = f"{grounded_count}/{len(sentences)} key assertions directly match retrieved materials."
    elif composite_score >= 50:
        label = f"Partially Grounded ⚠️ ({composite_score}%)"
        reasoning = "Some concepts are supported by study materials; others rely on broader tutor knowledge."
    else:
        label = f"Possibly Ungrounded 🚨 ({composite_score}%)"
        reasoning = "Low direct alignment with uploaded study excerpts. Verify independently."

    return {
        "score": composite_score,
        "label": label,
        "reasoning": reasoning
    }


def answer_question(question: str, chunks: List[Chunk], comparison_mode: bool = False) -> str:
    """Answers question with inline citations ([1], [2])."""
    result = answer_question_with_citations(question, chunks, comparison_mode=comparison_mode)
    return result["answer"]


def answer_question_with_citations(
    question: str,
    chunks: List[Chunk],
    comparison_mode: bool = False
) -> Dict[str, Any]:
    """
    Generates answer with inline bracketed citations and returns structured sources and groundedness score.
    """
    context = build_context(chunks)
    client = get_groq_client()

    comparison_instructions = ""
    if comparison_mode:
        comparison_instructions = (
            "\n\nMULTI-DOCUMENT COMPARISON MODE ACTIVE:\n"
            "The student has enabled Multi-Document Comparison. Structure your response to explicitly compare "
            "and contrast how the different source documents address the topic. Mention the document names and cite "
            "each perspective clearly (e.g. 'According to [Document A] [1]..., whereas [Document B] [2] explains...'). "
            "Highlight agreements, contradictions, or unique insights per document."
        )

    if context:
        system_prompt = (
            "You are a friendly, conversational, and highly intelligent study tutor. "
            "Your goal is to explain concepts clearly, breaking them down so they are easy to understand. "
            "CITATION INSTRUCTIONS:\n"
            "- Whenever stating facts, definitions, or steps derived from the study material, cite the excerpt inline using bracketed numbers like [1], [2] immediately following the statement.\n"
            "- If an excerpt does not contain the answer, feel free to complement with accurate general knowledge, but clearly distinguish it.\n"
            "- Structure your answers using clean Markdown with headings, bullet points, and bold text."
            f"{comparison_instructions}"
        )

        user_prompt = (
            f"Study material excerpts:\n\n{context}\n\n"
            f"Question: {question}\n\n"
            "Explain the answer clearly with inline citations [1], [2] where appropriate. Speak directly to me like a supportive tutor."
        )
    else:
        system_prompt = (
            "You are a friendly, conversational, and highly intelligent study tutor. "
            "Explain concepts clearly using clean Markdown formatting."
        )
        user_prompt = f"Question: {question}\n\nExplain the answer clearly like a tutor."

    completion = client.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )

    answer_text = completion.choices[0].message.content.strip()

    # Build sources metadata
    sources = []
    for i, ch in enumerate(chunks, start=1):
        sources.append({
            "index": i,
            "source": ch.source,
            "page": ch.page,
            "snippet": ch.text[:280] + ("..." if len(ch.text) > 280 else ""),
            "full_text": ch.text
        })

    groundedness = evaluate_groundedness(answer_text, chunks)

    return {
        "answer": answer_text,
        "sources": sources,
        "groundedness": groundedness
    }


def generate_study_quiz(room_code: str, count: int = 5) -> List[Dict[str, Any]]:
    """Generates an interactive multiple-choice quiz grounded in the room's materials."""
    try:
        chunks = retrieve("fundamental principles concepts definitions", k=6, room_code=room_code)
    except Exception:
        chunks = []

    if not chunks:
        return []

    client = get_groq_client()
    context = "\n\n".join([f"Excerpt: {c.text[:400]}" for c in chunks])
    prompt = f"""
Based on the following study excerpts, generate a high-quality {count}-question multiple choice practice quiz.
For each question, provide 4 distinct choices, the 0-indexed correct choice index, and a clear explanation.

Study Excerpts:
{context}

Respond ONLY with a JSON array in this exact schema:
[
  {{
    "question": "Question text here?",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct_index": 0,
    "explanation": "Why this answer is correct based on the text."
  }}
]
"""
    try:
        completion = client.chat.completions.create(
            model=GROQ_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        data = json.loads(completion.choices[0].message.content)
        questions = data if isinstance(data, list) else data.get("questions", data.get("quiz", []))
        return questions
    except Exception as e:
        print(f"Quiz generation error: {e}")
        return []


def generate_flashcards(room_code: str, count: int = 6) -> List[Dict[str, str]]:
    """Generates spaced-repetition flashcards from room study notes."""
    try:
        chunks = retrieve("core terms definitions mechanisms", k=6, room_code=room_code)
    except Exception:
        chunks = []

    if not chunks:
        return []

    client = get_groq_client()
    context = "\n\n".join([f"[{c.source}]: {c.text[:400]}" for c in chunks])
    prompt = f"""
Generate {count} flashcards summarizing essential definitions, formulas, or concepts from these study notes.

Notes:
{context}

Respond ONLY with a JSON array of objects:
[
  {{
    "front": "Key Term or Question",
    "back": "Clear, concise definition or breakdown",
    "source": "Document name or topic"
  }}
]
"""
    try:
        completion = client.chat.completions.create(
            model=GROQ_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        data = json.loads(completion.choices[0].message.content)
        cards = data if isinstance(data, list) else data.get("flashcards", data.get("cards", []))
        return cards
    except Exception as e:
        print(f"Flashcard generation error: {e}")
        return []


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
            chunks = retrieve(question, k=5, room_code=room_code, mode="hybrid")
            res = answer_question_with_citations(question, chunks)
            answer = res["answer"]
            groundedness = res["groundedness"]
        except Exception as e:
            print(f"Error: {e}")
            continue

        print(f"\nAssistant [{groundedness['label']}]:\n")
        print(answer)
        print("\n--- Sources Used ---")
        for s in res.get("sources", []):
            page_info = f", Page {s['page']}" if s['page'] else ""
            print(f"[{s['index']}] {s['source']}{page_info}: {s['snippet'][:120]}...")
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


