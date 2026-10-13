import json
import math
import re

from .config import PIPELINE_VERSION
from .runtime import DigupError, Worker
from .store import unpack

STOPWORDS = frozenset(
    "a an the and or of to in on at for from with by as is are was were be been being "
    "it its this that these those i me my we our you your he she they their "
    "about where when what which who how under over into does do did".split()
)


def search(store, query: str, limit=10, kinds=(), code=False, keyword_only=False, worker_factory=Worker):
    with store.snapshot():
        return _search(store, query, limit, kinds, code, keyword_only, worker_factory)


def _search(store, query, limit, kinds, code, keyword_only, worker_factory):
    if query.startswith("code:"):
        code, query = True, query[5:].strip()
    if not query.strip():
        raise DigupError("Search query must not be empty")
    collection = "code" if code else "files"
    params = [collection]
    where = "f.collection=?"
    if kinds:
        where += " AND f.kind IN (" + ",".join("?" for _ in kinds) + ")"
        params += list(kinds)
    # Keyword candidates are constrained to the collection before the limit is applied.
    all_terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)))[:32]
    terms = [t for t in all_terms if t not in STOPWORDS] if len(all_terms) > 1 else all_terms
    if not terms:
        terms = all_terms
    keyword = {}
    if terms:
        match = " OR ".join('"' + t + '"' for t in terms)
        rows = store.db.execute(
            f"""SELECT s.id,bm25(search_fts,2.0,1.0) AS rank FROM search_fts
            JOIN segments s ON s.id=search_fts.rowid JOIN files f ON f.id=s.file_id
            WHERE search_fts MATCH ? AND {where} ORDER BY rank LIMIT 500""",
            [match, *params],
        )
        keyword = {row[0]: i for i, row in enumerate(rows, 1)}
    semantic = {}
    if not keyword_only:
        for row in store.db.execute(
            f"SELECT DISTINCT f.pipeline FROM files f JOIN segments s ON s.file_id=f.id "
            f"WHERE {where} AND s.vector IS NOT NULL",
            params,
        ):
            if json.loads(row[0]).get("version") != PIPELINE_VERSION:
                raise DigupError(
                    "Index was made by a different embedding pipeline; run `digup index --reindex`"
                )
        count = store.db.execute(
            f"SELECT count(*) FROM segments s JOIN files f ON f.id=s.file_id "
            f"WHERE {where} AND s.vector IS NOT NULL",
            params,
        ).fetchone()[0]
        if count:
            worker = worker_factory(media=False)
            try:
                task = "code retrieval" if code else "search result"
                vector = worker.embed("text", f"task: {task} | query: {query}")
            finally:
                worker.close()
            scores = []
            for row in store.db.execute(
                f"SELECT s.id,s.vector FROM segments s JOIN files f ON f.id=s.file_id "
                f"WHERE {where} AND s.vector IS NOT NULL",
                params,
            ):
                document = unpack(row[1])
                if len(document) != len(vector):
                    raise DigupError("Index contains incompatible vector dimensions; reindex required")
                similarity = sum(a * b for a, b in zip(document, vector))
                if math.isfinite(similarity) and similarity > 0:
                    scores.append((row[0], similarity))
            scores.sort(key=lambda pair: pair[1], reverse=True)
            semantic = {id_: (rank, score) for rank, (id_, score) in enumerate(scores[:500], 1)}
        else:
            raise DigupError(
                "No semantic vectors in this collection. Index without --keyword-only, "
                "or search with --keyword-only."
            )
    candidates = keyword.keys() | semantic.keys()
    ranked = []
    for id_ in candidates:
        # Reciprocal rank fusion avoids mixing BM25's scale with cosine similarity.
        row = store.db.execute(
            """SELECT s.*,f.kind,f.collection FROM segments s JOIN files f ON f.id=s.file_id
                                  WHERE s.id=?""",
            (id_,),
        ).fetchone()
        words = set(re.findall(r"[^\W_]+", (row["path"] + " " + row["text"]).casefold()))
        coverage = sum(t in words for t in terms) / max(1, len(terms))
        # Weak partial keyword matches must not beat strong cross-modal meaning matches.
        score = coverage**2 / (60 + keyword[id_]) if id_ in keyword else 0
        score += 1 / (60 + semantic[id_][0]) if id_ in semantic else 0
        item = {key: row[key] for key in ("path", "kind", "location", "line", "page", "seconds")}
        item.update(
            score=score,
            similarity=semantic.get(id_, (None, None))[1],
            keyword_match=id_ in keyword,
            snippet=snippet(row["text"], terms),
        )
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["path"], item["location"]))
    # Return the strongest page/passage/moment for each file, rather than filling results with one PDF.
    results, seen = [], set()
    for item in ranked:
        if item["path"] not in seen:
            results.append(item)
            seen.add(item["path"])
        if len(results) >= limit:
            break
    return results


def snippet(text, terms):
    compact = " ".join(text.split())
    positions = [compact.casefold().find(term.casefold()) for term in terms]
    positions = [p for p in positions if p >= 0]
    start = max(0, min(positions) - 70) if positions else 0
    return ("…" if start else "") + compact[start : start + 240] + ("…" if len(compact) > start + 240 else "")
