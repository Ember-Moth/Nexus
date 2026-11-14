"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.salePrices"
      endpoints={["GET/POST /sale-price-versions?scope_id=（Idempotency-Key）", "PATCH /sale-price-versions/{id}（If-Match）", "POST /sale-price-versions/{id}/publish（If-Match + Idempotency-Key）"]}
    />
  );
}
