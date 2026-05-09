"""SQLModel models for content_pipeline.

Importing this package registers every table on `SQLModel.metadata` so
Alembic autogenerate sees them.
"""

from app.models.brand_kits import BrandKit
from app.models.content_references import ContentReference
from app.models.generation_calls import GenerationCall
from app.models.media_assets import MediaAsset
from app.models.model_routes import ModelRoute
from app.models.music import MusicTrack
from app.models.projects import Project
from app.models.reference_intake_rules import ReferenceIntakeRule
from app.models.reference_usages import ReferenceUsage
from app.models.scenarios import SCENARIO_STATUSES, Scenario
from app.models.social_accounts import SocialAccount
from app.models.templates import Template

__all__ = [
    "Project",
    "BrandKit",
    "SocialAccount",
    "ContentReference",
    "ReferenceIntakeRule",
    "ReferenceUsage",
    "Scenario",
    "SCENARIO_STATUSES",
    "Template",
    "MusicTrack",
    "MediaAsset",
    "ModelRoute",
    "GenerationCall",
]
