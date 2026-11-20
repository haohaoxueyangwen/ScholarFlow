"""Project capability's deterministic operations, independent of Pi dispatch."""
from scholarflow.domain_queries import ScholarFlowQueryService
from scholarflow.evidence import prepare_evidence_bundle
from scholarflow.read_operations import Field, Operation


OPERATIONS = [
    Operation("project.search", "project-evidence", "project_catalog", {
        "query": Field(str), "limit": Field(int, minimum=1, maximum=100), "offset": Field(int, minimum=0)},
        lambda store, params: ScholarFlowQueryService(store).search_project_stages(**params), resources=("project_stages.json",)),
    Operation("project.get", "project-evidence", "project_stage", {"stage_id": Field(str, required=True)},
        lambda store, params: ScholarFlowQueryService(store).get_project_stage(params["stage_id"]), "stage_id", ("project_stages.json",)),
    Operation("evidence.bundle", "project-evidence", "project_stage", {
        "project_stage_id": Field(str, required=True), "query": Field(str), "limit": Field(int, minimum=1, maximum=20)},
        lambda store, params: prepare_evidence_bundle(store, query=params.get("query", ""), limit=params.get("limit", 5), project_stage_id=params["project_stage_id"]).to_dict(), "project_stage_id",
        ("project_stages.json", "tasks.json", "experiments.json", "papers.json", "research_artifacts.json", "knowledge_relations.json")),
]
