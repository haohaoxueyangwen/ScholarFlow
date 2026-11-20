"""Paper capability's deterministic operations, metadata/summary only."""
from scholarflow.domain_queries import ScholarFlowQueryService
from scholarflow.read_operations import Field, Operation


OPERATIONS = [
    Operation("paper.search", "paper-read", "paper_catalog", {
        "query": Field(str), "status": Field(str), "author": Field(str), "limit": Field(int, minimum=1, maximum=100),
        "offset": Field(int, minimum=0), "year_from": Field(int, minimum=1000, maximum=2100), "year_to": Field(int, minimum=1000, maximum=2100)},
        lambda store, params: ScholarFlowQueryService(store).search_papers(**params), resources=("papers.json",)),
    Operation("paper.get", "paper-read", "paper_catalog", {"paper_id": Field(str, required=True)},
        lambda store, params: ScholarFlowQueryService(store).get_paper(params["paper_id"]), resources=("papers.json",)),
]
