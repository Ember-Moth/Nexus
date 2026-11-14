"""Allow the immutable Bedrock Converse usage format on admitted attempts."""

from alembic import op

revision = "0014_bedrock_converse"
down_revision = "0013_relay_contract"
branch_labels = None
depends_on = None


def allow_formats(bedrock):
    formats = "'openai.responses.v1','anthropic.messages.v1'"
    if bedrock:
        formats += ",'aws.bedrock.converse.v1'"
    name = "request_attempts_expected_usage_format_check"
    op.execute(f"ALTER TABLE billing.request_attempts DROP CONSTRAINT {name}")
    op.execute(
        f"ALTER TABLE billing.request_attempts ADD CONSTRAINT {name} "
        f"CHECK (expected_usage_format IN ({formats}))"
    )


def upgrade():
    allow_formats(True)


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (
        SELECT FROM catalog.upstreams WHERE protocol_family='aws_bedrock')
        OR EXISTS (SELECT FROM catalog.models WHERE protocols ? 'aws_bedrock')
        OR EXISTS (SELECT FROM publication.config_snapshots WHERE
            manifest->'required_capabilities' ? 'relay.bedrock-converse.v1')
        THEN RAISE EXCEPTION 'Bedrock configuration is retained'; END IF; END $$""")
    allow_formats(False)
