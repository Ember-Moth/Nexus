import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from nexus.shared.errors import ServiceError


class PublicationRepository:
    def __init__(self, session):
        self.session = session

    async def rows(self, sql, **params):
        return [dict(r) for r in (await self.session.execute(text(sql), params)).mappings()]

    async def execute(self, sql, **params):
        await self.session.execute(text(sql), params)

    async def cluster(self, cid, lock=False):
        rows = await self.rows(
            "SELECT * FROM publication.gateway_clusters WHERE id=:cid"
            + (" FOR UPDATE" if lock else ""),
            cid=cid,
        )
        if not rows:
            raise ServiceError("not_found", "Cluster not found", 404)
        return rows[0]

    async def create_cluster(self, cid, body, key):
        await self.execute(
            """INSERT INTO publication.gateway_clusters(
            id,name,region,organization_ids,deployment_ids,public_key)
        VALUES(:id,:name,:region,:orgs,:deps,:key)""",
            id=cid,
            name=body.name,
            region=body.region,
            orgs=body.organization_ids,
            deps=body.deployment_ids,
            key=key,
        )

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

    async def list_clusters(self):
        return await self.rows("SELECT * FROM publication.gateway_clusters ORDER BY id")

    async def update_cluster(self, c, body, new_key, scope_changed):
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
             organization_ids=:orgs,deployment_ids=:deps,active=:active,public_key=:key,
             key_version=:kv,scope_generation=:generation,version=version+1 WHERE id=:cid""",
            orgs=body.organization_ids or c["organization_ids"],
            deps=body.deployment_ids or c["deployment_ids"],
            active=c["active"] if body.active is None else body.active,
            key=new_key or c["public_key"],
            kv=c["key_version"] + int(bool(new_key)),
            generation=c["scope_generation"] + int(scope_changed),
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
        await self.execute(
            "INSERT INTO publication.release_commands VALUES(:cid,:actor,:key,:parameters,:rev)",
            cid=cid,
            actor=actor,
            key=key,
            parameters=parameters,
            rev=rev,
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
