"""Independent semantic review and bounded, field-scoped draft repair.

Only initial, unpublished bundles enter this pipeline. Identity, provenance and
version ownership remain application responsibilities; a reviewer cannot grant
confirmation or replace the bundle. Reports stay outside resume/guide semantics.
"""
from __future__ import annotations

from copy import deepcopy
import logging
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.content import normalize_bundle, stable_hash

logger = logging.getLogger("coach.review")


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewTarget(ReviewModel):
    scope: Literal["job", "blueprint", "plan"]
    target_id: str = Field(max_length=80)
    field: Literal["title", "summary", "text", "background", "task", "actions", "outcome", "verification"]
    quote: str = Field(min_length=1, max_length=2000)


class ReviewIssue(ReviewModel):
    id: str = Field(min_length=1, max_length=80)
    severity: Literal["error", "suggestion"]
    code: Literal["unsupported_claim", "contribution_boundary", "jd_alignment", "causal_gap", "plan_mismatch", "version_distinction", "feasibility", "wording"]
    targets: list[ReviewTarget] = Field(min_length=1, max_length=6)
    diagnosis: str = Field(min_length=4, max_length=1500)
    instruction: str = Field(min_length=4, max_length=1500)
    jd_quote: str = Field(default="", max_length=2000)


class ReviewDraft(ReviewModel):
    assessed_scopes: list[Literal["job", "blueprint", "plans"]] = Field(min_length=3, max_length=3)
    issues: list[ReviewIssue] = Field(max_length=20)


class RepairEdit(ReviewModel):
    issue_id: str = Field(min_length=1, max_length=80)
    scope: Literal["job", "blueprint", "plan"]
    target_id: str = Field(max_length=80)
    field: Literal["title", "summary", "text", "background", "task", "actions", "outcome", "verification"]
    before: str | list[str]
    after: str | list[str]


class RepairDraft(ReviewModel):
    edits: list[RepairEdit] = Field(max_length=40)


class RepairNoProgress(ValueError):
    """A repair makes no meaningful change to the reviewed copy."""


class ContentReviewBlocked(ValueError):
    def __init__(self, report: dict):
        super().__init__("content review did not resolve substantive issues")
        self.report = deepcopy(report)


def _key(target) -> tuple[str, str, str]:
    return target.scope, target.target_id, target.field


def _record(content: dict, scope: str, target_id: str, field: str) -> dict:
    if scope == "plan":
        if field not in {"title", "background", "task", "actions", "outcome", "verification"}:
            raise ValueError("protected plan field")
        records = [plan for plan in content["plans"] if plan["key"] == target_id]
    elif not target_id:
        if field not in {"title", "summary"}:
            raise ValueError("invalid resume field")
        records = [content["resumes"][scope]]
    else:
        if field != "text":
            raise ValueError("protected item field")
        records = [item for item in content["resumes"][scope]["items"] if item["id"] == target_id]
    if len(records) != 1:
        raise ValueError("unknown review target")
    return records[0]


def validate_review(raw: dict, content: dict, jd: str) -> dict:
    review = ReviewDraft.model_validate(raw)
    if set(review.assessed_scopes) != {"job", "blueprint", "plans"}:
        raise ValueError("incomplete semantic review")
    ids = [issue.id for issue in review.issues]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate issue id")
    for issue in review.issues:
        if issue.code == "wording" and issue.severity != "suggestion":
            raise ValueError("style preferences are not repair gates")
        if issue.jd_quote and issue.jd_quote not in jd:
            raise ValueError("review invented JD evidence")
        targets = set()
        for target in issue.targets:
            if _key(target) in targets:
                raise ValueError("duplicate issue target")
            targets.add(_key(target))
            value = _record(content, target.scope, target.target_id, target.field)[target.field]
            text = "\n".join(value) if isinstance(value, list) else value
            if not target.quote.strip() or target.quote not in text:
                raise ValueError("review evidence does not match target")
    return review.model_dump()


def _draft_shape(content: dict) -> dict:
    # Normalization accepts only model-owned copy; application metadata stays in
    # the candidate and can never be supplied by a repair response.
    plans = []
    for plan in content["plans"]:
        row = {key: deepcopy(plan[key]) for key in ("key", "title", "background", "task", "actions", "outcome", "verification")}
        row["target_evidence"] = plan.get("target_evidence") or plan.get("target_inference", "")
        plans.append(row)
    draft = {"plans": plans}
    for kind in ("job", "blueprint"):
        resume = content["resumes"][kind]
        draft[kind] = {"title": resume["title"], "summary": resume["summary"], "items": [
            {key: deepcopy(item[key]) for key in ("plan_key", "text", "source_quote", "achieved")}
            for item in resume["items"]
        ]}
    return draft


