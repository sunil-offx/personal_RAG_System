# Study Material Assistant (RAG)

This is a simple **Retrieval-Augmented Generation (RAG)** assistant that answers questions **only from your uploaded study materials** (PDF or text files).

### 1. Install dependencies

In a terminal in this folder:

```bash
pip install -r requirements.txt
```

### 2. Prepare your study materials

- Create a folder named `study_materials` next to `rag_study_assistant.py`.
- Put your files inside it:
  - `.pdf` files
  - `.txt` files

Example:

```text
RAG_projects/
  rag_study_assistant.py
  requirements.txt
  study_materials/
    math_notes.pdf
    physics.txt
```

### 3. Build the index (ingest)

Run:

```bash
python rag_study_assistant.py ingest
```

This will:

- Read all files in `study_materials/`
- Split them into chunks
- Create embeddings
- Save an index file `study_index.pkl`

### 4. Set your Groq API key

The assistant uses a Groq-hosted model (OpenAI-compatible API) to generate answers **only from the retrieved text**.

Set your key in the environment (PowerShell example on Windows):

```powershell
$env:GROQ_API_KEY = "your_groq_api_key_here"
```

Or in `cmd`:

```cmd
set GROQ_API_KEY=your_groq_api_key_here
```

Optional: choose a Groq model (defaults to `llama-3.1-8b-instant`):

```powershell
$env:GROQ_MODEL = "llama-3.1-8b-instant"
```

### 5. Ask questions (chat mode)

Run:

```bash
python rag_study_assistant.py chat
```

Then type your questions. The assistant will:

- Retrieve the most relevant chunks from your materials
- Ask the Groq model to answer **only using those chunks**
- If the answer is not in your materials, it will say it cannot answer from them

### 6. Notes

- If you add or change study materials, run `ingest` again to rebuild the index.
- Answers are strictly grounded in your uploaded notes/books; no outside knowledge is allowed.
