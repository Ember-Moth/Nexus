import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import key_cursor

# Automation state shown with every cluster; the lease and probe bookkeeping stay private.
STATUS = """a.pending_since,a.checked_at,a.published_revision,a.published_at,a.published_trigger,
    a.failure_code,a.failure_message,a.failure_at,a.paused_revision,a.paused_at,
    a.failed IS NOT NULL AS failing,a.content_digest,a.content_revision"""
# The cluster's effective Relay and auto-publish policy: its override, else the platform default.
POLICY_COLUMNS = (
    "max_attempts",
    "failover_budget_ms",
    "purge_grace_seconds",
    "stream_lifetime_seconds",
    "auto_publish_quiet_seconds",
    "auto_publish_max_delay_seconds",
)
POLICY = (
    ",".join(f"coalesce(p.{name},d.{name}) AS {name}" for name in POLICY_COLUMNS)
    + ",p.version AS policy_version,d.version AS defaults_version"
)
CLUSTER = (
    "SELECT c.*," + STATUS + "," + POLICY + " FROM publication.gateway_clusters c "
    "LEFT JOIN publication.cluster_automation a ON a.cluster_id=c.id "
    "LEFT JOIN publication.cluster_policies p ON p.cluster_id=c.id "
    "CROSS JOIN publication.cluster_policy_defaults d "
)
LOCK = (
    ",managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
    "THEN NULL ELSE clock_timestamp() END"
)
# Sentinel: an administrator's write leaves the policy lock as it is (it is refused while set).
KEEP = object()
# Columns the auto-publisher may write between claims; JSON documents are cast explicitly.
TRACKED = {
    "observed": True,
    "changed_at": False,
    "pending_since": False,
    "retry_after": False,
    "failed": True,
    "checked_at": False,
    "failure_code": False,
    "failure_message": False,
    "failure_at": False,
}


