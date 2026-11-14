"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.channels"
      endpoints={["GET /payment-gateway-types", "GET/POST /payment-channels（Idempotency-Key）", "PATCH /payment-channels/{id}（If-Match + Idempotency-Key）", "GET /payment-channels/{id}/versions"]}
    />
  );
}
