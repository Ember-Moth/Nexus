"""Bind Chat Completions token receipts to their originating attempts."""

from alembic import op

revision = "0030_chat_upstream"
down_revision = "0029_guardrail_units"
branch_labels = None
depends_on = None


def formats(chat):
    values = [
        "openai.responses.v1",
        "anthropic.messages.v1",
        "aws.bedrock.converse.v1",
        "openai.vector_storage.v1",
        "openai.container.v1",
    ]
    if chat:
        values.append("openai.chat.v1")
    name = "request_attempts_expected_usage_format_check"
    op.execute(f"ALTER TABLE billing.request_attempts DROP CONSTRAINT {name}")
    op.execute(
        f"ALTER TABLE billing.request_attempts ADD CONSTRAINT {name} "
        f"CHECK(expected_usage_format IN ({','.join(repr(v) for v in values)}))"
    )


def upgrade():
    formats(True)


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM catalog.deployments
        WHERE relay_profile->>'endpoint'='openai_chat_completions') OR EXISTS (
        SELECT FROM publication.config_snapshots WHERE
        manifest->'required_capabilities' ? 'relay.responses-to-chat.v1')
        THEN RAISE EXCEPTION 'Chat Completions configuration is retained'; END IF; END $$""")
    formats(False)
