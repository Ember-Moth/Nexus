"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.discounts"
      endpoints={["GET/POST /organizations/{org}/discount-versions（Idempotency-Key）", "POST /organizations/{org}/discount-versions/{id}/publish（If-Match + Idempotency-Key）"]}
    />
  );
}
