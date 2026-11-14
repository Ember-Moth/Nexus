"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.catalogModels"
      endpoints={["GET/POST /models", "GET/PATCH /models/{id}（If-Match）"]}
    />
  );
}
