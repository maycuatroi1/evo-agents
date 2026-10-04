import type { ReactNode } from "react";

import { AdminNav } from "@/components/admin/admin-nav";

/** Every admin page: the area's tabs (hub admins only), then the page. */
export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <AdminNav />
      {children}
    </>
  );
}
