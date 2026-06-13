"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/lib/auth";
import { Spinner } from "@/components/ui";

export default function Home() {
  const { me, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    router.replace(me ? "/dashboard" : "/login");
  }, [me, loading, router]);

  return (
    <div className="flex h-screen items-center justify-center">
      <Spinner className="h-8 w-8" />
    </div>
  );
}
