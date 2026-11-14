import { isIP } from "node:net";

/** Only a single IP supplied by the deployment's trusted ingress is forwarded. */
export function authRequestHeaders(incoming: Headers): Headers {
  const headers = new Headers({ "content-type": "application/json", accept: "application/json" });
  const source = process.env.NEXUS_WEB_CLIENT_IP_HEADER;
  const address = source && incoming.get(source);
  if (address && !address.includes("%") && isIP(address)) {
    headers.set("X-Nexus-Client-IP", address);
  }
  return headers;
}
