"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.paymentCases"
      endpoints={["GET /organizations/{org}/payment-cases", "POST /organizations/{org}/payment-orders/{id}/reconcile（202）", "GET /organizations/{org}/payment-orders/{id}/observations", "GET /organizations/{org}/payment-orders/{id}/receipts"]}
    />
  );
}
