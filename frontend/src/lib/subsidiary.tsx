"use client";

import { createContext, useContext, useState } from "react";

interface SubsidiaryState {
  selected: string; // "" = toutes (périmètre entreprise)
  setSelected: (id: string) => void;
}

const Ctx = createContext<SubsidiaryState | null>(null);

export function SubsidiaryProvider({ children }: { children: React.ReactNode }) {
  const [selected, setSelected] = useState("");
  return <Ctx.Provider value={{ selected, setSelected }}>{children}</Ctx.Provider>;
}

export function useSubsidiaryFilter() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSubsidiaryFilter doit être utilisé dans SubsidiaryProvider");
  return ctx;
}
