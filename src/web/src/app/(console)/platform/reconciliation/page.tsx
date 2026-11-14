"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.reconciliation"
      endpoints={["GET /reconciliation-cases?organization_id=&state=", "GET /reconciliation-cases/{id}", "POST /reconciliation-cases/{id}/resolve（If-Match + Idempotency-Key）", "POST /organizations/{org}/account/unfreeze（If-Match + Idempotency-Key）"]}
    />
  );
}
