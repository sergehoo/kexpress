"use client";

import { useState } from "react";
import { Pencil, Trash2 } from "lucide-react";

import { Button } from "@/components/ui";
import { Modal } from "@/components/Modal";

export function RowActions({
  onEdit,
  onDelete,
  deleting,
  label,
}: {
  onEdit: () => void;
  onDelete: () => void;
  deleting?: boolean;
  label?: string;
}) {
  const [confirm, setConfirm] = useState(false);
  return (
    <div className="flex justify-end gap-1">
      <button
        onClick={onEdit}
        className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-brand-600"
        title="Éditer"
        aria-label="Éditer"
      >
        <Pencil className="h-4 w-4" />
      </button>
      <button
        onClick={() => setConfirm(true)}
        className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-rose-600"
        title="Supprimer"
        aria-label="Supprimer"
      >
        <Trash2 className="h-4 w-4" />
      </button>

      <Modal open={confirm} title="Confirmer la suppression" onClose={() => setConfirm(false)}>
        <p className="text-sm text-muted">
          Voulez-vous vraiment supprimer {label ? <span className="font-medium text-ink">{label}</span> : "cet élément"} ? Cette action est irréversible.
        </p>
        <div className="flex justify-end gap-2 pt-4">
          <Button variant="secondary" onClick={() => setConfirm(false)}>Annuler</Button>
          <Button
            variant="danger"
            disabled={deleting}
            onClick={() => {
              onDelete();
              setConfirm(false);
            }}
          >
            Supprimer
          </Button>
        </div>
      </Modal>
    </div>
  );
}
