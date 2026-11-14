"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.quotas"
      endpoints={["GET/POST /organizations/{org}/quota-policies（契约调整中：POST upsert → PUT）"]}
    />
  );
}
