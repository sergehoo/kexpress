"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** CRUD générique sur une ressource REST (`/<resource>/`). Invalide le cache à chaque succès. */
export function useCrud(resource: string, invalidateKeys: string[] = []) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: [resource] });
    invalidateKeys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  };

  const create = useMutation({
    mutationFn: async (body: Record<string, unknown>) => {
      const { data } = await api.post(`/${resource}/`, body);
      return data;
    },
    onSuccess: invalidate,
  });

  const update = useMutation({
    mutationFn: async ({ id, body }: { id: string; body: Record<string, unknown> }) => {
      const { data } = await api.patch(`/${resource}/${id}/`, body);
      return data;
    },
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: async (id: string) => {
      await api.delete(`/${resource}/${id}/`);
    },
    onSuccess: invalidate,
  });

  return { create, update, remove };
}