class PublicationRepository:
    def __init__(self, session):
        self.session = session

    async def rows(self, sql, **params):
        return [dict(r) for r in (await self.session.execute(text(sql), params)).mappings()]

    async def execute(self, sql, **params):
        await self.session.execute(text(sql), params)

    async def audit(self, actor, action, resource, details=None):
        await self.execute(
            """INSERT INTO publication.audit_events(actor_id,action,resource_id,details)
            VALUES(:actor,:action,:resource,cast(:details AS jsonb))""",
            actor=actor,
            action=action,
            resource=resource,
            details=json.dumps(details or {}),
        )

    async def metrics(self):
        return dict(
            (
                await self.rows("""SELECT
            (SELECT count(*) FROM publication.config_outbox WHERE delivered_at IS NULL)
            publication_backlog,
            (SELECT max(extract(epoch FROM clock_timestamp()-s.created_at)) FROM
            publication.config_snapshots s
              JOIN publication.config_outbox o USING(cluster_id, revision) WHERE o.delivered_at
              IS NULL) publication_lag_seconds,
            (SELECT count(*) FROM publication.gateway_principals n JOIN
            publication.gateway_clusters c ON c.id=n.cluster_id
             WHERE c.active AND n.active AND
                n.applied_revision<c.desired_revision) lagging_nodes,
            (SELECT count(*) FROM publication.cluster_automation a JOIN
            publication.gateway_clusters c ON c.id=a.cluster_id WHERE c.active AND c.auto_publish
             AND a.pending_since IS NOT NULL) auto_publish_pending,
            (SELECT max(extract(epoch FROM clock_timestamp()-a.pending_since)) FROM
            publication.cluster_automation a JOIN publication.gateway_clusters c
             ON c.id=a.cluster_id WHERE c.active AND c.auto_publish) auto_publish_pending_seconds,
            (SELECT count(*) FROM publication.cluster_automation a JOIN
            publication.gateway_clusters c ON c.id=a.cluster_id WHERE c.active AND c.auto_publish
             AND a.failed IS NOT NULL) auto_publish_failing,
            (SELECT count(*) FROM publication.cluster_automation a JOIN
            publication.gateway_clusters c ON c.id=a.cluster_id WHERE NOT c.auto_publish
             AND a.paused_at IS NOT NULL) auto_publish_paused""")
            )[0]
        )

    async def cluster(self, cid, lock=False):
        rows = await self.rows(
            CLUSTER + "WHERE c.id=:cid" + (" FOR UPDATE OF c" if lock else ""),
            cid=cid,
        )
        if not rows:
            raise ServiceError("not_found", "Cluster not found", 404)
        return rows[0]

    async def create_cluster(self, cid, body, key):
        await self.execute(
            """INSERT INTO publication.gateway_clusters(
            id,name,region,organization_scope,organization_ids,deployment_ids,public_key,
            auto_publish)
        VALUES(:id,:name,:region,:scope,:orgs,:deps,:key,:auto)""",
            id=cid,
            name=body.name,
            region=body.region,
            scope=body.organization_scope,
            orgs=body.organization_ids,
            deps=body.deployment_ids,
            key=key,
            auto=body.auto_publish,
        )
        await self.execute(
            "INSERT INTO publication.cluster_automation(cluster_id) VALUES(:cid)", cid=cid
        )
        await self.execute(
            "INSERT INTO publication.cluster_policies(cluster_id) VALUES(:cid)", cid=cid
        )

    async def defaults(self, lock=False):
        return (
            await self.rows(
                f"SELECT {','.join(POLICY_COLUMNS)},version,managed_sha256,managed_at "
                "FROM publication.cluster_policy_defaults" + (" FOR UPDATE" if lock else "")
            )
        )[0]

    async def set_defaults(self, values, version, *, managed=KEEP):
        """Replace the defaults at ``version``; ``managed`` locks (SHA-256) or unlocks (None)."""
        extra = {} if managed is KEEP else {"managed": managed}
        rows = await self.rows(
            "UPDATE publication.cluster_policy_defaults SET "
            + ",".join(f"{name}=:{name}" for name in POLICY_COLUMNS)
            + ",version=version+1"
            + ("" if managed is KEEP else LOCK)
            + " WHERE version=:version RETURNING version",
            version=version,
            **values,
            **extra,
        )
        return bool(rows)

    async def overrides(self, cid, lock=False):
        rows = await self.rows(
            f"SELECT cluster_id,{','.join(POLICY_COLUMNS)},version "
            "FROM publication.cluster_policies WHERE cluster_id=:cid"
            + (" FOR UPDATE" if lock else ""),
            cid=cid,
        )
        if not rows:
            raise ServiceError("not_found", "Cluster not found", 404)
        return rows[0]

    async def set_overrides(self, cid, values, version):
        rows = await self.rows(
            "UPDATE publication.cluster_policies SET "
            + ",".join(f"{name}=:{name}" for name in POLICY_COLUMNS)
            + ",version=version+1 WHERE cluster_id=:cid AND version=:version RETURNING version",
            cid=cid,
            version=version,
            **values,
        )
        return bool(rows)

    async def register(self, cid, body):
        roles = await self.rows(
            """SELECT 1 AS valid FROM pg_roles WHERE rolname=:name AND rolcanlogin
            AND NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)
            AND NOT EXISTS(SELECT FROM pg_namespace ns
             WHERE ns.nspname IN ('identity','catalog','billing','publication','reporting')
             AND has_schema_privilege(pg_roles.oid,ns.oid,'USAGE'))
            AND pg_has_role(oid,'nexus_gateway','USAGE') AND NOT EXISTS(
             SELECT FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
             WHERE m.member=pg_roles.oid AND g.rolname<>'nexus_gateway')""",
            name=body.database_role,
        )
        if not roles:
            raise ServiceError("invalid_gateway_role", "A dedicated gateway login is required", 422)
        rows = await self.rows(
            """INSERT INTO publication.gateway_principals(
            node_id,cluster_id,database_role,capabilities,active)
        VALUES(:node, :cid, :role, :caps, :active) ON CONFLICT(node_id) DO UPDATE SET
        active=excluded.active,
        capabilities=excluded.capabilities, applied_revision=CASE WHEN
        gateway_principals.capabilities=excluded.capabilities
        AND gateway_principals.active=excluded.active
        THEN gateway_principals.applied_revision ELSE 0 END WHERE
        gateway_principals.cluster_id=excluded.cluster_id
        AND gateway_principals.database_role=excluded.database_role RETURNING node_id""",
            node=body.node_id,
            cid=cid,
            role=body.database_role,
            caps=body.capabilities,
            active=body.active,
        )
        if not rows:
            raise ServiceError("principal_conflict", "Node binding is immutable", 409)
        await self.execute(
            "UPDATE publication.gateway_clusters SET version=version+1 WHERE id=:cid", cid=cid
        )

    async def list_clusters(self, limit, cursor):
        rows = await self.rows(
            CLUSTER + ("WHERE c.id>:after " if cursor else "") + "ORDER BY c.id LIMIT :limit",
            after=cursor and cursor[0],
            limit=limit + 1,
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and key_cursor(last["id"])

    async def update_cluster(self, c, body, new_key, scope_changed, scope, orgs):
        if new_key:
            await self.execute(
                """INSERT INTO publication.previous_keys VALUES(
                 :cid,:ver,clock_timestamp()+make_interval(secs=>:grace))""",
                cid=c["id"],
                ver=c["key_version"],
                grace=body.key_grace_seconds,
            )
        await self.execute(
            """UPDATE publication.gateway_clusters SET
             organization_scope=:scope,organization_ids=:orgs,deployment_ids=:deps,
             active=:active,public_key=:key,key_version=:kv,scope_generation=:generation,
             auto_publish=:auto,version=version+1 WHERE id=:cid""",
            scope=scope,
            orgs=orgs,
            deps=body.deployment_ids or c["deployment_ids"],
            active=c["active"] if body.active is None else body.active,
            key=new_key or c["public_key"],
            kv=c["key_version"] + int(bool(new_key)),
            generation=c["scope_generation"] + int(scope_changed),
            auto=c["auto_publish"] if body.auto_publish is None else body.auto_publish,
            cid=c["id"],
        )
        if body.auto_publish is not None:
            # An explicit choice supersedes a pause left by a rollback.
            await self.execute(
                """UPDATE publication.cluster_automation SET paused_revision=NULL,
                paused_at=NULL WHERE cluster_id=:cid""",
                cid=c["id"],
            )

    async def capabilities(self, cid):
        return await self.rows(
            "SELECT capabilities FROM publication.gateway_principals WHERE "
            "cluster_id=:cid AND active",
            cid=cid,
        )

    async def delivery(self, cid, rev):
        jobs = await self.rows(
            """SELECT attempts,delivered_at FROM publication.config_outbox
                             WHERE cluster_id=:cid AND revision=:rev""",
            cid=cid,
            rev=rev,
        )
        nodes = await self.rows(
            """SELECT n.node_id,n.active,n.applied_revision,
            (SELECT jsonb_build_object('revision',a.revision::text,'applied',a.applied,
            'error_code',a.error_code) FROM publication.node_config_acks a
            WHERE a.node_id=n.node_id ORDER BY a.created_at DESC LIMIT 1) last_ack
            FROM publication.gateway_principals n WHERE n.cluster_id=:cid ORDER BY n.node_id""",
            cid=cid,
        )
        return jobs[0], nodes

    async def list_principals(self, cid, limit, cursor):
        rows = await self.rows(
            """SELECT n.node_id,n.database_role,n.capabilities,n.active,n.applied_revision,
            n.applied_at,(SELECT jsonb_build_object('revision',a.revision::text,
            'applied',a.applied,'error_code',a.error_code) FROM publication.node_config_acks a
            WHERE a.node_id=n.node_id ORDER BY a.created_at DESC LIMIT 1) last_ack
            FROM publication.gateway_principals n WHERE n.cluster_id=:cid """
            + ("AND n.node_id>:after " if cursor else "")
            + "ORDER BY n.node_id LIMIT :limit",
            cid=cid,
            after=cursor and cursor[0],
            limit=limit + 1,
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and key_cursor(last["node_id"])

    async def list_releases(self, cid, limit, cursor):
        # Every snapshot is written with its outbox job; only operator releases also store a
        # release command, so a snapshot without one was published automatically.
        rows = await self.rows(
            """SELECT s.revision,s.sha256,s.created_at,
            s.source_revision->>'rollback_revision' rollback_revision,
            (SELECT r.actor_id FROM publication.release_commands r
             WHERE r.cluster_id=s.cluster_id AND r.revision=s.revision LIMIT 1) actor_id,
            EXISTS(SELECT FROM publication.release_commands r
             WHERE r.cluster_id=s.cluster_id AND r.revision=s.revision) manual,
            o.attempts,o.delivered_at FROM publication.config_snapshots s
            JOIN publication.config_outbox o USING(cluster_id,revision)
            WHERE s.cluster_id=:cid """
            + ("AND s.revision<:before " if cursor else "")
            + "ORDER BY s.revision DESC LIMIT :limit",
            cid=cid,
            before=cursor and cursor[0],
            limit=limit + 1,
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and key_cursor(last["revision"])

    async def claim_release(self):
        jobs = await self.rows("""WITH candidate AS (
            SELECT o.cluster_id,o.revision FROM publication.config_outbox o
            WHERE (delivered_at IS NULL OR EXISTS(SELECT FROM publication.gateway_clusters c
            WHERE c.id=o.cluster_id AND c.active AND c.desired_revision=o.revision))
            AND next_attempt_at<=clock_timestamp()
            ORDER BY next_attempt_at LIMIT 1 FOR UPDATE SKIP LOCKED
        ) UPDATE publication.config_outbox o SET attempts=attempts+1,
            next_attempt_at=clock_timestamp()+interval '2 seconds' FROM candidate c
            WHERE o.cluster_id=c.cluster_id AND o.revision=c.revision RETURNING o.cluster_id,
            o.revision""")
        return await self.snapshot(jobs[0]["cluster_id"], jobs[0]["revision"]) if jobs else None

    async def register_limit_namespace(self, org):
        rows = await self.rows(
            """INSERT INTO publication.limit_namespaces(organization_id)
            VALUES(:org) ON CONFLICT DO NOTHING RETURNING organization_id""",
            org=org,
        )
        return bool(rows)

    async def distributed(self, cid, rev):
        await self.execute(
            """UPDATE publication.config_outbox SET
            delivered_at=coalesce(delivered_at,clock_timestamp()),
            next_attempt_at=clock_timestamp()+interval '30 seconds'
            WHERE cluster_id=:cid AND revision=:rev""",
            cid=cid,
            rev=rev,
        )
        await self.execute(
            """UPDATE publication.gateway_clusters
            SET distributed_revision=greatest(distributed_revision,:rev) WHERE id=:cid""",
            cid=cid,
            rev=rev,
        )

    async def replay(self, cid, actor, key, parameters):
        rows = await self.rows(
            """SELECT revision,parameters FROM publication.release_commands
         WHERE cluster_id=:cid AND actor_id=:actor AND key_digest=:key""",
            cid=cid,
            actor=actor,
            key=key,
        )
        if rows and rows[0]["parameters"] != parameters:
            raise ServiceError("idempotency_conflict", "Release key has different parameters", 409)
        return rows[0]["revision"] if rows else None

    async def snapshot(self, cid, rev):
        rows = await self.rows(
            "SELECT * FROM publication.config_snapshots WHERE cluster_id=:cid AND revision=:rev",
            cid=cid,
            rev=rev,
        )
        if not rows:
            raise ServiceError("not_found", "Snapshot not found", 404)
        return rows[0]

    async def publish(
        self, cid, rev, raw, sha, sources, envelopes, key_version, actor, key, parameters
    ):
        """Store a release; automatic releases (key None) have no idempotency record."""
        await self.execute(
            """INSERT INTO publication.config_snapshots(
            cluster_id,revision,manifest,payload_bytes,sha256,source_revision)
        VALUES(:cid,:rev,cast(:manifest AS jsonb),:raw,:sha,cast(:sources AS jsonb))""",
            cid=cid,
            rev=rev,
            manifest=raw.decode(),
            raw=raw,
            sha=sha,
            sources=json.dumps(sources),
        )
        for credential, (version, ciphertext) in envelopes.items():
            await self.execute(
                "INSERT INTO publication.credential_envelopes VALUES(:cid,:rev,"
                ":credential,:version,:key_version,:ciphertext)",
                cid=cid,
                rev=rev,
                credential=credential,
                version=version,
                key_version=key_version,
                ciphertext=ciphertext,
            )
        await self.execute(
            "INSERT INTO publication.config_outbox(cluster_id,revision) VALUES(:cid,:rev)",
            cid=cid,
            rev=rev,
        )
        await self.execute(
            "UPDATE publication.gateway_clusters SET desired_revision=:rev WHERE id=:cid",
            cid=cid,
            rev=rev,
        )
        if key is not None:
            await self.execute(
                "INSERT INTO publication.release_commands "
                "VALUES(:cid,:actor,:key,:parameters,:rev)",
                cid=cid,
                actor=actor,
                key=key,
                parameters=parameters,
                rev=rev,
            )

    async def settle(self, cid, rev, sources, digest, models, trigger=None):
        """Record what the desired snapshot represents; it ends any pending change."""
        await self.execute(
            """INSERT INTO publication.cluster_automation(cluster_id) VALUES(:cid)
            ON CONFLICT DO NOTHING""",
            cid=cid,
        )
        await self.execute(
            """UPDATE publication.cluster_automation SET settled=cast(:sources AS jsonb),
            content_digest=:digest,content_revision=:rev,price_models=:models,observed=NULL,
            changed_at=NULL,pending_since=NULL,retry_after=NULL,failed=NULL,
            published_revision=CASE WHEN :automatic THEN :rev ELSE published_revision END,
            published_at=CASE WHEN :automatic THEN clock_timestamp() ELSE published_at END,
            published_trigger=CASE WHEN :automatic THEN cast(:trigger AS jsonb)
             ELSE published_trigger END
            WHERE cluster_id=:cid""",
            cid=cid,
            rev=rev,
            sources=None if sources is None else json.dumps(sources),
            digest=digest,
            models=models,
            automatic=trigger is not None,
            trigger=json.dumps(trigger),
        )

    async def pause(self, cid, rev):
        """A rollback stops the auto-publisher until an administrator resumes it."""
        await self.execute(
            """UPDATE publication.gateway_clusters SET auto_publish=false,version=version+1
            WHERE id=:cid""",
            cid=cid,
        )
        await self.execute(
            """UPDATE publication.cluster_automation SET paused_revision=:rev,
            paused_at=clock_timestamp() WHERE cluster_id=:cid""",
            cid=cid,
            rev=rev,
        )

    async def automated_clusters(self):
        return [
            r["id"]
            for r in await self.rows(
                """SELECT id FROM publication.gateway_clusters WHERE active AND auto_publish
                ORDER BY id"""
            )
        ]

    async def claim_automation(self, cid, lease):
        """Lease one cluster to this worker; another worker skips it until released."""
        claimed = await self.rows(
            """WITH candidate AS (
            SELECT a.cluster_id FROM publication.cluster_automation a
            JOIN publication.gateway_clusters c ON c.id=a.cluster_id
            WHERE a.cluster_id=:cid AND c.active AND c.auto_publish
            AND (a.lease_until IS NULL OR a.lease_until<clock_timestamp())
            FOR UPDATE OF a SKIP LOCKED
            ) UPDATE publication.cluster_automation a
            SET lease_until=clock_timestamp()+make_interval(secs=>:lease) FROM candidate
            WHERE a.cluster_id=candidate.cluster_id
            RETURNING a.settled,a.price_models,a.observed,a.changed_at,a.pending_since,
            a.retry_after,a.failed,clock_timestamp() AS now""",
            cid=cid,
            lease=lease,
        )
        if not claimed:
            return None
        cluster = await self.rows(CLUSTER + "WHERE c.id=:cid", cid=cid)
        return cluster[0], claimed[0]

    async def defer(self, cid, code, message, seconds):
        await self.execute(
            """UPDATE publication.cluster_automation SET lease_until=NULL,failure_code=:code,
            failure_message=:message,failure_at=clock_timestamp(),
            retry_after=clock_timestamp()+make_interval(secs=>:seconds) WHERE cluster_id=:cid""",
            cid=cid,
            code=code,
            message=message,
            seconds=seconds,
        )

    async def track(self, cid, **values):
        """Save probe bookkeeping and release this worker's lease."""
        assignments = ["lease_until=NULL"]
        params = {"cid": cid}
        for column, value in values.items():
            document = TRACKED[column]
            assignments.append(
                f"{column}=cast(:{column} AS jsonb)" if document else f"{column}=:{column}"
            )
            params[column] = json.dumps(value) if document and value is not None else value
        await self.execute(
            "UPDATE publication.cluster_automation SET "
            + ",".join(assignments)
            + " WHERE cluster_id=:cid",
            **params,
        )


@asynccontextmanager
async def transaction(database):
    try:
        async with database.transaction() as session:
            yield PublicationRepository(session)
    except IntegrityError:
        raise ServiceError(
            "publication_conflict", "Publication binding already exists", 409
        ) from None
    except SQLAlchemyError:
        raise ServiceError(
            "storage_unavailable", "Publication storage is unavailable", 503
        ) from None
