"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.adjustments"
      endpoints={["GET/POST /organizations/{organization_id}/adjustments（Idempotency-Key）", "POST /organizations/{organization_id}/adjustments/{id}/post（If-Match + Idempotency-Key）"]}
    />
  );
}
