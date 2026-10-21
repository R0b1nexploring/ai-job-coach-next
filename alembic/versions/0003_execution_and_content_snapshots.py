"""Fence executions and preserve per-revision content and guide snapshots."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _plans(base, items, derive=False):
    by_key = {plan["key"]: deepcopy(plan) for plan in base}
    result = []
    def project_key(item):
        return item.get("plan_key") or "legacy-project-" + _hash({"id": item.get("id"), "text": item.get("text", "")})[:16]
    for key in dict.fromkeys(project_key(item) for item in items):
        members = [item for item in items if project_key(item) == key]
        plan = by_key.get(key)
        if plan is None:
            plan = {"key": key, "title": "历史新增项目", "target_evidence": "", "target_evidence_status": "user_unconfirmed", "background": "历史修订未保存独立背景，待用户补充。", "task": "本人职责以保存条目为准，待用户确认。", "actions": [item["text"] for item in members], "outcome": "实际结果与预期产物待用户补充。", "verification": "验证材料待用户补充。"}
        elif derive:
            plan["task"] = "本人职责以历史修订条目为准，待用户确认。"
            plan["actions"] = [item["text"] for item in members]
            plan["outcome"] = "历史修订未单独确认结果，需核对实际结果与预期产物。"
        if derive or key not in by_key:
            plan["revision_items"] = [{k: deepcopy(v) for k, v in item.items() if k != "locked"} for item in members]
        result.append(plan)
    return result


def _semantic(data, plans, jd):
    items = [{key: value for key, value in item.items() if key != "locked"} for item in data["items"]]
    return _hash({"jd": jd, "title": data["title"], "summary": data["summary"], "items": items, "plans": plans})


def upgrade() -> None:
    op.add_column("batches", sa.Column("claim_token", sa.String(32)))
    for table in ("resumes", "resume_revisions"):
        op.add_column(table, sa.Column("semantic_hash", sa.String(64), nullable=False, server_default=""))
        op.add_column(table, sa.Column("plans", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("guides", sa.Column("claim_token", sa.String(32)))
    op.add_column("guides", sa.Column("semantic_hash", sa.String(64), nullable=False, server_default=""))
    op.add_column("guides", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("guides", sa.Column("resume_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "guide_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("guide_id", sa.String(32), sa.ForeignKey("guides.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("resume_revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("resume_snapshot", sa.JSON(), nullable=False),
        sa.Column("plans", sa.JSON(), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("sections", sa.JSON()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("guide_id", "revision"),
    )
    op.create_table(
        "task_executions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("guide_id", sa.String(32), sa.ForeignKey("guides.id", ondelete="CASCADE")),
        sa.Column("task_type", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.String(32), nullable=False, unique=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_task_executions_batch_id", "task_executions", ["batch_id"])

    bind = op.get_bind()
    metadata = sa.MetaData()
    batches = sa.Table("batches", metadata, autoload_with=bind)
    resumes = sa.Table("resumes", metadata, autoload_with=bind)
    revisions = sa.Table("resume_revisions", metadata, autoload_with=bind)
    guides = sa.Table("guides", metadata, autoload_with=bind)
    history = sa.Table("guide_revisions", metadata, autoload_with=bind)
    batch_map = {row["id"]: row for row in bind.execute(sa.select(batches)).mappings()}
    resume_map = {row["id"]: row for row in bind.execute(sa.select(resumes)).mappings()}
    legacy_hash_map = {}
    updated_revisions = {}
    for row in bind.execute(sa.select(revisions)).mappings():
        resume = resume_map[row["resume_id"]]
        batch = batch_map[resume["batch_id"]]
        data = {"title": row["title"], "summary": row["summary"], "items": row["items"]}
        plans = _plans((batch["plans"] or {}).get("projects", []), row["items"], derive=row["revision"] > 1)
        content_hash = row["content_hash"]
        fingerprint = _semantic(data, plans, batch["jd_text"])
        bind.execute(revisions.update().where(revisions.c.id == row["id"]).values(plans=plans, semantic_hash=fingerprint))
        snapshot = {"resume": data, "plans": plans, "semantic_hash": fingerprint, "content_hash": content_hash, "revision": row["revision"]}
        legacy_hash_map[(row["resume_id"], row["content_hash"])] = snapshot
        updated_revisions[(row["resume_id"], row["revision"])] = snapshot
    for resume in resume_map.values():
        snapshot = updated_revisions.get((resume["id"], resume["revision"]))
        if snapshot is None:
            raise RuntimeError("resume revision missing during snapshot upgrade")
        bind.execute(resumes.update().where(resumes.c.id == resume["id"]).values(plans=snapshot["plans"], semantic_hash=snapshot["semantic_hash"]))
    for guide in bind.execute(sa.select(guides)).mappings():
        resume = resume_map[guide["resume_id"]]
        batch = batch_map[guide["batch_id"]]
        snapshot = legacy_hash_map.get((resume["id"], guide["content_hash"])) or updated_revisions[(resume["id"], resume["revision"])]
        now = datetime.now(timezone.utc)
        # Historical columns and delivered text remain untouched. Missing old
        # plan data is represented conservatively in the new snapshot only.
        bind.execute(guides.update().where(guides.c.id == guide["id"]).values(semantic_hash=snapshot["semantic_hash"], resume_revision=snapshot["revision"], revision=1))
        bind.execute(history.insert().values(id=uuid4().hex, guide_id=guide["id"], batch_id=guide["batch_id"], revision=1, resume_revision=snapshot["revision"], content_hash=snapshot["content_hash"], semantic_hash=snapshot["semantic_hash"], resume_snapshot=snapshot["resume"], plans=snapshot["plans"], jd_text=batch["jd_text"], status=guide["status"], sections=guide["sections"], error_code=guide["error_code"], created_at=now, completed_at=now if guide["status"] in {"ready", "failed"} else None))
    # New nullable claim tokens ensure prototype leases cannot authenticate old
    # executions; an expired lease can be safely reclaimed by the new executor.


def downgrade() -> None:
    op.drop_index("ix_task_executions_batch_id", table_name="task_executions")
    op.drop_table("task_executions")
    op.drop_table("guide_revisions")
    for column in ("resume_revision", "revision", "semantic_hash", "claim_token"):
        op.drop_column("guides", column)
    for table in ("resume_revisions", "resumes"):
        op.drop_column(table, "plans")
        op.drop_column(table, "semantic_hash")
    op.drop_column("batches", "claim_token")
