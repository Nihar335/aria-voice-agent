"""
rag.py - RAG = Retrieval-Augmented Generation.

Problem: the LLM does not know Fortivex's prices or FAQ. If we ask it,
it will guess (hallucinate).
Solution: before the LLM answers, we FIND the matching paragraphs in our
own documents and give them to the LLM with the instruction
"answer only from these". We also keep the file name, so we can show the source.

Steps:
  1. CHUNK  - cut every document into small pieces (one "## section" each).
  2. EMBED  - turn every piece into a list of numbers (a vector) that
              represents its MEANING. Similar meaning -> similar numbers.
  3. STORE  - save the vectors in a file, so we only do this once.
  4. SEARCH - embed the customer's question the same way and pick the
              pieces whose vectors are closest (cosine similarity).
"""
import hashlib
import json
import math
import re
import threading
from collections import Counter
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import numpy as np

import config


# A piece must be at least this similar to the question to count as relevant.
# Below this, we tell the agent "no info found" instead of letting it guess.
MIN_SCORE_EMBED = 0.55
MIN_SCORE_KEYWORD = 0.12


# ---------------------------------------------------------------------------
# Two separate collections. This split is the security boundary:
#   public   -> the voice agent may read these and say them to customers
#   internal -> confidential. Only the admin page can search them. The agent
#               never gets them, so it CANNOT leak them (an LLM can't reveal
#               text that was never put in its prompt).
# ---------------------------------------------------------------------------
SCOPES = {
    "public": config.KNOWLEDGE_DIR,
    "internal": config.INTERNAL_KNOWLEDGE_DIR,
}
DOC_EXTENSIONS = (".md", ".txt")
MAX_CHUNK_CHARS = 1200


# ---------------------------------------------------------------------------
# Step 1: CHUNK
# ---------------------------------------------------------------------------
def _split_plain(text: str) -> list[str]:
    """For text without '## ' headings (e.g. an uploaded PDF): group paragraphs into ~1200-char pieces."""
    pieces, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{para}".strip()
        while len(current) > MAX_CHUNK_CHARS * 1.5:     # one giant paragraph -> hard cut
            pieces.append(current[:MAX_CHUNK_CHARS])
            current = current[MAX_CHUNK_CHARS:]
    if current:
        pieces.append(current)
    return pieces


def list_documents(scope: str = "public") -> list:
    folder = SCOPES[scope]
    return sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in DOC_EXTENSIONS)


def load_chunks(scope: str = "public") -> list[dict]:
    """Read every document and cut it at each '## ' heading (or into paragraphs if it has none)."""
    chunks = []
    for path in list_documents(scope):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        first = text.splitlines()[0]
        # First line "# Title" is the document title; otherwise use the file name.
        title = first.lstrip("# ").strip() if first.startswith("# ") else path.stem.replace("_", " ")
        sections = [s.strip() for s in re.split(r"\n(?=## )", text) if s.strip().startswith("## ")]
        if sections:
            for section in sections:
                heading = section.splitlines()[0].lstrip("# ").strip()
                chunks.append({
                    "id": f"{path.stem}#{len(chunks)}",
                    "source": f"{path.name} > {heading}",   # shown to the user as the source
                    # Put the title + heading inside the text so the search knows the context.
                    "text": f"{title} - {section.lstrip('# ')}",
                })
        else:
            for n, piece in enumerate(_split_plain(text), 1):
                chunks.append({
                    "id": f"{path.stem}#{len(chunks)}",
                    "source": f"{path.name} > part {n}",
                    "text": f"{title} - {piece}",
                })
    return chunks


# ---------------------------------------------------------------------------
# Step 2: EMBED  (Gemini embeddings, with a free offline backup)
# ---------------------------------------------------------------------------
_client = None
_pool = ThreadPoolExecutor(max_workers=4)
QUERY_TIME_LIMIT = 1.2   # seconds we are willing to wait for a query embedding on a live call


