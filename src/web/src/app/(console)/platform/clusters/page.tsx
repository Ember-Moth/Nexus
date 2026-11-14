"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.clusters"
      endpoints={["GET/POST /gateway-clusters", "PATCH /gateway-clusters/{id}（If-Match）", "PUT /gateway-clusters/{id}/principals/{node_id}", "POST /gateway-clusters/{id}/releases（202，Idempotency-Key）", "GET /gateway-clusters/{id}/releases/{revision}"]}
    />
  );
}
