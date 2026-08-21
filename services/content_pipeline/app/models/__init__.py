"""SQLModel models for content_pipeline.

Importing this package registers every table on `SQLModel.metadata` so
Alembic autogenerate sees them.

Note (CP-M9): user / project_membership / auth_session tables live in
the main `app/` service now. content_pipeline is auth-less internally
and trusts the gateway's X-API-Key.

CP-M10: the scenario pipeline (scenarios / scene_renders / render_variants
/ reference_usages / auto_generation_rules) was removed and replaced by
the remake vertical (remakes / remake_shots / remake_steps).
"""

from app.models.brand_kits import BrandKit
from app.models.content_references import ContentReference
from app.models.generation_calls import GenerationCall
from app.models.media_assets import MediaAsset
from app.models.model_routes import ModelRoute
from app.models.music import MusicTrack
from app.models.plan_slots import CONTENT_TYPES, PLAN_SLOT_SOURCE_KINDS, PLAN_SLOT_STATUSES, PlanSlot
from app.models.posting_strategy import PostingStrategy
from app.models.projects import Project
from app.models.publish_jobs import PUBLISH_JOB_STATUSES, PublishJob
from app.models.reference_intake_rules import ReferenceIntakeRule
from app.models.remake_shots import SHOT_STATUSES, SHOT_TECHNIQUES, RemakeShot
from app.models.remake_steps import STEP_QUEUES, STEP_STATUSES, RemakeStep
from app.models.remakes import REMAKE_FROZEN_STATUSES, REMAKE_STATUSES, Remake
from app.models.social_accounts import SocialAccount
from app.models.templates import Template
from app.models.weekly_plans import WEEKLY_PLAN_STATUSES, WeeklyPlan

__all__ = [
    "Project",
    "BrandKit",
    "SocialAccount",
    "ContentReference",
    "ReferenceIntakeRule",
    "Remake",
    "REMAKE_STATUSES",
    "REMAKE_FROZEN_STATUSES",
    "RemakeShot",
    "SHOT_STATUSES",
    "SHOT_TECHNIQUES",
    "RemakeStep",
    "STEP_STATUSES",
    "STEP_QUEUES",
    "Template",
    "MusicTrack",
    "MediaAsset",
    "ModelRoute",
    "GenerationCall",
    "PostingStrategy",
    "WeeklyPlan",
    "WEEKLY_PLAN_STATUSES",
    "PlanSlot",
    "PLAN_SLOT_STATUSES",
    "PLAN_SLOT_SOURCE_KINDS",
    "CONTENT_TYPES",
    "PublishJob",
    "PUBLISH_JOB_STATUSES",
]
