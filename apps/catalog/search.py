"""Product search: PostgreSQL full-text search with a trigram fallback.

1. Full-text: ``websearch_to_tsquery`` (Google-like syntax: quotes, OR, -term)
   against the weighted ``search_vector`` (title A, brand B, description C),
   ranked with ``ts_rank``. Uses the GIN index on search_vector.
2. If that finds nothing - typically a typo ("keybaord") that stemming can't
   fix - fall back to pg_trgm *word similarity* on the title through the
   ``%>`` operator, which is what lets Postgres use the gin_trgm_ops index.
   (Filtering on ``similarity(...) > x`` instead would scan every row.)
"""

from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramWordSimilarity
from django.db.models import F, QuerySet

def search_products(queryset: QuerySet, query: str, *, limit: int = 50) -> tuple[list, str]:
    query = (query or "").strip()
    if not query:
        return [], "none"

    ts_query = SearchQuery(query, search_type="websearch", config="english")
    ranked = list(
        queryset.filter(search_vector=ts_query)
        .annotate(rank=SearchRank(F("search_vector"), ts_query))
        .order_by("-rank", "-id")[:limit]
    )
    if ranked:
        return ranked, "fulltext"

    fuzzy = list(
        queryset.filter(title__trigram_word_similar=query)
        .annotate(rank=TrigramWordSimilarity(query, "title"))
        .order_by("-rank", "-id")[:limit]
    )
    return fuzzy, "trigram"
