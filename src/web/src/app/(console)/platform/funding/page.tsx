"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.funding"
      endpoints={["GET/POST /organizations/{organization_id}/funding-records（Idempotency-Key）", "POST /organizations/{organization_id}/funding-records/{id}/post（If-Match + Idempotency-Key）", "POST /organizations/{organization_id}/funding-records/{id}/cancel（If-Match）"]}
    />
  );
}
