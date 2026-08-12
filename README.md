# Study Material Assistant (RAG)

A lightweight **Retrieval-Augmented Generation (RAG)** assistant that answers questions strictly from your own study materials (PDF or plain text), rather than from the general knowledge of a language model. Retrieval is powered by a Supabase `pgvector` index, and answer generation uses a Groq-hosted, OpenAI-compatible language model.

## What This Project Does

Large language models are powerful, but left on their own they answer from general training knowledge — which may be outdated, generic, or simply not aligned with a specific textbook, lecture, or set of notes. This project addresses that limitation by combining two components:

- **Retrieval**: a search step that finds the most relevant passages from *your* materials for a given question.
- **Generation**: a language model that composes an answer, but is constrained to use only the retrieved passages as its source of truth.

This pattern is known as **Retrieval-Augmented Generation (RAG)**. The practical benefit for studying is that answers are traceable back to your own materials, and the assistant will say so explicitly if a question falls outside what you've provided, rather than guessing or fabricating an answer.

## How It Works

### 1. Ingestion (indexing your materials)

- Files in the `study_materials` folder are read and their text extracted (including PDF parsing).
- Each document is split into smaller **chunks** — manageable segments of text (e.g., a paragraph or a few sentences) rather than whole files. Chunking matters because language models can only reason effectively over a limited amount of text at once, and smaller chunks allow more precise retrieval.
- Each chunk is converted into an **embedding**: a numerical vector representation that captures its semantic meaning. Text with similar meaning ends up with vectors that are close together in this vector space.
- These embeddings, along with the original chunk text, are stored in a **Supabase** database table (`documents`) using the **pgvector** extension, which allows efficient similarity search directly inside PostgreSQL.

### 2. Chat (answering questions)

- When a question is asked, it is embedded using the same embedding method used during ingestion.
- The system performs a **similarity search** against the stored vectors in Supabase to retrieve the chunks most semantically related to the question.
- These retrieved chunks are inserted into a prompt sent to the Groq-hosted language model, along with an instruction to answer *only* using that retrieved context.
- The model returns an answer. If the retrieved chunks don't contain enough relevant information, the assistant explicitly states that it cannot answer from the provided materials, rather than falling back on outside knowledge.

## Architecture at a Glance

```text
                ┌───────────────────────┐
                │   study_materials/     │
                │   (.pdf, .txt files)   │
                └───────────┬────────────┘
                            │  ingest
                            ▼
                ┌───────────────────────┐
                │  Chunking + Embedding  │
                └───────────┬────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │  Supabase (pgvector)   │
                │  "documents" table     │
                └───────────┬────────────┘
                            │  similarity search
                            ▼
   question ──▶ ┌───────────────────────┐
                │   Retrieved context    │
                └───────────┬────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │  Groq LLM (answer      │
                │  generation, grounded  │
                │  in retrieved chunks)  │
                └───────────┬────────────┘
                            │
                            ▼
                       final answer
```

## Key Components

| Component | Role |
|---|---|
| `study_materials/` | Source folder for your own PDFs and text notes |
| Chunking | Breaks documents into retrievable, right-sized pieces of text |
| Embeddings | Numeric representations used to measure semantic similarity |
| Supabase + pgvector | Stores chunks and their embeddings; performs similarity search |
| Groq LLM | Generates the final answer, constrained to retrieved context |
| Ingestion mode | One-time (or repeatable) step to build/update the index |
| Chat mode | Interactive question-answering session |

## Prerequisites

- Python 3.9+
- A Supabase project with the `pgvector` extension enabled
- A Groq API key
- Project dependencies installed from `requirements.txt`

## Project Structure

Study materials should be placed in a `study_materials` folder alongside the main script:

```text
RAG_projects/
├── rag_study_assistant.py
├── requirements.txt
└── study_materials/
    ├── math_notes.pdf
    └── physics.txt
```

Supported file types: `.pdf`, `.txt`

## Configuration

A Groq API key must be set as an environment variable before running the assistant. By default, the assistant uses the `llama-3.1-8b-instant` model; an alternative Groq model can be specified via a separate environment variable if desired.

## Usage

### Building the Index

The ingestion step reads all files in `study_materials/`, splits them into chunks, generates embeddings, and writes them to the `documents` table in the Supabase `pgvector` database. This step needs to be (re-)run any time the contents of `study_materials/` change.

### Chat Session

The chat mode launches an interactive question-answering session grounded in your indexed materials. For each question, the assistant will:

- Retrieve the most relevant chunks from your study materials
- Prompt the Groq model to answer using only that retrieved context
- Indicate explicitly when a question cannot be answered from the provided materials

## Why RAG Instead of Just Asking the Model Directly?

- **Grounded answers**: responses are based on the specific materials you provide, not generic or potentially outdated knowledge.
- **Reduced hallucination**: the model is instructed to answer only from retrieved context, and to say so when it can't.
- **Personalization**: the same underlying model can answer very differently depending on whose notes or textbooks it's been pointed at.
- **Transparency**: because answers come from identifiable chunks, it's possible to trace a given answer back to its source material.

## Limitations

- Answer quality depends on the quality and coverage of the uploaded materials — the assistant cannot answer questions about topics not present in `study_materials/`.
- Very large or poorly formatted PDFs (e.g., scanned images without OCR) may not extract cleanly and could reduce retrieval accuracy.
- Retrieval is based on semantic similarity, so oddly phrased or highly ambiguous questions may retrieve less relevant chunks.

## Maintenance

The index should be rebuilt whenever study materials are added, removed, or modified, to ensure the assistant's responses reflect the current contents of `study_materials/`.

## Notes

- Answers are strictly grounded in the ingested materials; the model does not draw on outside knowledge.
- All retrieval and generation occurs at query time, so the index should always reflect the current contents of `study_materials/`.