_cache: dict[str, np.ndarray] = {}   # question -> vector (same question again = 0 ms)
_slow_until = 0.0                     # "circuit breaker": skip embeddings until this time


def _embed_query(question: str) -> np.ndarray:
    """
    Embed the customer's question, but never keep a live call waiting:
      - already seen this question?  -> use the saved vector (0 ms)
      - embeddings were slow recently? -> skip them for 60 s (circuit breaker)
      - otherwise wait at most QUERY_TIME_LIMIT seconds, else give up (keyword search is used)
    A slow answer that arrives late is still saved, so the next identical question is instant.
    """
    global _slow_until
    if question in _cache:
        return _cache[question]
    if time.time() < _slow_until:
        raise TimeoutError("embeddings were slow recently - using keyword search for now")

    def save(job):
        if job.exception() is None:
            _cache[question] = job.result()[0]

    job = _pool.submit(_gemini_embed, [question], "RETRIEVAL_QUERY")
    job.add_done_callback(save)
    try:
        return job.result(timeout=QUERY_TIME_LIMIT)[0]
    except FutureTimeout:
        _slow_until = time.time() + 60      # open the circuit for one minute
        raise TimeoutError(f"embedding slower than {QUERY_TIME_LIMIT}s")


def _gemini_embed(texts: list[str], task: str) -> np.ndarray:
    """Ask Gemini to turn texts into meaning-vectors."""
    from google import genai
    from google.genai import types

    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)   # create once, reuse (faster)
    result = _client.models.embed_content(
        model=config.GEMINI_EMBED_MODEL,
        contents=texts,
        # DOCUMENT for stored pieces, QUERY for the customer's question.
        config=types.EmbedContentConfig(task_type=task),
    )
    vectors = np.array([e.values for e in result.embeddings], dtype=np.float32)
    # Make every vector length 1, so "dot product" = cosine similarity.
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


