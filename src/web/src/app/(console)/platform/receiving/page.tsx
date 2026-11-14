"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.receiving"
      endpoints={["GET/POST /receiving-accounts（Idempotency-Key）", "PATCH /receiving-accounts/{id}（If-Match）"]}
    />
  );
}
