"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.paymentCases"
      endpoints={["GET /organizations/{organization_id}/payment-cases", "POST /organizations/{organization_id}/payment-orders/{id}/reconcile（202）", "GET /organizations/{organization_id}/payment-orders/{id}/observations", "GET /organizations/{organization_id}/payment-orders/{id}/receipts"]}
    />
  );
}
