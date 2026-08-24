"""add tenant-scoped provider callback receipts

Revision ID: 0015_provider_callback_receipts
Revises: 0014_external_effect_outbox
Create Date: 2026-07-19
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    ExternalEffectType,
    ProviderCallbackKind,
    ProviderCallbackReason,
    ProviderCallbackState,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0015_provider_callback_receipts"
down_revision: str | None = "0014_external_effect_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKEN_VERSION = "cr2"
_CLINIC_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_SCOPE_DOMAIN = b"clinic-recall-effect-scope-v1\0"


def _enum(py_enum, name: str) -> sa.Enum:
    values = [member.value for member in py_enum]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Add opaque effect correlation and the minimized callback inbox."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for member in (ExternalEffectType.CALL, ExternalEffectType.RECORDING):
                op.execute(
                    sa.text(
                        f"ALTER TYPE external_effect_type ADD VALUE IF NOT EXISTS '{member.value}'"
                    )
                )
        _create_callback_enums(bind)

    _add_external_effect_correlation(bind)
    _create_receipt_table(bind)
    _ensure_receipt_indexes()

    if bind.dialect.name == "postgresql":
        _apply_policy()


def downgrade() -> None:
    """Remove local PR-02 schema while retaining harmless enum expansions."""
    bind = op.get_bind()
    if "provider_callback_receipt" in sa.inspect(bind).get_table_names():
        op.drop_table("provider_callback_receipt")

    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("external_effect")}
    indexes = {index["name"] for index in inspector.get_indexes("external_effect")}
    unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("external_effect")
    }
    check_names = {
        constraint["name"] for constraint in inspector.get_check_constraints("external_effect")
    }
    if "ix_external_effect_provider_resource" in indexes:
        op.drop_index("ix_external_effect_provider_resource", table_name="external_effect")
    with op.batch_alter_table("external_effect") as batch:
        if "uq_external_effect_callback_token" in unique_names:
            batch.drop_constraint(
                "uq_external_effect_callback_token",
                type_="unique",
            )
        if "uq_external_effect_clinic_id_id" in unique_names:
            batch.drop_constraint(
                "uq_external_effect_clinic_id_id",
                type_="unique",
            )
        if "ck_external_effect_callback_token_length" in check_names:
            batch.drop_constraint(
                "ck_external_effect_callback_token_length",
                type_="check",
            )
        if "provider_sequence" in columns:
            batch.drop_column("provider_sequence")
        if "callback_token" in columns:
            batch.drop_column("callback_token")

    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="provider_callback_reason").drop(bind, checkfirst=True)
        postgresql.ENUM(name="provider_callback_state").drop(bind, checkfirst=True)
        postgresql.ENUM(name="provider_callback_kind").drop(bind, checkfirst=True)


def _create_callback_enums(bind) -> None:
    postgresql.ENUM(
        *[member.value for member in ProviderCallbackKind],
        name="provider_callback_kind",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        *[member.value for member in ProviderCallbackState],
        name="provider_callback_state",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        *[member.value for member in ProviderCallbackReason],
        name="provider_callback_reason",
    ).create(bind, checkfirst=True)


def _add_external_effect_correlation(bind) -> None:
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("external_effect")}
    if "callback_token" not in columns:
        op.add_column(
            "external_effect",
            sa.Column("callback_token", sa.String(length=240), nullable=True),
        )
    if "provider_sequence" not in columns:
        op.add_column(
            "external_effect",
            sa.Column("provider_sequence", sa.BigInteger(), nullable=True),
        )

    if bind.dialect.name == "postgresql":
        clinic_ids = list(bind.execute(sa.text("SELECT id FROM clinic ORDER BY id")).scalars())
        for clinic_id in clinic_ids:
            bind.execute(
                sa.text(f"SELECT set_config('{RLS_GUC}', :clinic_id, true)"),
                {"clinic_id": str(clinic_id)},
            )
            _backfill_clinic_tokens(bind, str(clinic_id))
    else:
        rows = bind.execute(
            sa.text(
                "SELECT id, clinic_id FROM external_effect WHERE callback_token IS NULL ORDER BY id"
            )
        ).all()
        for effect_id, clinic_id in rows:
            _set_effect_token(bind, str(effect_id), str(clinic_id))

    inspector = sa.inspect(bind)
    unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("external_effect")
    }
    check_names = {
        constraint["name"] for constraint in inspector.get_check_constraints("external_effect")
    }
    with op.batch_alter_table("external_effect") as batch:
        batch.alter_column(
            "callback_token",
            existing_type=sa.String(length=240),
            nullable=False,
        )
        if "uq_external_effect_callback_token" not in unique_names:
            batch.create_unique_constraint(
                "uq_external_effect_callback_token",
                ["callback_token"],
            )
        if "uq_external_effect_clinic_id_id" not in unique_names:
            batch.create_unique_constraint(
                "uq_external_effect_clinic_id_id",
                ["clinic_id", "id"],
            )
        if "ck_external_effect_callback_token_length" not in check_names:
            batch.create_check_constraint(
                "ck_external_effect_callback_token_length",
                "length(callback_token) BETWEEN 50 AND 240",
            )

    index_names = {index["name"] for index in sa.inspect(bind).get_indexes("external_effect")}
    if "ix_external_effect_provider_resource" not in index_names:
        op.create_index(
            "ix_external_effect_provider_resource",
            "external_effect",
            ["clinic_id", "effect_type", "provider_resource_id"],
        )


def _create_receipt_table(bind) -> None:
    if "provider_callback_receipt" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "provider_callback_receipt",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("external_effect_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "callback_kind",
            _enum(ProviderCallbackKind, "provider_callback_kind"),
            nullable=False,
        ),
        sa.Column("deduplication_hash", sa.String(length=64), nullable=False),
        sa.Column("effect_token_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_resource_id", sa.String(length=128), nullable=True),
        sa.Column("normalized_status", sa.String(length=32), nullable=False),
        sa.Column("provider_sequence", sa.BigInteger(), nullable=True),
        sa.Column("provider_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            _enum(ProviderCallbackState, "provider_callback_state"),
            server_default=ProviderCallbackState.PENDING.value,
            nullable=False,
        ),
        sa.Column(
            "reason_code",
            _enum(ProviderCallbackReason, "provider_callback_reason"),
            nullable=True,
        ),
        sa.Column("processing_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 32",
            name="ck_provider_callback_receipt_provider_length",
        ),
        sa.CheckConstraint(
            "length(deduplication_hash) = 64",
            name="ck_provider_callback_receipt_dedup_hash_length",
        ),
        sa.CheckConstraint(
            "length(effect_token_hash) = 64",
            name="ck_provider_callback_receipt_token_hash_length",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_provider_callback_receipt_payload_hash_length",
        ),
        sa.CheckConstraint(
            "length(normalized_status) BETWEEN 1 AND 32",
            name="ck_provider_callback_receipt_status_length",
        ),
        sa.CheckConstraint(
            "provider_sequence IS NULL OR provider_sequence >= 0",
            name="ck_provider_callback_receipt_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "processing_attempts >= 0",
            name="ck_provider_callback_receipt_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_provider_callback_receipt_tenant_effect",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "provider",
            "callback_kind",
            "deduplication_hash",
            name="uq_provider_callback_receipt_event",
        ),
    )


def _ensure_receipt_indexes() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("provider_callback_receipt")
    }
    required = {
        "ix_provider_callback_receipt_clinic_id": ["clinic_id"],
        "ix_provider_callback_receipt_external_effect_id": ["external_effect_id"],
        "ix_provider_callback_receipt_claim": ["clinic_id", "state", "received_at"],
        "ix_provider_callback_receipt_expired_lease": [
            "clinic_id",
            "state",
            "lease_expires_at",
        ],
        "ix_provider_callback_receipt_effect": [
            "clinic_id",
            "external_effect_id",
            "callback_kind",
        ],
    }
    for name, columns in required.items():
        if name not in indexes:
            op.create_index(name, "provider_callback_receipt", columns)


def _backfill_clinic_tokens(bind, clinic_id: str) -> None:
    effect_ids = bind.execute(
        sa.text(
            "SELECT id FROM external_effect "
            "WHERE clinic_id = :clinic_id AND callback_token IS NULL ORDER BY id"
        ),
        {"clinic_id": clinic_id},
    ).scalars()
    for effect_id in effect_ids:
        _set_effect_token(bind, str(effect_id), clinic_id)


def _set_effect_token(bind, effect_id: str, clinic_id: str) -> None:
    bind.execute(
        sa.text(
            "UPDATE external_effect SET callback_token = :token "
            "WHERE id = :effect_id AND callback_token IS NULL"
        ),
        {"effect_id": effect_id, "token": _generate_effect_token(clinic_id)},
    )


def _apply_policy() -> None:
    policy = "provider_callback_receipt_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text("ALTER TABLE provider_callback_receipt ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE provider_callback_receipt FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON provider_callback_receipt"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON provider_callback_receipt "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def _generate_effect_token(clinic_id: str) -> str:
    if not _CLINIC_ID_PATTERN.fullmatch(clinic_id):
        raise ValueError("existing clinic id cannot be encoded as an effect token")
    scope_id = hashlib.sha256(_TOKEN_SCOPE_DOMAIN + clinic_id.encode("utf-8")).hexdigest()
    return f"{_TOKEN_VERSION}.{scope_id}.{secrets.token_urlsafe(32)}"
