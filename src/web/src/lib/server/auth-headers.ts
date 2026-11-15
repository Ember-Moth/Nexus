import { createHmac } from "node:crypto";
import { isIP } from "node:net";

/** Signature the API verifies with NEXUS_AUTH_PROXY_SECRET before trusting the address. */
export function clientIpSignature(secret: string, address: string): string {
  return createHmac("sha256", secret).update(`nexus-client-ip:v1:${address}`).digest("base64url");
}

/**
 * Only a single IP supplied by the deployment's trusted ingress is forwarded, and only with the
 * shared secret's signature: the API ignores unsigned addresses, because a proxy in front of the
 * BFF could pass a client-chosen header through.
 */
export function authRequestHeaders(incoming: Headers): Headers {
  const headers = new Headers({ "content-type": "application/json", accept: "application/json" });
  const source = process.env.NEXUS_WEB_CLIENT_IP_HEADER;
  const secret = process.env.NEXUS_AUTH_PROXY_SECRET;
  const address = source && incoming.get(source);
  if (secret && address && !address.includes("%") && isIP(address)) {
    headers.set("X-Nexus-Client-IP", address);
    headers.set("X-Nexus-Client-IP-Signature", clientIpSignature(secret, address));
  }
  return headers;
}
