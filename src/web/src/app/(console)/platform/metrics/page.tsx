"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.metrics"
      endpoints={["GET /internal/metrics"]}
    />
  );
}
