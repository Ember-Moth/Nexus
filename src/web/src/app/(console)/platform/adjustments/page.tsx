"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.adjustments"
      endpoints={["GET/POST /organizations/{org}/adjustments（Idempotency-Key）", "POST /organizations/{org}/adjustments/{id}/post（If-Match + Idempotency-Key）"]}
    />
  );
}
