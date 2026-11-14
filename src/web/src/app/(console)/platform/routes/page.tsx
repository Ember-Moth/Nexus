"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.routes"
      endpoints={["GET/POST /route-policies（契约调整中：POST upsert → PUT）"]}
    />
  );
}