# Very common words carry no meaning for search, so we ignore them.
STOPWORDS = set("""a an the is are was were be to of in on for and or with at by from as it this that
what which who how when where why do does did can could will would should i you we they he she my your
our me us about any have has had there here not no yes so if then than also just like get""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


class KeywordIndex:
    """
    Backup search with NO internet and NO API key (TF-IDF).
    Words that are rare across documents (like "refund") count more
    than common words (like "the"). Works only on matching words, not meaning.
    """

    def __init__(self, texts: list[str]):
        self.docs = [Counter(_words(t)) for t in texts]
        n = len(texts)
        df = Counter(w for d in self.docs for w in d)            # in how many docs each word appears
        self.idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}
        self.vecs = [self._vec(d) for d in self.docs]

    def _vec(self, counts: Counter) -> dict:
        v = {w: c * self.idf.get(w, 0) for w, c in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / norm for w, x in v.items()}

    def scores(self, query: str) -> list[float]:
        q = self._vec(Counter(_words(query)))
        return [sum(q[w] * d.get(w, 0) for w in q) for d in self.vecs]


# ---------------------------------------------------------------------------
# Step 3: STORE  (build once, reuse until the documents change)
# ---------------------------------------------------------------------------
class KnowledgeBase:
    def __init__(self, scope: str = "public"):
        self.scope = scope
        self.index_file = config.DATA_DIR / ("rag_index.json" if scope == "public" else f"rag_index_{scope}.json")
        self.chunks = load_chunks(scope)
        self.keyword = KeywordIndex([c["text"] for c in self.chunks])
        self.vectors = None   # filled if Gemini embeddings work
        self.mode = "keyword"
        if config.GEMINI_API_KEY and self.chunks:
            try:
                self.vectors = self._load_or_build_vectors()
                self.mode = "embeddings"
            except Exception as e:
                print(f"[rag:{scope}] Embeddings unavailable, using keyword search. Reason: {e}")
        print(f"[rag:{scope}] {len(self.chunks)} chunks loaded, search mode = {self.mode}")

    def _load_or_build_vectors(self) -> np.ndarray:
        # Vectors are saved per chunk, keyed by a hash of (model + text). After an upload
        # only the NEW chunks are sent to Gemini; unchanged ones are reused from the file.
        key = lambda text: hashlib.md5((config.GEMINI_EMBED_MODEL + text).encode()).hexdigest()
        saved = {}
        if self.index_file.exists():
            try:
                saved = json.loads(self.index_file.read_text()).get("chunks", {})
            except (ValueError, AttributeError):
                saved = {}   # old/corrupt format -> rebuild
        keys = [key(c["text"]) for c in self.chunks]
        missing = [i for i, k in enumerate(keys) if k not in saved]
        if missing:
            print(f"[rag:{self.scope}] Embedding {len(missing)} new chunk(s)...")
            for start in range(0, len(missing), 100):            # Gemini accepts up to 100 per call
                batch = missing[start:start + 100]
                vecs = _gemini_embed([self.chunks[i]["text"] for i in batch], "RETRIEVAL_DOCUMENT")
                for i, v in zip(batch, vecs):
                    saved[keys[i]] = v.tolist()
        # Keep only chunks that still exist, so deleted documents leave no trace in the index.
        saved = {k: saved[k] for k in keys}
        self.index_file.write_text(json.dumps({"chunks": saved}))
        return np.array([saved[k] for k in keys], dtype=np.float32)

    # -----------------------------------------------------------------------
    # Step 4: SEARCH
    # -----------------------------------------------------------------------
    def search(self, question: str, k: int = 3) -> list[dict]:
        """Return the k most relevant pieces, each with its source and score."""
        if not self.chunks:
            return []
        if self.mode == "embeddings":
            try:
                q = _embed_query(question)
                scores = self.vectors @ q          # cosine similarity with every chunk at once
                min_score = MIN_SCORE_EMBED
            except Exception as e:
                # Too slow or failed -> instant keyword search, so the caller is not kept waiting.
                print(f"[rag] Query embedding failed/slow, keyword backup. Reason: {e!r}")
                scores, min_score = np.array(self.keyword.scores(question)), MIN_SCORE_KEYWORD
        else:
            scores, min_score = np.array(self.keyword.scores(question)), MIN_SCORE_KEYWORD

        best = np.argsort(scores)[::-1][:k]        # indexes of the highest scores
        return [
            {**self.chunks[i], "score": round(float(scores[i]), 3)}
            for i in best
            if scores[i] >= min_score              # drop weak matches -> no guessing
        ]


def format_context(results: list[dict]) -> str:
    """Turn search results into text we paste into the LLM prompt."""
    if not results:
        return "NO RELEVANT COMPANY INFORMATION FOUND."
    return "\n\n".join(f"[Source: {r['source']}]\n{r['text']}" for r in results)


# One knowledge base per scope, built when first used.
_kbs: dict[str, KnowledgeBase] = {}
_lock = threading.Lock()


def get_kb(scope: str = "public") -> KnowledgeBase:
    """The voice agent only ever calls get_kb() -> public. Internal is for the admin page."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if scope not in _kbs:
        with _lock:
            if scope not in _kbs:
                _kbs[scope] = KnowledgeBase(scope)
    return _kbs[scope]


def reload_kb(scope: str) -> KnowledgeBase:
    """Rebuild after an upload/delete. The old index keeps serving live calls until the new one is ready."""
    with _lock:
        new = KnowledgeBase(scope)
        _kbs[scope] = new
        return new


# ---------------------------------------------------------------------------
# Self-test:  python rag.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    base = get_kb()
    for q in [
        "How much does the AI voice agent cost?",
        "Is my data safe with you?",
        "Kitne din mein project complete hoga?",      # Hinglish
        "What is the capital of France?",             # should find nothing
    ]:
        print(f"\nQ: {q}")
        hits = base.search(q)
        if not hits:
            print("   (nothing relevant -> agent will not guess)")
        for h in hits:
            print(f"   {h['score']:.3f}  {h['source']}")
