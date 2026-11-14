"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.costPrices"
      endpoints={["GET/POST /cost-price-versions?scope_id=（Idempotency-Key）", "PATCH /cost-price-versions/{id}（If-Match）", "POST /cost-price-versions/{id}/publish（If-Match + Idempotency-Key）"]}
    />
  );
}
