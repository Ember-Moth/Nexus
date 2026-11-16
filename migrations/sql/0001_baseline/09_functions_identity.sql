-- identity functions and trigger functions.
-- Created by their owning role, so their privileges are exactly the grants in 17_privileges.sql.

-- Owned by nexus_identity.
SET LOCAL ROLE nexus_identity;

CREATE FUNCTION identity.v1_authorize(kid uuid, model uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE k identity.api_keys;
BEGIN
 PERFORM revision FROM identity.source_revision WHERE id=1 FOR SHARE;
 SELECT ak.* INTO k FROM identity.api_keys ak JOIN identity.organizations o ON o.id=ak.organization_id
 JOIN identity.projects pr ON pr.id=ak.project_id JOIN identity.service_accounts sa ON sa.id=ak.service_account_id
 WHERE ak.id=kid AND o.status='active' AND pr.status='active' AND sa.status='active'
 AND ak.revoked_at IS NULL AND (ak.expires_at IS NULL OR ak.expires_at>clock_timestamp());
 IF NOT FOUND THEN RAISE EXCEPTION 'key_inactive'; END IF;
 IF NOT EXISTS(SELECT FROM identity.access_policies WHERE organization_id=k.organization_id
 AND layer='platform' AND scope_type='organization') OR EXISTS(
 SELECT FROM identity.access_policies ap WHERE ap.organization_id=k.organization_id AND
 (ap.scope_type='organization' OR ap.scope_type='project' AND ap.scope_id=k.project_id
 OR ap.scope_type='key' AND ap.scope_id=kid) AND NOT EXISTS(
 SELECT FROM identity.access_policy_models pm WHERE pm.policy_id=ap.id AND pm.model_id=model))
 THEN RAISE EXCEPTION 'model_forbidden'; END IF;
 RETURN jsonb_build_object('organization_id',k.organization_id,'project_id',k.project_id,
 'service_account_id',k.service_account_id,'api_key_id',k.id);
END $$;

CREATE FUNCTION identity.v1_key_activity(kids uuid[]) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT coalesce(jsonb_agg(jsonb_build_object(
  'api_key_id',k.id,'organization_id',ak.organization_id,
  'reason',CASE WHEN ak.id IS NULL THEN 'key_missing'
   WHEN ak.revoked_at IS NOT NULL THEN 'key_revoked'
   WHEN ak.expires_at<=clock_timestamp() THEN 'key_expired'
   WHEN o.status<>'active' THEN 'organization_'||o.status
   WHEN pr.status<>'active' THEN 'project_'||pr.status
   WHEN sa.status<>'active' THEN 'service_account_'||sa.status END,
  'since',CASE WHEN ak.revoked_at IS NOT NULL THEN ak.revoked_at
   WHEN ak.expires_at<=clock_timestamp() THEN ak.expires_at END)),'[]'::jsonb)
 FROM unnest(kids) k(id) LEFT JOIN identity.api_keys ak ON ak.id=k.id
 LEFT JOIN identity.organizations o ON o.id=ak.organization_id
 LEFT JOIN identity.projects pr ON pr.id=ak.project_id
 LEFT JOIN identity.service_accounts sa ON sa.id=ak.service_account_id
$$;

RESET ROLE;

-- Owned by nexus_migrator.
CREATE FUNCTION identity.advance_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            UPDATE identity.source_revision SET revision=revision+1 WHERE id=1;
            RETURN NULL; END $$;

CREATE FUNCTION identity.announce_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            PERFORM pg_notify('nexus_source_revision','identity'); RETURN NULL; END $$;

CREATE FUNCTION identity.create_personal_organization(owner_id uuid) RETURNS void
    LANGUAGE sql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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

CREATE FUNCTION identity.on_user_created() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
    PERFORM identity.create_personal_organization(NEW.id);
    RETURN NEW;
END $$;

CREATE FUNCTION identity.protect_personal_membership() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
    IF EXISTS(SELECT FROM identity.organizations WHERE id=OLD.organization_id AND personal_owner_id=OLD.user_id)
       AND (TG_OP='DELETE' OR NEW.role<>'org_owner' OR NEW.status<>'active'
            OR (NEW.organization_id, NEW.user_id) IS DISTINCT FROM (OLD.organization_id, OLD.user_id)) THEN
        RAISE EXCEPTION 'personal_owner_required';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION identity.protect_personal_organization() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