def apply_repair(raw: dict, content: dict, issues: list[dict], *, jd: str, material: str, confirmed: bool) -> tuple[dict, list[dict]]:
    repair = RepairDraft.model_validate(raw)
    if not repair.edits:
        raise RepairNoProgress("repair contains no edits")
    allowed = {issue["id"]: {(target["scope"], target["target_id"], target["field"]) for target in issue["targets"]}
               for issue in issues if issue["severity"] == "error"}
    candidate = deepcopy(content)
    edited = set()
    for edit in repair.edits:
        key = _key(edit)
        if key not in allowed.get(edit.issue_id, set()) or key in edited:
            raise ValueError("repair outside reviewed scope")
        record = _record(candidate, edit.scope, edit.target_id, edit.field)
        if record.get("locked"):
            raise ValueError("repair target is locked")
        if edit.scope == "plan" and any(item.get("locked") and item["plan_key"] == edit.target_id
                                          for resume in candidate["resumes"].values() for item in resume["items"]):
            raise ValueError("repair changes a locked item's plan")
        if record[edit.field] != edit.before:
            raise ValueError("repair is stale")
        if edit.field == "actions":
            if not isinstance(edit.after, list) or not 1 <= len(edit.after) <= 20 or any(not part.strip() or len(part) > 2000 for part in edit.after):
                raise ValueError("invalid repaired actions")
            after = [part.strip() for part in edit.after]
            before = [part.strip() for part in edit.before]
        elif not isinstance(edit.after, str) or not edit.after.strip() or len(edit.after) > 10000:
            raise ValueError("invalid repaired copy")
        else:
            after = edit.after.strip()
            before = edit.before.strip()
        if before == after:
            raise RepairNoProgress("repair has no meaningful progress")
        edit.after = after
        record[edit.field] = deepcopy(after)
        if edit.field == "text":
            # AI revision never inherits completion of a previously verbatim
            # confirmed claim; its original citation is preserved for inspection.
            record["achieved"] = False
            record["verification_status"] = "ai_generated"
        edited.add(key)
    if any(not targets.intersection(edited) for targets in allowed.values()):
        raise ValueError("repair leaves an initial error untouched")
    # Validate the same dual-version and source contract as initial generation.
    # Do not replace the candidate with regenerated IDs or discard original flags.
    normalize_bundle(_draft_shape(candidate), jd, material, confirmed)
    return candidate, [edit.model_dump() for edit in repair.edits]


def review_bundle(content: dict, *, jd: str, material: str, confirmed: bool, provider, source: str,
                  checkpoint: Callable[[], None]) -> tuple[dict, dict]:
    report = {"version": 1, "status": "unavailable", "source": source, "reason": None,
              "rounds": [], "issues": [], "edits": [], "repair_count": 0}
    if source in {"local", "degraded_local"}:
        report["reason"] = "local_provider"
        return content, report
    review_method = getattr(provider, "review", None)
    if review_method is None:
        report["reason"] = "unsupported_provider"
        return content, report

    def payload(draft):
        return {"jd": jd, "material": material, "material_confirmed": confirmed, "draft": deepcopy(draft)}

    checkpoint()
    try:
        raw = review_method(payload(content))
    except Exception as exc:
        logger.warning("content review unavailable phase=review category=%s", type(exc).__name__)
        report["reason"] = "model_unavailable"
        return content, report
    checkpoint()
    if raw is None:
        report["reason"] = "unsupported_provider"
        return content, report
    try:
        review = validate_review(raw, content, jd)
    except (ValueError, TypeError, KeyError):
        report["reason"] = "invalid_review"
        return content, report
    report["rounds"].append({"input_hash": stable_hash(content), **review})
    report["issues"] = review["issues"]
    errors = [issue for issue in review["issues"] if issue["severity"] == "error"]
    if not errors:
        report["status"] = "suggestions" if review["issues"] else "passed"
        return content, report

    def blocked(reason):
        report.update(status="blocked", reason=reason)
        raise ContentReviewBlocked(report)

    checkpoint()
    try:
        raw_repair = provider.repair({**payload(content), "issues": deepcopy(errors)})
    except Exception as exc:
        logger.warning("content review unavailable phase=repair category=%s", type(exc).__name__)
        blocked("repair_failed")
    checkpoint()
    try:
        candidate, edits = apply_repair(raw_repair, content, errors, jd=jd, material=material, confirmed=confirmed)
    except RepairNoProgress:
        blocked("no_progress")
    except (ValueError, TypeError, KeyError):
        blocked("repair_rejected")
    if stable_hash(candidate) == stable_hash(content):
        blocked("no_progress")
    report["edits"] = edits
    report["repair_count"] = len(edits)
    checkpoint()
    try:
        raw_recheck = review_method(payload(candidate))
    except Exception as exc:
        logger.warning("content review unavailable phase=recheck category=%s", type(exc).__name__)
        blocked("recheck_unavailable")
    checkpoint()
    try:
        recheck = validate_review(raw_recheck, candidate, jd)
    except (ValueError, TypeError, KeyError):
        blocked("recheck_unavailable")
    report["rounds"].append({"input_hash": stable_hash(candidate), **recheck})
    report["issues"] = recheck["issues"]
    if any(issue["severity"] == "error" for issue in recheck["issues"]):
        blocked("unresolved_issues")
    report["status"] = "repaired"
    return candidate, report
