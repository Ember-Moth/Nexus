#!/usr/bin/env node
// Seeds a local development API with a demo enterprise that has money in its wallet, so the
// console's balance and ledger pages have data. Development use only.
//
//   NEXUS_API_URL=http://127.0.0.1:8000 NEXUS_ADMIN_LOGIN=admin \
//   NEXUS_ADMIN_PASSWORD_FILE=../../.local/dev-admin-password pnpm dev:seed
//
// Steps (all idempotent): grant the admin platform_finance, create the enterprise
// "演示企业" if missing, wait until the worker activates it, create a manual receiving
// account and post a USD 1000.00 funding record.
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";

const base = (process.env.NEXUS_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "") + "/api/v1";
const login = process.env.NEXUS_ADMIN_LOGIN ?? "admin";
const password =
  process.env.NEXUS_ADMIN_PASSWORD ??
  (process.env.NEXUS_ADMIN_PASSWORD_FILE
    ? (await readFile(process.env.NEXUS_ADMIN_PASSWORD_FILE, "utf8")).trim()
    : undefined);
const orgName = process.env.NEXUS_SEED_ORG ?? "演示企业";
if (!password) {
  console.error("Set NEXUS_ADMIN_PASSWORD or NEXUS_ADMIN_PASSWORD_FILE.");
  process.exit(1);
}

let token;
async function api(method, path, { body, headers = {}, ok = [200, 201, 202, 204] } = {}) {
  const response = await fetch(base + path, {
    method,
    headers: {
      accept: "application/json",
      ...(body ? { "content-type": "application/json" } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  const json = text ? JSON.parse(text) : null;
  if (!ok.includes(response.status)) {
    throw new Error(`${method} ${path} -> ${response.status} ${text}`);
  }
  return { json, etag: response.headers.get("etag"), status: response.status };
}

const session = await api("POST", "/auth/sessions", { body: { login, password } });
token = session.json.access_token;
try {
  const me = (await api("GET", "/me")).json;
  const roles = await api("GET", `/users/${me.user.id}/platform-roles`);
  if (!roles.json.roles.includes("platform_finance")) {
    await api("PATCH", `/users/${me.user.id}/platform-roles`, {
      body: { roles: [...roles.json.roles, "platform_finance"] },
      headers: { "if-match": roles.etag },
    });
    console.log("granted platform_finance");
  }

  const orgs = (await api("GET", "/organizations?limit=100")).json.items;
  let org = orgs.find((item) => item.name === orgName);
  if (!org) {
    org = (await api("POST", "/organizations", { body: { name: orgName } })).json;
    console.log(`created organization ${org.id}`);
  }
  for (let i = 0; i < 40 && org.status !== "active"; i++) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    org = (await api("GET", `/organizations/${org.id}`)).json;
  }
  if (org.status !== "active") {
    throw new Error(`organization is ${org.status}; is the worker running?`);
  }

  const accounts = (await api("GET", "/receiving-accounts?limit=100")).json.items;
  let receiving = accounts.find((item) => item.reference === "demo-bank");
  if (!receiving) {
    receiving = (
      await api("POST", "/receiving-accounts", {
        body: { name: "演示银行账户", reference: "demo-bank", currency: "USD" },
        headers: { "idempotency-key": "seed:receiving:demo-bank" },
      })
    ).json;
  }

  const funding = (await api("GET", `/organizations/${org.id}/funding-records?limit=100`)).json.items;
  if (!funding.some((item) => item.status === "posted")) {
    const draft = await api("POST", `/organizations/${org.id}/funding-records`, {
      body: {
        receiving_account_id: receiving.id,
        external_reference: `seed-${randomUUID()}`,
        amount: "1000.00",
        currency: "USD",
        received_at: new Date(Date.now() - 60_000).toISOString(),
        evidence: "development seed",
      },
      headers: { "idempotency-key": `seed:funding:${org.id}` },
    });
    await api("POST", `/organizations/${org.id}/funding-records/${draft.json.id}/post`, {
      headers: { "if-match": draft.etag, "idempotency-key": `seed:post:${draft.json.id}` },
    });
    console.log("posted USD 1000.00");
  }
  const account = (await api("GET", `/organizations/${org.id}/account`)).json;
  console.log(`organization ${org.id} balance ${account.balance} ${account.currency}`);
} finally {
  await api("DELETE", "/auth/sessions/current", { ok: [204, 401] });
}
