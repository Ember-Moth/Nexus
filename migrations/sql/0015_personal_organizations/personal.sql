ALTER TABLE identity.organizations
    ADD COLUMN personal_owner_id uuid UNIQUE REFERENCES identity.users(id) ON DELETE RESTRICT,
    ADD CONSTRAINT personal_org_not_archived CHECK (personal_owner_id IS NULL OR status <> 'archived');

-- One transaction creates the tenant, the owner's membership and the ordinary
-- billing outbox fact. The same statement is used for the upgrade backfill.
CREATE FUNCTION identity.create_personal_organization(owner_id uuid) RETURNS void
LANGUAGE sql SET search_path=pg_catalog,pg_temp AS $$
    WITH org AS (
        INSERT INTO identity.organizations(id, name, personal_owner_id)
        VALUES(gen_random_uuid(), 'Personal''s Organisation', owner_id)
        RETURNING id
    ), member AS (
        INSERT INTO identity.memberships(id, organization_id, user_id, role)
        SELECT gen_random_uuid(), id, owner_id, 'org_owner' FROM org
        RETURNING organization_id
    )
    INSERT INTO identity.outbox(id, topic, schema_version, aggregate_id, aggregate_version, payload)
    SELECT gen_random_uuid(), 'identity.organization_created', 1, organization_id, 1,
           jsonb_build_object('organization_id', organization_id, 'owner_user_id', owner_id,
                              'personal_organization', true)
    FROM member;
$$;
REVOKE ALL ON FUNCTION identity.create_personal_organization(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.create_personal_organization(uuid) TO nexus_identity;

CREATE FUNCTION identity.on_user_created() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    PERFORM identity.create_personal_organization(NEW.id);
    RETURN NEW;
END $$;
CREATE TRIGGER user_personal_organization AFTER INSERT ON identity.users
    FOR EACH ROW EXECUTE FUNCTION identity.on_user_created();

-- A downgrade retains organizations, memberships, billing IDs and creation events.
-- Recover the original binding even if the organization was subsequently renamed.
UPDATE identity.organizations o
SET personal_owner_id=(e.payload->>'owner_user_id')::uuid
FROM identity.outbox e
WHERE e.aggregate_id=o.id AND e.topic='identity.organization_created'
  AND e.payload->'personal_organization'='true'::jsonb;

DO $$ BEGIN
    IF EXISTS (
        SELECT FROM identity.organizations o WHERE o.personal_owner_id IS NOT NULL
        AND NOT EXISTS (SELECT FROM identity.memberships m WHERE m.organization_id=o.id
            AND m.user_id=o.personal_owner_id AND m.role='org_owner' AND m.status='active')
    ) THEN RAISE EXCEPTION 'personal owner membership must be restored before upgrading'; END IF;
END $$;

SELECT identity.create_personal_organization(u.id) FROM identity.users u
WHERE NOT EXISTS (SELECT FROM identity.organizations o WHERE o.personal_owner_id=u.id);

CREATE FUNCTION identity.protect_personal_organization() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.personal_owner_id IS NOT NULL THEN RAISE EXCEPTION 'personal_organization_required'; END IF;
        RETURN OLD;
    END IF;
    IF NEW.personal_owner_id IS DISTINCT FROM OLD.personal_owner_id THEN
        RAISE EXCEPTION 'personal_owner_immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER personal_organization_binding BEFORE UPDATE OF personal_owner_id OR DELETE
    ON identity.organizations FOR EACH ROW EXECUTE FUNCTION identity.protect_personal_organization();

CREATE FUNCTION identity.protect_personal_membership() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF EXISTS(SELECT FROM identity.organizations WHERE id=OLD.organization_id AND personal_owner_id=OLD.user_id)
       AND (TG_OP='DELETE' OR NEW.role<>'org_owner' OR NEW.status<>'active'
            OR (NEW.organization_id, NEW.user_id) IS DISTINCT FROM (OLD.organization_id, OLD.user_id)) THEN
        RAISE EXCEPTION 'personal_owner_required';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER personal_membership_binding BEFORE UPDATE OR DELETE ON identity.memberships
    FOR EACH ROW EXECUTE FUNCTION identity.protect_personal_membership();

CREATE TABLE identity.organization_invitations (
    id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES identity.organizations(id),
    login varchar(254) NOT NULL, role varchar(32) NOT NULL CHECK(role IN ('org_owner','org_finance','org_developer')),
    invited_by uuid NOT NULL REFERENCES identity.users(id), token_digest varchar(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
    revoked_at timestamptz, accepted_at timestamptz, accepted_by uuid REFERENCES identity.users(id),
    CHECK((accepted_by IS NULL) = (accepted_at IS NULL)),
    CHECK(accepted_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX invitation_open_login ON identity.organization_invitations(organization_id, login)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX invitations_page_idx ON identity.organization_invitations(organization_id, created_at, id);
GRANT SELECT, INSERT, UPDATE ON identity.organization_invitations TO nexus_identity;
